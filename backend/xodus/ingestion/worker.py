"""The ingestion worker process: `python -m xodus.ingestion.worker`.

Started and supervised by the backend (core tier, own Job Object). It processes one job at a time from the
database queue, renews its lease, and ends itself if it stops making progress for watchdog_s seconds (a hung
parser). The backend stops it after worker_idle_timeout_s without work. It never talks to the browser; its
only side effects are writes to the database and calls to the local Ollama embedder.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

import psutil

from xodus.config.settings import ConfigError, Settings, load_settings
from xodus.ingestion.pipeline import Cancelled, Embedder, IngestError, PipelineDeps, Progress, RootInfo, process_job
from xodus.obs import configure_logging, log_event
from xodus.providers.ollama import SyncOllamaEmbedder
from xodus.storage.knowledge import KnowledgeStore, LeaseLost
from xodus.storage.sqlite import Database

SETTINGS_ENV = "XODUS_WORKER_SETTINGS"
WATCHDOG_EXIT_CODE = 75


def owner_id() -> str:
    proc = psutil.Process()
    return f"{proc.pid}:{proc.create_time():.3f}"


def owner_alive(owner: str) -> bool:
    try:
        pid, created = owner.split(":", 1)
        proc = psutil.Process(int(pid))
        return abs(proc.create_time() - float(created)) < 0.05 and proc.is_running()
    except (ValueError, psutil.Error):
        return False


def settings_from_environment() -> Settings:
    """The backend passes its validated settings as JSON; running by hand falls back to the config files."""
    raw = os.environ.get(SETTINGS_ENV)
    return Settings.model_validate_json(raw) if raw else load_settings()


def start_watchdog(progress: Progress, limit_s: float) -> threading.Thread:
    def watch() -> None:
        while True:
            time.sleep(1.0)
            if time.monotonic() - progress.last > limit_s:
                log_event("worker_watchdog_exit", logging.ERROR, idle_s=round(time.monotonic() - progress.last, 1))
                os._exit(WATCHDOG_EXIT_CODE)  # no cleanup: the job returns to the queue and resumes from its checkpoint

    thread = threading.Thread(target=watch, name="watchdog", daemon=True)
    thread.start()
    return thread


def run_one(deps: PipelineDeps, job: dict) -> None:
    store = deps.store
    started = time.monotonic()
    try:
        result = process_job(deps, job)
    except Cancelled:
        store.finish_job(job["id"], deps.owner, "cancelled")
        log_event("ingest_job", job=job["id"], status="cancelled")
        return
    except IngestError as exc:
        store.finish_job(job["id"], deps.owner, "failed", error_code=exc.code, error=exc.message)
        log_event("ingest_job", logging.WARNING, job=job["id"], status="failed", code=exc.code, message=exc.message)
        return
    except LeaseLost as exc:
        log_event("ingest_job", logging.WARNING, job=job["id"], status="lease_lost", message=exc.message)
        return
    except Exception as exc:
        store.finish_job(job["id"], deps.owner, "failed", error_code="internal_error",
                         error=f"Unexpected error ({type(exc).__name__}); see logs/ingestion-worker.jsonl.")
        log_event("ingest_job", logging.ERROR, job=job["id"], status="failed", code="internal_error", detail=repr(exc))
        return
    store.finish_job(job["id"], deps.owner, "completed", outcome=result["outcome"], document_id=result["document_id"],
                     chunks_done=result["chunks"])
    log_event("ingest_job", job=job["id"], status="completed", outcome=result["outcome"], chunks=result["chunks"],
              seconds=round(time.monotonic() - started, 2))


def serve(settings: Settings, embedder: Embedder, *, should_stop=lambda: False) -> None:  # noqa: ANN001
    cfg = settings.ingestion
    db = Database(settings.data_dir / settings.storage.filename, embedding_dim=settings.embedding.dimensions,
                  cache_kib=settings.storage.cache_kib, busy_timeout_ms=settings.storage.busy_timeout_ms, pool_size=2)
    db.open()
    store = KnowledgeStore(db, max_chunks=settings.knowledge.max_chunks)
    owner = owner_id()
    progress = Progress()
    start_watchdog(progress, cfg.watchdog_s)
    roots = {r.id: RootInfo(r.path, frozenset(r.grants)) for r in settings.permissions.roots}
    deps = PipelineDeps(settings=settings, store=store, embedder=embedder, roots=roots, owner=owner, progress=progress)
    log_event("worker_started", pid=os.getpid(), owner=owner, embed_model=settings.ollama.embed_model,
              embed_num_gpu=settings.embedding.num_gpu)
    last_sweep = 0.0
    while not should_stop():
        progress.touch()
        if time.monotonic() - last_sweep > 30:
            outcome = store.requeue_orphans(owner_alive, cfg.max_attempts)
            if outcome["requeued"] or outcome["failed"]:
                log_event("orphaned_jobs", **outcome)
            last_sweep = time.monotonic()
        job = store.claim_next_job(owner)
        if job is None:
            time.sleep(cfg.poll_interval_s)
            continue
        run_one(deps, job)
    db.close()


def main() -> int:
    try:
        settings = settings_from_environment()
    except ConfigError as exc:
        print(f"XODUS worker configuration error:\n{exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_dir, "ingestion-worker.jsonl", to_stdout=False)
    try:
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)  # background work must not starve the chat
    except (psutil.Error, AttributeError):
        pass
    embedder = SyncOllamaEmbedder(settings.ollama, settings.embedding)
    try:
        serve(settings, embedder)
    except KeyboardInterrupt:
        pass
    finally:
        embedder.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
