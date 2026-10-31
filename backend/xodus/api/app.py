"""HTTP API: health, streamed chat (SSE), capabilities, tasks (SSE events), approvals, processes,
audit verification, and the static UI. Bound to loopback; Host header allow-listed; state-changing
requests from another browser origin are refused.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from xodus import __version__
from xodus.api.knowledge import KnowledgeContext, register_knowledge_routes
from xodus.api.workspace import WorkspaceContext, register_workspace_routes
from xodus.capabilities.discovery import discover
from xodus.capabilities.kb_search import KnowledgeSearch
from xodus.capabilities.registry import CapabilityError, CapabilityRegistry, InvocationContext
from xodus.config.settings import REPO_ROOT, Settings
from xodus.ingestion.pipeline import RootInfo
from xodus.ingestion.service import IngestionService
from xodus.memory.manager import MemoryManager
from xodus.obs import log_event
from xodus.orchestrator.gate import GateContext, ProposedStep, StepGate
from xodus.orchestrator.runner import ExecutorDeps, invoke_checked, roots_for
from xodus.orchestrator.task import TaskLog
from xodus.orchestrator.tasks import TaskLimit, TaskManager
from xodus.providers import build_provider
from xodus.providers.base import ModelProvider, ProviderError, RequestBudget, estimate_tokens
from xodus.providers.ollama import remote_model_error, unload_embedder_sync
from xodus.providers.ollama_env import check_ollama_environment
from xodus.providers.profiles import ModelManager, SwitchLog
from xodus.resources.admission import AdmissionGate, QueueFull
from xodus.resources.governor import ResourceGovernor, ResourcePressure
from xodus.resources.sampler import Nvml, ResourceSampler, mib
from xodus.retrieval.answer import AnswerService
from xodus.retrieval.hybrid import Retriever
from xodus.security.approvals import ApprovalError, ApprovalStore
from xodus.security.audit import AuditLog, verify
from xodus.security.policy import PermissionPolicy, Root
from xodus.security.token import HEADER as TOKEN_HEADER
from xodus.security.token import cookie_name, load_or_create, matches
from xodus.storage.conversations import ConversationStore
from xodus.storage.knowledge import KnowledgeStore
from xodus.storage.memory import MemoryStore
from xodus.storage.sqlite import Database
from xodus.supervisor.supervisor import Supervisor

UI_DIR = REPO_ROOT / "frontend" / "out"
GATE_STATUS = {"unknown_capability": 404, "not_found": 404, "not_offered": 403, "permission_denied": 403,
               "path_violation": 403, "unavailable": 409, "invalid_arguments": 422}


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    num_ctx: int | None = None
    seed: int | None = None
    conversation_id: int | None = Field(None, ge=1)  # save both turns and send earlier turns as history


class InvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    args: dict[str, Any] = {}


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1)
    arm: str | None = None
    conversation_id: int | None = Field(None, ge=1)  # record the goal and, once the task ends, its outcome


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str
    action_hash: str = Field(min_length=1, max_length=128)


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


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
        "uss_optional_mib": mib(snap["totals"]["uss_optional"]),
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
            {"pid": p["pid"], "name": p["name"], "group": p["group"], "tier": p["tier"], "uss_mib": mib(p["uss"]),
             "rss_mib": mib(p["rss"]), "gpu_dedicated_mib": mib(p["gpu_dedicated"])}
            for p in snap["processes"]
        ],
        "sample_ms": snap["sample_ms"],
        "excluded": snap["excluded"],
    }


class UIStaticFiles(StaticFiles):
    """The exported UI. Next.js 16 prefetches route segments as "<view>/__next.<view>.__PAGE__.txt" but its static export
    writes them as "<view>/__next.<view>/__PAGE__.txt"; when the first is missing, serve the second. The mapped path goes
    through the same lookup (and the same confinement to the UI folder) as any other request."""

    _SEGMENT = re.compile(r"^__next\.([A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*)\.txt$")

    def lookup_path(self, path: str) -> tuple[str, Any]:
        full, stat = super().lookup_path(path)
        if stat is None:
            head, _, name = path.replace("\\", "/").rpartition("/")
            match = self._SEGMENT.match(name)
            if match:
                first, *rest = match.group(1).split(".")
                if rest:
                    alt = "/".join(p for p in (head, f"__next.{first}", *rest[:-1], f"{rest[-1]}.txt") if p)
                    return super().lookup_path(alt)
        return full, stat


def build_policy(settings: Settings) -> PermissionPolicy:
    roots = [Root(r.id, r.path, frozenset(r.grants)) for r in settings.permissions.roots]
    collections = {c.id: c.grants for c in settings.permissions.collections}
    return PermissionPolicy(roots, settings.permissions.global_grants, collections)


def create_app(
    settings: Settings,
    provider: ModelProvider | None = None,
    sampler: ResourceSampler | None = None,
    supervisor: Supervisor | None = None,
    entry_points: Callable[..., Any] | None = None,
) -> FastAPI:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(settings.data_dir / "audit.jsonl")
    policy = build_policy(settings)
    limit_mb = settings.supervisor.optional_memory_limit_mb
    supervisor = supervisor or Supervisor(
        max_optional=settings.supervisor.max_optional_processes,
        idle_timeout_s=settings.supervisor.optional_idle_timeout_s,
        memory_limit_bytes=limit_mb * 2**20 if limit_mb else None,
        audit=audit,
    )
    provider = provider or build_provider(settings, on_event=lambda event, **fields: audit.append(event, **fields))

    # Storage. Opening runs the migrations and refuses a database whose vector size disagrees with the config.
    db = Database(settings.data_dir / settings.storage.filename, embedding_dim=settings.embedding.dimensions,
                  cache_kib=settings.storage.cache_kib, busy_timeout_ms=settings.storage.busy_timeout_ms,
                  pool_size=settings.storage.pool_size)
    applied_migrations = db.open()
    store = KnowledgeStore(db, max_chunks=settings.knowledge.max_chunks)
    memory = MemoryManager(MemoryStore(db), audit)

    async def embed_query(text: str) -> list[float]:
        return (await provider.embed([text]))[0]

    retriever = Retriever(store, settings.knowledge, settings.embedding, embed_query)

    registry = CapabilityRegistry(policy, settings.capabilities.disabled, provider.confirmed_capabilities)
    discover(registry, settings=settings, policy=policy, supervisor=supervisor,
             **({"entry_points": entry_points} if entry_points is not None else {}))
    readable = policy.readable_collections()
    if readable:  # a model can search only the collections the configuration grants it (deny by default)
        registry.register(KnowledgeSearch(retriever, store, readable), "builtin")
    gate = StepGate(registry, policy, settings.data_dir)
    approvals = ApprovalStore(audit, ttl_s=settings.approvals.ttl_s)
    admission = AdmissionGate(settings.envelope.max_running, settings.envelope.max_queued)
    deps = ExecutorDeps(provider=provider, registry=registry, gate=gate, approvals=approvals, audit=audit,
                        admission=admission, policy=policy, seed=settings.executor.seed)
    tasks = TaskManager(deps, settings, TaskLog(settings.log_dir / "tasks.jsonl"))
    ingestion = IngestionService(settings, store, supervisor,
                                 {r.id: RootInfo(r.path, frozenset(r.grants)) for r in settings.permissions.roots}, audit)
    conversations = ConversationStore(db)
    knowledge = KnowledgeContext(settings, store, memory, ingestion, retriever,
                                 AnswerService(retriever, provider, admission, settings), audit, conversations)
    embedding_status: dict[str, Any] = {"status": "unchecked", "model": settings.ollama.embed_model,
                                        "configured_dimensions": settings.embedding.dimensions,
                                        "num_gpu": settings.embedding.num_gpu}
    ollama_port = urlparse(settings.ollama.base_url).port or 11434
    sampler = sampler or ResourceSampler(ollama_port=ollama_port, supervisor=supervisor)

    def generation_busy() -> str | None:
        active = [t for t in tasks.list() if not t.terminal]
        if active:
            return f"{len(active)} task(s) are still running; let them finish or cancel them before switching models."
        if admission.running or admission.waiting:
            return "A reply is being generated; wait for it to finish before switching models."
        return None

    models = ModelManager(settings, provider, admission, sampler.nvml, SwitchLog(db), audit, generation_busy)

    async def embedder_loaded() -> bool:
        try:
            return any((m.get("name") or m.get("model")) == settings.ollama.embed_model for m in await provider.running_models())
        except ProviderError:
            return False

    governor = ResourceGovernor(settings.thresholds, sampler.snapshot, ingestion, supervisor, audit, embedder_loaded,
                                unload_embedder=lambda: unload_embedder_sync(settings.ollama))
    knowledge.governor = governor
    recorders: set[asyncio.Task[None]] = set()  # write each task's outcome into its conversation
    started_at = time.time()
    origins = {f"http://127.0.0.1:{settings.server.port}", f"http://localhost:{settings.server.port}"}
    api_token = load_or_create(settings.data_dir)
    token_cookie = cookie_name(settings.server.port)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            info = (await provider.probe()).public()
        except ProviderError as exc:
            info = {"error": exc.code, "message": exc.message}
        await _probe_embedder()
        try:  # never two generation models resident: unload one an earlier run left behind
            leftover = await models.reconcile()
        except ProviderError:
            leftover = []
        log_event(
            "startup",
            version=__version__,
            bind=f"{settings.server.host}:{settings.server.port}",
            ollama=await provider.health(),
            ollama_environment=await asyncio.to_thread(check_ollama_environment, ollama_port),
            model_probe=info,
            measurement={"nvml": sampler.nvml.error or "ok", "gpu_counters": sampler.gpu.error or "ok"},
            capabilities=registry.available_ids(),
            rejected=registry.describe_rejected(),
            audit_head={"seq": audit.seq, "hash": audit.head},
            storage={"database": str(db.path), "migrations_applied": applied_migrations, "embedding": embedding_status},
            ui_built=UI_DIR.is_dir(),
            models={"active": models.active_id, "profiles": sorted(models.profiles), "unloaded_leftovers": leftover},
            extensions=[e.inventory()["id"] for e in registry.extensions],
        )
        reaper = asyncio.create_task(supervisor.run_reaper(settings.supervisor.reaper_interval_s))
        monitor = asyncio.create_task(_ingestion_monitor())
        background_tasks = [monitor, reaper]
        if governor.enabled:
            background_tasks.append(asyncio.create_task(governor.run()))
        app.state.deps = deps
        app.state.tasks = tasks
        app.state.supervisor = supervisor
        yield
        await tasks.shutdown()
        if recorders:  # cancelled tasks have ended; let their outcomes reach the database before it closes
            await asyncio.wait(set(recorders), timeout=5)
        for background in background_tasks:
            background.cancel()
            with suppress(asyncio.CancelledError):
                await background
        await asyncio.to_thread(supervisor.stop_all, "shutdown")  # includes the ingestion worker
        await provider.aclose()
        sampler.close()
        db.close()
        log_event("shutdown")

    async def _ingestion_monitor() -> None:
        while True:
            await asyncio.sleep(settings.ingestion.monitor_interval_s)
            try:
                await asyncio.to_thread(ingestion.tick)
            except Exception as exc:  # the monitor must never die
                log_event("ingestion_monitor_error", logging.ERROR, detail=repr(exc))

    async def _probe_embedder() -> None:
        """Does the embedding model exist, and are its vectors at least as large as embedding.dimensions?
        Uses /api/show, which does not load the model into memory."""
        try:
            info = await provider.model_info(settings.ollama.embed_model)
        except ProviderError as exc:
            embedding_status.update(status="unavailable", code=exc.code, message=exc.message)
            return
        native = info.embedding_length
        if info.remote:
            embedding_status.update(status="remote_model", message=remote_model_error(info.name, info.remote_host).message)
        elif native is None:
            embedding_status.update(status="unverified", message="The provider did not report the embedding size.")
        elif native < settings.embedding.dimensions:
            embedding_status.update(status="dimension_mismatch", native_dimensions=native,
                                    message=f"The model produces {native}-dimensional vectors but embedding.dimensions is "
                                            f"{settings.embedding.dimensions}.")
        else:
            embedding_status.update(status="ok", native_dimensions=native, digest=info.digest)

    app = FastAPI(title="XODUS", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def same_origin_and_token(request: Request, call_next: Callable[..., Any]) -> Any:
        """Refuse state-changing requests that a browser sends from another origin (CSRF, local web pages), and any /api
        request without the per-install token (header for scripts, cookie for the UI). UI pages set the cookie."""
        path = request.url.path
        is_api = path == "/api" or path.startswith("/api/")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            site = request.headers.get("sec-fetch-site")
            if (origin is not None and origin not in origins) or (site is not None and site not in ("same-origin", "none")):
                log_event("cross_origin_refused", logging.WARNING, path=path, origin=origin, site=site)
                return JSONResponse({"detail": "Cross-origin request refused."}, status_code=403)
        if is_api and not (matches(request.headers.get(TOKEN_HEADER), api_token)
                           or matches(request.cookies.get(token_cookie), api_token)):
            log_event("api_token_refused", logging.WARNING, path=path, method=request.method)
            return JSONResponse({"ok": False, "error": {"code": "unauthorized", "message": (
                f"Missing or wrong XODUS API token. Open the UI at http://127.0.0.1:{settings.server.port}/ or send the "
                f"{TOKEN_HEADER} header with the token from {settings.data_dir / 'api_token'}.")}}, status_code=401)
        response = await call_next(request)
        if not is_api and request.method == "GET":
            response.set_cookie(token_cookie, api_token, httponly=True, samesite="strict", path="/")
        return response

    @app.exception_handler(sqlite3.OperationalError)
    async def database_error(_: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        """Found by failure injection F2: a database locked by another program answered a bare HTTP 500."""
        busy = any(word in str(exc).lower() for word in ("locked", "busy"))
        log_event("database_error", logging.ERROR, busy=busy, detail=str(exc))
        if busy:
            return JSONResponse({"ok": False, "error": {"code": "database_busy", "message": (
                f"The database stayed locked for {settings.storage.busy_timeout_ms / 1000:.0f} s, probably by another program "
                f"that has {settings.storage.filename} open. This request was not saved; try again in a moment.")}},
                503, headers={"Retry-After": "5"})
        return JSONResponse({"ok": False, "error": {"code": "database_error", "message": (
            "The database reported an error and this request was not saved. See logs/xodus.jsonl.")}}, 500)

    @app.exception_handler(ResourcePressure)
    async def resource_pressure(_: Request, exc: ResourcePressure) -> JSONResponse:
        log_event("refused_under_resource_pressure", logging.WARNING, message=exc.message)
        return JSONResponse({"ok": False, "error": {"code": exc.code, "message": exc.message}}, 503,
                            headers={"Retry-After": str(max(1, math.ceil(exc.retry_after_s)))})

    # Requests must name the loopback host we bind to; blocks DNS-rebinding style Host headers.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    # ------------------------------------------------------------------ health

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        confirmed = provider.confirmed_capabilities()
        return {
            "backend": {"status": "ok", "version": __version__, "uptime_s": round(time.time() - started_at, 1)},
            "ollama": await provider.health(),
            "ollama_environment": await asyncio.to_thread(check_ollama_environment, ollama_port),
            "model_capabilities": {"declared": sorted(settings.ollama.capabilities),
                                   "confirmed": sorted(confirmed) if confirmed is not None else None},
            "envelope": {"max_running": admission.max_running, "max_queued": admission.max_queued,
                         "running": admission.running, "queued": admission.waiting},
            "measurement": {"nvml": sampler.nvml.error or "ok", "gpu_counters": sampler.gpu.error or "ok"},
            "resources": governor.state(),
            "models": {"active": models.active_id, "switching": models.switching, "num_ctx": settings.ollama.num_ctx},
            "knowledge": {"embedding": embedding_status, "ingestion": ingestion.status(),
                          "store": (lambda s: {k: s[k] for k in ("schema_version", "collections", "documents", "chunks", "jobs",
                                                                 "memory_items")})(await asyncio.to_thread(store.stats))},
        }

    # ------------------------------------------------------------------ chat (Gate B slice)

    @app.post("/api/chat")
    async def chat(req: ChatRequest) -> StreamingResponse:
        if len(req.message) > settings.chat.max_message_chars:
            raise HTTPException(422, f"Message is longer than {settings.chat.max_message_chars} characters.")
        num_ctx = req.num_ctx or settings.ollama.num_ctx
        if num_ctx not in settings.ollama.allowed_num_ctx:
            raise HTTPException(422, f"num_ctx must be one of {settings.ollama.allowed_num_ctx}.")
        request_id = uuid.uuid4().hex[:12]
        system = [{"role": "system", "content": settings.chat.system_prompt}] if settings.chat.system_prompt else []
        user = {"role": "user", "content": req.message}
        history: list[dict[str, str]] = []
        if req.conversation_id is not None:
            # Earlier turns fill what is left of the context after the system prompt, this message and the answer
            # (resource rule 6: history is truncated, oldest first, never unbounded).
            room = num_ctx - settings.ollama.max_output_tokens - estimate_tokens([*system, user]) - 64
            history = await asyncio.to_thread(conversations.history, req.conversation_id,
                                              max(0, min(settings.chat.max_history_tokens, room)))
            await asyncio.to_thread(conversations.add_message, req.conversation_id, role="user", mode="chat",
                                    content=req.message, request_id=request_id)
        messages = [*system, *history, user]
        budget = RequestBudget(num_ctx=num_ctx, max_output_tokens=settings.ollama.max_output_tokens,
                               timeout_s=settings.ollama.request_timeout_s)
        return StreamingResponse(
            _chat_events(request_id, messages, budget, req.seed, len(req.message), req.conversation_id, len(history)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Request-ID": request_id},
        )

    def save_reply(conversation_id: int | None, mode: str, content: str, status: str, **fields: Any) -> int | None:
        """Write an assistant turn; a failure to save is logged and never breaks the reply already shown."""
        if conversation_id is None:
            return None
        try:
            return conversations.add_message(conversation_id, role="assistant", mode=mode, content=content, status=status,
                                             model=provider.model, **fields)["id"]
        except Exception as exc:
            log_event("conversation_save_failed", logging.ERROR, conversation_id=conversation_id, detail=repr(exc))
            return None

    async def _chat_events(request_id: str, messages: list[dict[str, Any]], budget: RequestBudget, seed: int | None,
                           message_chars: int, conversation_id: int | None, history_turns: int) -> AsyncIterator[str]:
        t0 = time.perf_counter()
        base = {"request_id": request_id, "model": provider.model, "profile": models.active_id, "num_ctx": budget.num_ctx,
                "message_chars": message_chars, "conversation_id": conversation_id, "history_turns": history_turns}
        yield sse("meta", base)
        t_first: float | None = None
        parts: list[str] = []
        saved = False
        try:
            async with admission.slot():
                t_admitted = time.perf_counter()
                peak = _VramPeak(sampler.nvml)
                peak.start()
                final: dict[str, Any] = {}
                try:
                    async for chunk in provider.stream(messages, budget, seed=seed):
                        if chunk.final is not None:
                            final = chunk.final
                            break
                        if t_first is None:
                            t_first = time.perf_counter()
                        parts.append(chunk.text)
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
            saved = True
            message_id = await asyncio.to_thread(save_reply, conversation_id, "chat", "".join(parts), "complete",
                                                 request_id=request_id, meta={"timing": timing, "usage": usage,
                                                                              "num_ctx": budget.num_ctx, "profile": models.active_id})
            yield sse("done", {**base, "timing": timing, "usage": usage, "resources": summary, "message_id": message_id})
        except QueueFull as exc:
            log_event("chat_request", logging.WARNING, **base, outcome="queue_full")
            saved = True
            await asyncio.to_thread(save_reply, conversation_id, "chat", "", "error", request_id=request_id,
                                    meta={"code": "queue_full", "message": str(exc)})
            yield sse("error", {**base, "code": "queue_full", "message": str(exc)})
        except ProviderError as exc:
            log_event("chat_request", logging.WARNING, **base, outcome=exc.code, message=exc.message, detail=exc.detail,
                      elapsed_ms=round((time.perf_counter() - t0) * 1000, 1), partial=t_first is not None)
            saved = True
            await asyncio.to_thread(save_reply, conversation_id, "chat", "".join(parts), "partial" if parts else "error",
                                    request_id=request_id, meta={"code": exc.code, "message": exc.message})
            yield sse("error", {**base, "code": exc.code, "message": exc.message, "partial": t_first is not None})
        except asyncio.CancelledError:
            log_event("chat_request", **base, outcome="cancelled", elapsed_ms=round((time.perf_counter() - t0) * 1000, 1))
            if not saved:  # the client went away (Stop); keep what was shown, marked as cancelled. Synchronous: no awaits here.
                save_reply(conversation_id, "chat", "".join(parts), "cancelled", request_id=request_id,
                           meta={"code": "cancelled", "message": "Stopped before the model finished."})
            raise
        except Exception as exc:
            log_event("chat_request", logging.ERROR, **base, outcome="internal_error", detail=repr(exc))
            if not saved:
                await asyncio.to_thread(save_reply, conversation_id, "chat", "".join(parts), "partial" if parts else "error",
                                        request_id=request_id, meta={"code": "internal_error"})
            yield sse("error", {**base, "code": "internal_error",
                                "message": f"Unexpected backend error (request {request_id}); see logs/xodus.jsonl."})

    # ------------------------------------------------------------------ capabilities

    @app.get("/api/capabilities")
    async def list_capabilities() -> list[dict[str, Any]]:
        return registry.describe()

    @app.get("/api/capabilities/rejected")
    async def rejected_capabilities() -> list[dict[str, Any]]:
        return registry.describe_rejected()

    @app.post("/api/capabilities/{cap_id}/invoke")
    async def invoke(cap_id: str, req: InvokeRequest) -> JSONResponse:
        """Direct, user-initiated call of a read-only capability. Goes through the same StepGate as tasks."""
        t0 = time.perf_counter()
        decision = gate.evaluate(ProposedStep(0, cap_id, req.args, "user"),
                                 GateContext(task_id="direct", allowed=frozenset(registry.ids())))
        audit.append("capability_requested", task_id=None, capability=cap_id, args=req.args, origin="user",
                     verdict=decision.verdict, code=decision.code)

        def refuse(code: str, message: str, status: int) -> JSONResponse:
            audit.append("capability_blocked", task_id=None, capability=cap_id, code=code, message=message)
            log_event("capability_call", logging.WARNING, capability=cap_id, args=req.args, outcome=code, message=message)
            return JSONResponse({"ok": False, "error": {"code": code, "message": message}}, status)

        if decision.verdict == "deny":
            return refuse(decision.code, decision.message, GATE_STATUS.get(decision.code, 400))
        cap = decision.capability
        assert cap is not None and decision.args is not None
        if decision.verdict == "approve" or cap.manifest.side_effects != "none":
            return refuse("approval_required", "This capability changes things; run it as a task so it can be approved.", 409)
        if cap.manifest.resource_class == "heavy":
            governor.check("capability")
        ctx = InvocationContext(task_id=None, step_index=0, roots=roots_for(cap.manifest, policy))
        try:
            result, size = await invoke_checked(cap, decision.args, ctx, cap.manifest.timeout_s)
        except CapabilityError as exc:
            return refuse(exc.code, exc.message, exc.status)
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        audit.append("capability_result", task_id=None, capability=cap_id, bytes=size, duration_ms=duration_ms)
        log_event("capability_call", capability=cap_id, args=req.args, outcome="ok", duration_ms=duration_ms)
        return JSONResponse({"ok": True, "result": result, "duration_ms": duration_ms})

    # ------------------------------------------------------------------ tasks

    @app.post("/api/tasks", status_code=202)
    async def create_task(req: TaskRequest) -> dict[str, Any]:
        if len(req.goal) > settings.chat.max_message_chars:
            raise HTTPException(422, f"Goal is longer than {settings.chat.max_message_chars} characters.")
        if req.conversation_id is not None:
            await asyncio.to_thread(conversations.get, req.conversation_id)  # 404 before anything starts
        governor.check("tasks")
        try:
            task = tasks.create(req.goal, req.arm)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except TaskLimit as exc:
            raise HTTPException(429, str(exc)) from exc
        if req.conversation_id is not None:
            await asyncio.to_thread(conversations.add_message, req.conversation_id, role="user", mode="task",
                                    content=req.goal, task_id=task.id, meta={"arm": task.arm})
            recorder = asyncio.create_task(_record_task(task, req.conversation_id), name=f"xodus-record-{task.id}")
            recorders.add(recorder)
            recorder.add_done_callback(recorders.discard)
        return {"task_id": task.id, "arm": task.arm, "events": f"/api/tasks/{task.id}/events",
                "conversation_id": req.conversation_id}

    async def _record_task(task: Any, conversation_id: int) -> None:
        """Wait for the task's final event, then write its outcome as the assistant turn: 'complete' only if the
        backend finished it as completed."""
        async for _ in task.follow():
            pass
        status = {"completed": "complete", "cancelled": "cancelled"}.get(task.status, "error")
        content = task.answer or task.message or f"The task ended with status {task.status}."
        await asyncio.to_thread(save_reply, conversation_id, "task", content, status, task_id=task.id,
                                meta={"task_status": task.status, "arm": task.arm, "steps": len(task.steps),
                                      "tainted": task.tainted})

    @app.get("/api/tasks")
    async def list_tasks() -> list[dict[str, Any]]:
        return [t.public() for t in tasks.list()]

    def _task(task_id: str) -> Any:
        task = tasks.get(task_id)
        if task is None:
            raise HTTPException(404, "No task with that id.")
        return task

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        return _task(task_id).public()

    @app.get("/api/tasks/{task_id}/events")
    async def task_events(task_id: str, after: int = 0) -> StreamingResponse:
        task = _task(task_id)

        async def stream() -> AsyncIterator[str]:
            async for event in task.follow(after):
                yield sse(event["type"], event)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.post("/api/tasks/{task_id}/cancel", status_code=202)
    async def cancel_task(task_id: str) -> dict[str, Any]:
        task = _task(task_id)
        if task.terminal:
            raise HTTPException(409, f"Task already finished ({task.status}).")
        await tasks.cancel(task)
        return {"task_id": task.id, "status": task.status}

    # ------------------------------------------------------------------ approvals

    @app.get("/api/approvals")
    async def list_approvals(status: str | None = "pending") -> list[dict[str, Any]]:
        return [r.public() for r in approvals.list(None if status == "all" else status)]

    @app.post("/api/approvals/{approval_id}")
    async def decide(approval_id: str, body: ApprovalDecision) -> JSONResponse:
        try:
            req = approvals.resolve(approval_id, body.decision, body.action_hash)
        except ApprovalError as exc:
            return JSONResponse({"ok": False, "error": {"code": exc.code, "message": exc.message}}, exc.status)
        return JSONResponse({"ok": True, "approval": req.public()})

    # ------------------------------------------------------------------ processes and audit

    @app.get("/api/processes")
    async def processes() -> dict[str, Any]:
        snap = await asyncio.to_thread(sampler.snapshot)
        return {"supervised": supervisor.snapshot(), "processes": _resource_summary(snap, None)["processes"],
                "excluded": snap["excluded"]}

    @app.get("/api/audit/verify")
    async def audit_verify() -> dict[str, Any]:
        result = await asyncio.to_thread(verify, audit.path)
        return {"ok": result.ok, "records": result.records, "first_bad_seq": result.first_bad_seq, "reason": result.reason}

    register_knowledge_routes(app, knowledge)
    register_workspace_routes(app, WorkspaceContext(settings, conversations, models, sampler, supervisor, registry,
                                                    ingestion, audit, governor))

    if UI_DIR.is_dir():
        app.mount("/", UIStaticFiles(directory=UI_DIR, html=True), name="ui")
    else:

        @app.get("/", response_class=HTMLResponse)
        async def ui_missing() -> str:
            return "<p>XODUS backend is running, but the UI is not built. See README.md (Build the UI).</p>"

    app.state.registry = registry
    app.state.api_token = api_token
    app.state.governor = governor
    app.state.audit = audit
    app.state.approvals = approvals
    app.state.knowledge = knowledge
    app.state.db = db
    app.state.models = models
    app.state.conversations = conversations
    app.state.settings = settings
    return app
