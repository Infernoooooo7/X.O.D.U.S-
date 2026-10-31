"""HTTP routes for the Gate E views: conversations, model profiles, resources and processes, audit log, experiments,
settings and the extension inventory.

Read routes are paginated and bounded. State-changing routes (switch model, stop a process, delete a conversation)
are user actions behind the same-origin check in app.py, and each writes the audit log.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from xodus.capabilities.registry import CapabilityRegistry
from xodus.config.settings import REPO_ROOT, Settings, default_config_files
from xodus.ingestion.service import IngestionService
from xodus.providers.profiles import ModelManager, SwitchError
from xodus.resources.governor import ResourceGovernor
from xodus.resources.sampler import ResourceSampler, mib
from xodus.resources.thresholds import evaluate
from xodus.security.audit import AuditLog, read_recent
from xodus.security.paths import PathViolation, resolve_existing
from xodus.storage.conversations import ConversationStore
from xodus.supervisor.supervisor import Supervisor

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]
EXPERIMENTS_DIR = REPO_ROOT / "experiments"
TEXT_SUFFIXES = {".md", ".json", ".txt", ".csv", ".jsonl"}
MAX_TEXT_BYTES = 1_000_000
LINES = (
    ("xodus_app", "XODUS application (backend, ingestion worker)"),
    ("ollama", "Ollama server and runners"),
    ("supporting", "Supporting services (PostgreSQL)"),
    ("optional", "Optional helpers and plugins (MCP servers, plugin processes)"),
)


@dataclass
class WorkspaceContext:
    settings: Settings
    conversations: ConversationStore
    models: ModelManager
    sampler: ResourceSampler
    supervisor: Supervisor
    registry: CapabilityRegistry
    ingestion: IngestionService
    audit: AuditLog
    governor: ResourceGovernor | None = None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationCreate(_Strict):
    title: str = Field("New conversation", min_length=1, max_length=200)


class ConversationRename(_Strict):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class SwitchRequest(_Strict):
    profile: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]


def config_hash(settings: Settings) -> str:
    return hashlib.sha256(settings.model_dump_json().encode()).hexdigest()[:16]


def resource_view(snap: dict[str, Any], w: WorkspaceContext) -> dict[str, Any]:
    roles = {}
    for mp in w.supervisor.managed():
        for pid in mp.job.pids() or [mp.pid]:
            roles[pid] = {"role": mp.role, "owner": mp.owner, "supervised": True}
    processes = [{
        "pid": p["pid"], "name": p["name"], "group": p["group"], "tier": p["tier"],
        **roles.get(p["pid"], {"role": "backend" if p["group"] == "xodus_app" else p["group"], "owner": None, "supervised": False}),
        "uss_mib": mib(p["uss"]), "working_set_mib": mib(p["rss"]), "private_commit_mib": mib(p["private_commit"]),
        "gpu_dedicated_mib": mib(p["gpu_dedicated"]), "gpu_shared_mib": mib(p["gpu_shared"]), "errors": p["errors"],
    } for p in snap["processes"]]
    lines = []
    for key, label in LINES:
        members = [p for p in snap["processes"] if p["group"] == key]
        line = {"line": key, "label": label, "processes": len(members), "included": True,
                "uss_mib": mib(sum(p["uss"] or 0 for p in members)) if members else None,
                "working_set_mib": mib(sum(p["rss"] or 0 for p in members)) if members else None,
                "private_commit_mib": mib(sum(p["private_commit"] or 0 for p in members)) if members else None, "note": None}
        if key == "supporting":
            line["note"] = "None: the SQLite storage adapter runs inside the backend process (PostgreSQL was not installed)."
        elif not members:
            line["note"] = "Nothing running."
        lines.append(line)
    system = snap["system_memory"]
    lines.append({"line": "system", "label": "Total system physical memory in use (all applications)", "included": False,
                  "processes": None, "uss_mib": None, "working_set_mib": mib(system["in_use"]), "private_commit_mib": None,
                  "note": f"{mib(system['available'])} MiB available of {mib(system['total'])} MiB."})
    lines.append({"line": "browser", "label": "Browser tab", "included": False, "processes": None, "uss_mib": None,
                  "working_set_mib": None, "private_commit_mib": None,
                  "note": "Not measured here; excluded from every aggregate (external to the budget)."})
    vram = snap["device_vram"]
    return {
        "timestamp": snap["timestamp"],
        "totals": {"uss_mib": mib(snap["totals"]["uss"]), "working_set_mib": mib(snap["totals"]["rss"]),
                   "private_commit_mib": mib(snap["totals"]["private_commit"]), "uss_complete": snap["uss_complete"],
                   "included": "core and optional tiers", "excluded": snap["excluded"]},
        "lines": lines,
        "processes": processes,
        "device_vram": {k: mib(v) for k, v in vram.items()} if vram else None,
        "gpu_ollama": {"dedicated_mib": mib(snap["totals"]["gpu_dedicated_ollama"]),
                       "shared_mib": mib(snap["totals"]["gpu_shared_ollama"])},
        "gpu_xodus": {"dedicated_mib": mib(snap["totals"].get("gpu_dedicated_xodus")),
                      "shared_mib": mib(snap["totals"].get("gpu_shared_xodus"))},
        "measurement": {"nvml": snap["errors"]["nvml"] or "ok", "gpu_counters": snap["errors"]["gpu_counters"] or "ok",
                        "sample_ms": snap["sample_ms"]},
        "thresholds": evaluate(snap, w.settings.thresholds, w.governor.state() if w.governor else None),
        "ingestion": w.ingestion.status(),
        "supervised": w.supervisor.snapshot(),
        "limits": {"max_optional_processes": w.supervisor.max_optional,
                   "optional_idle_timeout_s": w.supervisor.idle_timeout_s,
                   "optional_memory_limit_mib": mib(w.supervisor.memory_limit_bytes)},
    }


def register_workspace_routes(app: FastAPI, w: WorkspaceContext) -> None:
    convs, audit = w.conversations, w.audit

    @app.exception_handler(SwitchError)
    async def switch_error(_: Request, exc: SwitchError) -> JSONResponse:
        return JSONResponse({"ok": False, "error": {"code": exc.code, "message": exc.message}, "record": exc.record}, exc.status)

    # ------------------------------------------------------------------ conversations

    @app.get("/api/conversations")
    async def list_conversations(limit: Limit = 50, offset: Offset = 0,
                                 q: Annotated[str | None, Query(max_length=100)] = None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(convs.list, limit, offset, q)

    @app.post("/api/conversations", status_code=201)
    async def create_conversation(req: ConversationCreate) -> dict[str, Any]:
        return await asyncio.to_thread(convs.create, req.title)

    @app.get("/api/conversations/{conversation_id}")
    async def get_conversation(conversation_id: int, limit: Annotated[int, Query(ge=1, le=500)] = 200,
                               offset: Offset = 0) -> dict[str, Any]:
        conversation = await asyncio.to_thread(convs.get, conversation_id)
        return {**conversation, "items": await asyncio.to_thread(convs.messages, conversation_id, limit, offset)}

    @app.patch("/api/conversations/{conversation_id}")
    async def rename_conversation(conversation_id: int, req: ConversationRename) -> dict[str, Any]:
        return await asyncio.to_thread(convs.rename, conversation_id, req.title)

    @app.delete("/api/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: int) -> dict[str, Any]:
        report = await asyncio.to_thread(convs.delete, conversation_id)
        audit.append("conversation_deleted", conversation_id=conversation_id, messages=report["messages"],
                     memory_items=report["memory_items"], verified_clean=report["messages_left"] == 0)
        return {"deleted": report}

    # ------------------------------------------------------------------ models

    @app.get("/api/models")
    async def models() -> dict[str, Any]:
        return await w.models.describe()

    @app.post("/api/models/switch")
    async def switch_model(req: SwitchRequest) -> dict[str, Any]:
        if w.governor is not None:
            w.governor.check("model_switch")
        record = await w.models.switch(req.profile)
        return {"ok": True, "switch": record, "active": w.models.active_id}

    # ------------------------------------------------------------------ resources and processes

    @app.get("/api/resources")
    async def resources() -> dict[str, Any]:
        # The governor samples every check_interval_s anyway; reuse a fresh snapshot instead of taking a second one.
        recent = w.governor.recent_snapshot(w.settings.thresholds.check_interval_s) if w.governor else None
        snap = recent or await asyncio.to_thread(w.sampler.snapshot)
        return await asyncio.to_thread(resource_view, snap, w)

    @app.post("/api/processes/{pid}/stop")
    async def stop_process(pid: int) -> dict[str, Any]:
        """Stop a process XODUS launched in the optional tier (its whole job). Core processes are managed by XODUS."""
        mp = next((m for m in w.supervisor.managed() if m.pid == pid), None)
        if mp is None:
            raise HTTPException(404, "XODUS did not launch a running process with that id; only those can be stopped here.")
        if mp.tier != "optional":
            raise HTTPException(409, "Core-tier processes are managed by XODUS itself. To stop the ingestion worker, pause "
                                     "ingestion.")
        await asyncio.to_thread(w.supervisor.stop, mp, "stopped by user")
        audit.append("process_stopped_by_user", pid=pid, role=mp.role, owner=mp.owner)
        return {"stopped": pid, "role": mp.role, "owner": mp.owner,
                "note": "It starts again on the next use of its capability."}

    # ------------------------------------------------------------------ audit log

    @app.get("/api/audit")
    async def audit_records(limit: Limit = 50, before: Annotated[int | None, Query(ge=1)] = None,
                            event: Annotated[str | None, Query(pattern=r"^[a-z_]{1,48}$")] = None) -> dict[str, Any]:
        records = await asyncio.to_thread(read_recent, audit.path, limit, before, event)
        return {"records": records, "head_seq": audit.seq,
                "next_before": records[-1]["seq"] if len(records) == limit else None}

    # ------------------------------------------------------------------ experiments (read-only)

    @app.get("/api/experiments")
    async def experiments() -> dict[str, Any]:
        def listing() -> dict[str, Any]:
            results = EXPERIMENTS_DIR / "results"
            runs = []
            if results.is_dir():
                for folder in sorted((p for p in results.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)[:200]:
                    files = [{"path": f.relative_to(EXPERIMENTS_DIR).as_posix(), "name": f.name, "bytes": f.stat().st_size,
                              "modified": f.stat().st_mtime, "viewable": f.suffix.lower() in TEXT_SUFFIXES}
                             for f in sorted(folder.iterdir()) if f.is_file()][:200]
                    runs.append({"name": folder.name, "files": files})
            prereg = EXPERIMENTS_DIR / "PREREGISTRATION.md"
            return {"runs": runs, "preregistration": "present" if prereg.is_file() else "not written yet (Gate G)",
                    "note": "Results are read from experiments/results/. Formal experiments are run in Gate G."}

        return await asyncio.to_thread(listing)

    @app.get("/api/experiments/file")
    async def experiment_file(path: Annotated[str, Query(min_length=1, max_length=260)]) -> dict[str, Any]:
        try:
            target = resolve_existing(EXPERIMENTS_DIR, path, "file")
        except PathViolation as exc:
            raise HTTPException(404 if exc.code == "not_found" else 403, exc.message) from exc
        if target.suffix.lower() not in TEXT_SUFFIXES:
            raise HTTPException(415, "Only text results (.md, .json, .jsonl, .txt, .csv) can be shown.")
        size = target.stat().st_size
        with open(target, "rb") as fh:
            raw = fh.read(MAX_TEXT_BYTES)
        return {"path": Path(path).as_posix(), "bytes": size, "truncated": size > MAX_TEXT_BYTES,
                "text": raw.decode("utf-8", errors="replace")}

    # ------------------------------------------------------------------ settings (read-only) and extensions

    @app.get("/api/settings")
    async def settings_view() -> dict[str, Any]:
        s = w.settings
        return {
            "config_files": [str(p) for p in default_config_files()],
            "config_hash": config_hash(s),
            "how_to_change": "Edit config/local.toml (or set XODUS_<SECTION>__<KEY> environment variables) and restart the "
                             "backend. The UI cannot change configuration: grants and roots are never set from a web page.",
            "server": {"host": s.server.host, "port": s.server.port},
            "roots": [{"id": r.id, "path": str(r.path), "grants": r.grants} for r in s.permissions.roots],
            "collection_grants": [{"id": c.id, "grants": c.grants} for c in s.permissions.collections],
            "global_grants": s.permissions.global_grants,
            "envelope": s.envelope.model_dump(), "executor": {"default_arm": s.executor.default_arm,
                                                             "max_active_tasks": s.executor.max_active_tasks},
            "approvals": s.approvals.model_dump(), "thresholds": s.thresholds.model_dump(),
            "embedding": s.embedding.model_dump(exclude={"query_template", "document_template"}),
            "knowledge_limits": {k: v for k, v in s.knowledge.model_dump().items() if k.startswith("max_")},
            "data_dir": str(s.data_dir), "log_dir": str(s.log_dir),
            "effective": json.loads(s.model_dump_json()),
        }

    @app.get("/api/extensions")
    async def extensions() -> dict[str, Any]:
        rows = [e.inventory() for e in w.registry.extensions]
        running = sum(1 for mp in w.supervisor.managed() if mp.tier == "optional")
        return {"extensions": rows,
                "rejected": [r for r in w.registry.describe_rejected() if r["source"] in ("plugin", "mcp_server")],
                "optional_running": running, "max_optional_processes": w.supervisor.max_optional,
                "idle_timeout_s": w.supervisor.idle_timeout_s, "memory_limit_mib": mib(w.supervisor.memory_limit_bytes),
                "plugin_dir": str(w.settings.plugins.dir),
                "note": "Extensions run as separate processes with your user rights. This is process isolation, not a "
                        "sandbox; install only extensions whose code you have read."}
