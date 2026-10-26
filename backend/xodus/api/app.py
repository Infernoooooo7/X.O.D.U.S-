"""HTTP API: health, streamed chat over SSE, capability listing/invocation, and the static UI."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from xodus import __version__
from xodus.capabilities.list_files import ListFiles
from xodus.capabilities.registry import CapabilityError, CapabilityRegistry
from xodus.config.settings import REPO_ROOT, Settings
from xodus.obs import log_event
from xodus.providers.ollama import OllamaProvider, ProviderError
from xodus.resources.admission import AdmissionGate, QueueFull
from xodus.resources.sampler import Nvml, ResourceSampler, mib

UI_DIR = REPO_ROOT / "frontend" / "out"


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    num_ctx: int | None = None
    seed: int | None = None


class InvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    args: dict[str, Any] = {}


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


class _VramPeak:
    """Polls NVML device memory while a request runs (200 ms, master prompt 3.6)."""

    def __init__(self, nvml: Nvml, interval_s: float = 0.2) -> None:
        self._nvml = nvml
        self._interval = interval_s
        self.peak: int | None = None
        self._task: asyncio.Task[None] | None = None

    def _read(self) -> None:
        mem = self._nvml.memory()
        if mem is not None:
            self.peak = max(self.peak or 0, mem["used"])

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            self._read()

    def start(self) -> None:
        self._read()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> int | None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._read()
        return self.peak


def _resource_summary(snap: dict[str, Any], vram_peak: int | None) -> dict[str, Any]:
    vram = snap["device_vram"] or {}
    return {
        "uss_total_mib": mib(snap["totals"]["uss"]),
        "uss_xodus_app_mib": mib(snap["totals"]["uss_xodus_app"]),
        "uss_ollama_mib": mib(snap["totals"]["uss_ollama"]),
        "uss_complete": snap["uss_complete"],
        "rss_total_mib": mib(snap["totals"]["rss"]),
        "private_commit_total_mib": mib(snap["totals"]["private_commit"]),
        "gpu_dedicated_ollama_mib": mib(snap["totals"]["gpu_dedicated_ollama"]),
        "gpu_shared_ollama_mib": mib(snap["totals"]["gpu_shared_ollama"]),
        "device_vram_used_mib": mib(vram.get("used")),
        "device_vram_peak_mib": mib(vram_peak),
        "device_vram_total_mib": mib(vram.get("total")),
        "system_in_use_mib": mib(snap["system_memory"]["in_use"]),
        "processes": [
            {"pid": p["pid"], "name": p["name"], "group": p["group"], "uss_mib": mib(p["uss"]), "rss_mib": mib(p["rss"]),
             "gpu_dedicated_mib": mib(p["gpu_dedicated"])}
            for p in snap["processes"]
        ],
        "sample_ms": snap["sample_ms"],
        "excluded": snap["excluded"],
    }


def create_app(
    settings: Settings,
    provider: OllamaProvider | None = None,
    sampler: ResourceSampler | None = None,
) -> FastAPI:
    provider = provider or OllamaProvider(settings.ollama)
    sampler = sampler or ResourceSampler(ollama_port=urlparse(settings.ollama.base_url).port or 11434)
    gate = AdmissionGate(settings.envelope.max_running, settings.envelope.max_queued)
    registry = CapabilityRegistry()
    registry.register(ListFiles(settings.capabilities.list_files_root, settings.capabilities.list_files_max_entries))
    started_at = time.time()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        health = await provider.health()
        log_event(
            "startup",
            version=__version__,
            bind=f"{settings.server.host}:{settings.server.port}",
            ollama=health,
            measurement={"nvml": sampler.nvml.error or "ok", "gpu_counters": sampler.gpu.error or "ok"},
            capabilities=[c["id"] for c in registry.describe()],
            ui_built=UI_DIR.is_dir(),
        )
        yield
        await provider.aclose()
        sampler.close()
        log_event("shutdown")

    app = FastAPI(title="XODUS", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    # Requests must name the loopback host we bind to; blocks DNS-rebinding style Host headers.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "backend": {"status": "ok", "version": __version__, "uptime_s": round(time.time() - started_at, 1)},
            "ollama": await provider.health(),
            "envelope": {"max_running": gate.max_running, "max_queued": gate.max_queued,
                         "running": gate.running, "queued": gate.waiting},
            "measurement": {"nvml": sampler.nvml.error or "ok", "gpu_counters": sampler.gpu.error or "ok"},
        }

    @app.post("/api/chat")
    async def chat(req: ChatRequest) -> StreamingResponse:
        if len(req.message) > settings.chat.max_message_chars:
            raise HTTPException(422, f"Message is longer than {settings.chat.max_message_chars} characters.")
        num_ctx = req.num_ctx or settings.ollama.num_ctx
        if num_ctx not in settings.ollama.allowed_num_ctx:
            raise HTTPException(422, f"num_ctx must be one of {settings.ollama.allowed_num_ctx}.")
        request_id = uuid.uuid4().hex[:12]
        messages = [{"role": "user", "content": req.message}]
        if settings.chat.system_prompt:
            messages.insert(0, {"role": "system", "content": settings.chat.system_prompt})
        return StreamingResponse(
            _chat_events(request_id, messages, num_ctx, req.seed, len(req.message)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Request-ID": request_id},
        )

    async def _chat_events(
        request_id: str, messages: list[dict[str, str]], num_ctx: int, seed: int | None, message_chars: int
    ) -> AsyncIterator[str]:
        t0 = time.perf_counter()
        base = {"request_id": request_id, "model": provider.model, "num_ctx": num_ctx, "message_chars": message_chars}
        yield sse("meta", base)
        t_first: float | None = None
        try:
            async with gate.slot():
                t_admitted = time.perf_counter()
                peak = _VramPeak(sampler.nvml)
                peak.start()
                final: dict[str, Any] = {}
                try:
                    async for chunk in provider.stream_chat(
                        messages, num_ctx=num_ctx, max_output_tokens=settings.ollama.max_output_tokens, seed=seed
                    ):
                        if chunk.final is not None:
                            final = chunk.final
                            break
                        if t_first is None:
                            t_first = time.perf_counter()
                        yield sse("token", {"t": chunk.text})
                finally:
                    vram_peak = await peak.stop()
                t_end = time.perf_counter()
            snapshot = await asyncio.to_thread(sampler.snapshot)
            ns = 1e-6
            timing = {
                "queue_wait_ms": round((t_admitted - t0) * 1000, 1),
                "ttft_ms": round((t_first - t0) * 1000, 1) if t_first else None,
                "total_ms": round((t_end - t0) * 1000, 1),
                "ollama_load_ms": round(final.get("load_duration", 0) * ns, 1),
                "ollama_prompt_eval_ms": round(final.get("prompt_eval_duration", 0) * ns, 1),
                "ollama_eval_ms": round(final.get("eval_duration", 0) * ns, 1),
            }
            eval_count, eval_ns = final.get("eval_count"), final.get("eval_duration")
            usage = {
                "prompt_tokens": final.get("prompt_eval_count"),
                "output_tokens": eval_count,
                "tokens_per_s": round(eval_count / (eval_ns / 1e9), 1) if eval_count and eval_ns else None,
                "done_reason": final.get("done_reason"),
            }
            summary = _resource_summary(snapshot, vram_peak)
            log_event("chat_request", **base, outcome="ok", timing=timing, usage=usage, resources=summary,
                      resource_snapshot=snapshot)
            yield sse("done", {**base, "timing": timing, "usage": usage, "resources": summary})
        except QueueFull as exc:
            log_event("chat_request", logging.WARNING, **base, outcome="queue_full")
            yield sse("error", {**base, "code": "queue_full", "message": str(exc)})
        except ProviderError as exc:
            log_event("chat_request", logging.WARNING, **base, outcome=exc.code, message=exc.message, detail=exc.detail,
                      elapsed_ms=round((time.perf_counter() - t0) * 1000, 1), partial=t_first is not None)
            yield sse("error", {**base, "code": exc.code, "message": exc.message, "partial": t_first is not None})
        except asyncio.CancelledError:
            log_event("chat_request", **base, outcome="cancelled", elapsed_ms=round((time.perf_counter() - t0) * 1000, 1))
            raise
        except Exception as exc:
            log_event("chat_request", logging.ERROR, **base, outcome="internal_error", detail=repr(exc))
            yield sse("error", {**base, "code": "internal_error",
                                "message": f"Unexpected backend error (request {request_id}); see logs/xodus.jsonl."})

    @app.get("/api/capabilities")
    async def list_capabilities() -> list[dict[str, Any]]:
        return registry.describe()

    @app.post("/api/capabilities/{cap_id}/invoke")
    async def invoke(cap_id: str, req: InvokeRequest) -> JSONResponse:
        t0 = time.perf_counter()
        try:
            result = await registry.invoke(cap_id, req.args)
        except CapabilityError as exc:
            log_event("capability_call", logging.WARNING, capability=cap_id, args=req.args, outcome=exc.code,
                      message=exc.message)
            return JSONResponse({"ok": False, "error": {"code": exc.code, "message": exc.message}}, exc.status)
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        log_event("capability_call", capability=cap_id, args=req.args, outcome="ok", duration_ms=duration_ms)
        return JSONResponse({"ok": True, "result": result, "duration_ms": duration_ms})

    if UI_DIR.is_dir():
        app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
    else:

        @app.get("/", response_class=HTMLResponse)
        async def ui_missing() -> str:
            return "<p>XODUS backend is running, but the UI is not built. See README.md (Build the UI).</p>"

    return app
