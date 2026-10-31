"""Backend side of ingestion: validate and queue requests, run and watch the supervised worker.

The worker is a core-tier process in its own Job Object. It is started when jobs are pending, restarted if it
dies, killed if its job heartbeat stalls, and stopped by the supervisor's idle reaper after
ingestion.worker_idle_timeout_s without work. A crash loop (too many starts per minute) stops restarts until the
next request, so a poison file cannot spin forever; the per-job attempt limit fails such a file after a few tries.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xodus.config.settings import REPO_ROOT, Settings
from xodus.ingestion.parsers import EXTENSIONS, MiB
from xodus.ingestion.pipeline import RootInfo
from xodus.ingestion.worker import SETTINGS_ENV
from xodus.obs import log_event
from xodus.security.audit import AuditLog
from xodus.security.paths import PathViolation, WrongKind, is_link, resolve_existing
from xodus.storage.knowledge import Conflict, KnowledgeStore
from xodus.supervisor.supervisor import ManagedProcess, Supervisor

MAX_DEPTH = 8


class IngestRequestError(Exception):
    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class Enqueued:
    jobs: list[dict[str, Any]]
    rejected: list[dict[str, str]]
    paused: bool = False  # queued, but ingestion is paused: nothing runs until it is resumed


class IngestionService:
    def __init__(self, settings: Settings, store: KnowledgeStore, supervisor: Supervisor, roots: dict[str, RootInfo],
                 audit: AuditLog) -> None:
        self.settings = settings
        self.store = store
        self.supervisor = supervisor
        self.roots = roots
        self.audit = audit
        self._lock = threading.Lock()
        self._worker: ManagedProcess | None = None
        self._starts: deque[float] = deque()
        self.degraded: str | None = None
        self.worker_starts_total = 0
        # Set by the resource governor (warn and stop levels). Kept in memory, apart from the user's pause, which is
        # stored: a restart drops a hold, and releasing a hold never resumes what the user paused.
        self.held: str | None = None

    # ------------------------------------------------------------------ requests

    def enqueue(self, collection: str, root_id: str, rel_path: str, recursive: bool = False) -> Enqueued:
        k = self.settings.knowledge
        coll = self.store.collection_by_name(collection)
        if coll is None:
            raise IngestRequestError("not_found", f"No collection named {collection!r}.", 404)
        root = self.roots.get(root_id)
        if root is None or "fs.read" not in root.grants:
            raise IngestRequestError("permission_denied", f"Folder {root_id!r} is not configured for reading.", 403)
        try:
            try:
                target, is_dir = resolve_existing(root.path, rel_path, "dir"), True
            except WrongKind:
                target, is_dir = resolve_existing(root.path, rel_path, "file"), False
        except PathViolation as exc:
            raise IngestRequestError(exc.code, exc.message, 404 if exc.code == "not_found" else 403) from exc

        rejected: list[dict[str, str]] = []
        candidates = self._enumerate(root.path, target, recursive, rejected) if is_dir else [target]
        if len(candidates) > k.max_files_per_request:
            raise IngestRequestError("too_many_files", f"{len(candidates)} files found; one request may queue at most "
                                                       f"{k.max_files_per_request}. Queue a smaller folder.", 413)
        jobs: list[dict[str, Any]] = []
        for path in candidates:
            rel = path.relative_to(root.path).as_posix()
            reason = self._precheck(path)
            if reason:
                rejected.append({"path": rel, "code": reason[0], "reason": reason[1]})
                continue
            try:  # the database allows one active job per file, so concurrent requests cannot both queue it
                jobs.append(self.store.enqueue_job(coll["id"], root_id, rel))
            except Conflict:
                rejected.append({"path": rel, "code": "already_queued", "reason": "This file is already queued or running."})
        self.audit.append("ingest_requested", collection=collection, root=root_id, path=rel_path, recursive=recursive,
                          queued=len(jobs), rejected=len(rejected))
        if jobs:
            self.degraded, self._starts = None, deque()  # a new request retries a stopped worker
            self.ensure_worker("ingest request")
        return Enqueued(jobs, rejected, self.store.ingestion_paused() or self.held is not None)

    def _precheck(self, path: Path) -> tuple[str, str] | None:
        ext = path.suffix.lower()
        if ext not in EXTENSIONS:
            return "unsupported_format", f"{ext or 'files without an extension'} is not supported."
        limit = self.settings.knowledge.max_file_mb * MiB
        try:
            size = path.stat().st_size
        except OSError as exc:
            return "unreadable", f"The file cannot be read ({type(exc).__name__})."
        if size > limit:
            return "file_too_large", f"{size / MiB:.1f} MB is over the {self.settings.knowledge.max_file_mb} MB limit."
        if size == 0:
            return "empty_file", "The file is empty."
        return None

    def _enumerate(self, root: Path, folder: Path, recursive: bool, rejected: list[dict[str, str]]) -> list[Path]:
        found: list[Path] = []
        limit = self.settings.knowledge.max_files_per_request + 1
        stack = [(folder, 0)]
        while stack and len(found) < limit:
            current, depth = stack.pop()
            try:
                entries = sorted(os.scandir(current), key=lambda e: e.name.lower())
            except OSError:
                continue
            for entry in entries:
                path = Path(entry.path)
                if is_link(path):  # links are listed as skipped, never followed
                    rejected.append({"path": path.relative_to(root).as_posix(), "code": "link",
                                     "reason": "Symbolic links and junctions are not followed."})
                elif entry.name.endswith(".xodus-tmp") or entry.name.startswith("~$"):
                    continue
                elif entry.is_dir(follow_symlinks=False):
                    if recursive and depth < MAX_DEPTH:
                        stack.append((path, depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    found.append(path)
                    if len(found) >= limit:
                        break
        return sorted(found)

    # ------------------------------------------------------------------ worker lifecycle

    def worker_command(self) -> list[str]:
        return [sys.executable, "-m", "xodus.ingestion.worker"]

    def worker_alive(self) -> bool:
        return self._worker is not None and self.supervisor.is_alive(self._worker)

    def pause(self, reason: str = "user") -> dict[str, Any]:
        """Stop background ingestion: the worker is stopped and its job goes back to the queue (resumes from its
        checkpoint later). Queued jobs stay queued. Survives a backend restart until resumed."""
        with self._lock:
            self.store.set_ingestion_paused(True)
            worker, self._worker = self._worker, None
        if worker is not None:
            self.supervisor.stop(worker, "paused")
        requeued = self.store.requeue_running()
        self.audit.append("ingestion_paused", reason=reason, requeued_jobs=requeued)
        log_event("ingestion_paused", reason=reason, requeued_jobs=requeued)
        return {"paused": True, "requeued_jobs": requeued, "pending_jobs": self.store.pending_jobs()}

    def resume(self, reason: str = "user") -> dict[str, Any]:
        self.store.set_ingestion_paused(False)
        self.degraded, self._starts = None, deque()
        started = self.ensure_worker("resumed") if self.store.pending_jobs() else False
        self.audit.append("ingestion_resumed", reason=reason, worker_started=started)
        log_event("ingestion_resumed", reason=reason, worker_started=started)
        return {"paused": False, "worker_started": started, "pending_jobs": self.store.pending_jobs()}

    def hold(self, reason: str) -> dict[str, Any]:
        """Resource governor: stop the worker at once; its job goes back to the queue and later resumes from its last
        checkpoint (a committed batch), exactly as after a user pause. Queued jobs wait."""
        with self._lock:
            already = self.held is not None
            self.held = reason
            worker, self._worker = self._worker, None
        if worker is not None:
            self.supervisor.stop(worker, "held: " + reason)
        requeued = self.store.requeue_running()
        if not already:
            self.audit.append("ingestion_held", reason=reason, requeued_jobs=requeued)
            log_event("ingestion_held", logging.WARNING, reason=reason, requeued_jobs=requeued)
        return {"held": reason, "worker_stopped": worker is not None, "requeued_jobs": requeued}

    def release(self, reason: str) -> dict[str, Any]:
        with self._lock:
            was, self.held = self.held, None
        started = self.ensure_worker("released after resource pressure") if was and self.store.pending_jobs() else False
        if was:
            self.audit.append("ingestion_released", reason=reason, worker_started=started)
            log_event("ingestion_released", reason=reason, worker_started=started)
        return {"released": was is not None, "worker_started": started}

    def ensure_worker(self, reason: str) -> bool:
        """Start the worker if none is running (and ingestion is neither paused nor held). True if one was started."""
        cfg = self.settings.ingestion
        with self._lock:  # pause() and hold() set their flags under this lock, so a worker cannot start after them
            if self.store.ingestion_paused() or self.held is not None:
                return False
            if self._worker is not None:
                if self.supervisor.is_alive(self._worker):
                    self.supervisor.touch(self._worker)
                    return False
                self.supervisor.stop(self._worker, "exited")
                self._worker = None
            now = time.monotonic()
            while self._starts and now - self._starts[0] > 60:
                self._starts.popleft()
            if len(self._starts) >= cfg.max_restarts_per_minute:
                self.degraded = (f"The ingestion worker was started {len(self._starts)} times in the last minute and kept "
                                 "stopping; not restarting it until the next ingest request. See logs/ingestion-worker.jsonl.")
                log_event("ingestion_degraded", logging.ERROR, message=self.degraded)
                return False
            limit = cfg.worker_memory_limit_mb
            self.settings.log_dir.mkdir(parents=True, exist_ok=True)
            env = {**os.environ, SETTINGS_ENV: self.settings.model_dump_json()}
            self._worker = self.supervisor.launch(
                self.worker_command(), role="ingestion", owner="ingestion", start_reason=reason, cwd=REPO_ROOT / "backend",
                env=env, tier="core", idle_timeout_s=cfg.worker_idle_timeout_s,
                memory_limit_bytes=limit * MiB if limit else None, pipes=False,
                stderr_path=self.settings.log_dir / "ingestion-worker.stderr.log")
            self._starts.append(now)
            self.worker_starts_total += 1
            return True

    def tick(self) -> None:
        """One monitoring pass: keep a worker running while jobs are pending, kill one whose heartbeat stalled."""
        if self.store.pending_jobs() == 0 or self.store.ingestion_paused() or self.held is not None:
            return
        if self.worker_alive():
            assert self._worker is not None
            self.supervisor.touch(self._worker)  # pending work: not idle
            stall = self.settings.ingestion.stall_timeout_s
            for job in self.store.running_jobs():
                if job["heartbeat_at"] and time.time() - job["heartbeat_at"] > stall:
                    log_event("ingestion_worker_stalled", logging.ERROR, job=job["id"], silent_s=round(time.time() - job["heartbeat_at"]))
                    self.supervisor.stop(self._worker, "stalled")
                    self._worker = None
                    break
        else:
            self.ensure_worker("pending jobs")

    def status(self) -> dict[str, Any]:
        worker = self._worker
        alive = self.worker_alive()
        return {
            "worker": {"running": alive, "pid": worker.pid if worker and alive else None,
                       "starts_total": self.worker_starts_total, "degraded": self.degraded,
                       "idle_timeout_s": self.settings.ingestion.worker_idle_timeout_s},
            "pending_jobs": self.store.pending_jobs(),
            "paused": self.store.ingestion_paused(),
            "held": self.held,  # the resource governor's reason, or None
            "embedding": {"model": self.settings.ollama.embed_model, "dimensions": self.settings.embedding.dimensions,
                          "num_gpu": self.settings.embedding.num_gpu, "batch_size": self.settings.embedding.batch_size},
        }
