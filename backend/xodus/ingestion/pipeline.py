"""Ingestion of one source file (one job): validate, parse, chunk, embed in batches, store with checkpoints.

Properties (each has a test):
  * the path is re-validated here against the configured root, whatever the queue says (links, ADS, device
    names, '..'); a file that changed or became a link after it was queued is refused;
  * idempotent: an unchanged file is skipped (content hash + chunker version); a changed file replaces the old
    document, whose chunks and vectors are removed when the new ingestion starts;
  * resumable: each batch of at most embedding.batch_size chunks is committed with a checkpoint. After a crash
    or forced stop the file is parsed again (the chunker is deterministic), the stored chunks are skipped without
    re-embedding, and ingestion continues; a mismatch between stored and regenerated chunks restarts the file;
  * bounded: file size, PDF pages and total chunks are limited; memory is one batch plus one block;
  * cancellable at the next batch boundary, which removes the partial document.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from xodus.config.settings import Settings
from xodus.ingestion.chunking import CHUNKER_VERSION, ChunkParams, chunk_blocks
from xodus.ingestion.parsers import Limits, ParseError, SourceInfo, hash_file, inspect_file, iter_blocks
from xodus.ingestion.text import Block
from xodus.providers.base import ModelNotAvailable, ProviderError, ProviderTimeout, ProviderUnavailable
from xodus.providers.ollama import EmbeddingDimensionError
from xodus.security.paths import PathViolation, resolve_existing
from xodus.storage.knowledge import ChunkRecord, CorpusLimitExceeded, KnowledgeStore, LeaseLost, NotFound

DB_HEARTBEAT_EVERY_S = 20.0


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class IngestError(Exception):
    """A job outcome the user should see: a stable code and a message meant for people."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable  # keep the partial document so that re-queueing the file resumes it


class Cancelled(Exception):
    pass


class _Restart(Exception):
    """The stored chunks do not match what the file produces now; start the file again from zero."""


@dataclass
class Progress:
    """Wall-clock of the last sign of life; the worker's watchdog ends the process if it stops advancing."""

    last: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last = time.monotonic()


@dataclass(frozen=True)
class RootInfo:
    path: Path
    grants: frozenset[str]


@dataclass
class PipelineDeps:
    settings: Settings
    store: KnowledgeStore
    embedder: Embedder
    roots: dict[str, RootInfo]
    owner: str
    progress: Progress = field(default_factory=Progress)
    sleep: Callable[[float], None] = time.sleep


def embedding_input(template: str, chunk: ChunkRecord) -> str:
    body = f"{chunk.heading_path}\n{chunk.text}" if chunk.heading_path else chunk.text
    return template.replace("{text}", body)


def _embed(d: PipelineDeps, texts: list[str]) -> list[list[float]]:
    retries = d.settings.ingestion.embed_retries
    for attempt in range(retries + 1):
        try:
            return d.embedder.embed(texts)
        except (ProviderUnavailable, ProviderTimeout) as exc:
            if attempt == retries:
                raise IngestError("embedding_unavailable", f"{exc.message} Ingestion stopped at a checkpoint; queue the file "
                                  "again to resume.", retryable=True) from exc
            d.sleep(d.settings.ingestion.embed_retry_backoff_s * (2 ** attempt))
            d.progress.touch()
        except ModelNotAvailable as exc:
            raise IngestError("embedding_model_missing", exc.message, retryable=True) from exc
        except EmbeddingDimensionError as exc:
            raise IngestError("embedding_dimension_mismatch", exc.message) from exc
        except ProviderError as exc:
            raise IngestError("embedding_error", exc.message, retryable=True) from exc
    raise AssertionError("unreachable")  # pragma: no cover


def process_job(d: PipelineDeps, job: dict[str, Any]) -> dict[str, Any]:
    """Run one queued job to completion. Returns {outcome, document_id, chunks}; raises IngestError or Cancelled."""
    store, k = d.store, d.settings.knowledge
    limits, params = Limits.from_settings(k), ChunkParams.from_settings(k)
    try:
        collection = store.get_collection(job["collection_id"])
    except NotFound as exc:
        raise IngestError("collection_missing", "The collection no longer exists.") from exc
    root = d.roots.get(job["root_id"])
    if root is None or "fs.read" not in root.grants:
        raise IngestError("permission_denied", f"Folder {job['root_id']!r} is not configured or not readable.")
    try:
        path = resolve_existing(root.path, job["rel_path"], "file")
    except PathViolation as exc:
        raise IngestError(exc.code, exc.message) from exc
    d.progress.touch()
    try:
        info = inspect_file(path, limits)
    except ParseError as exc:
        raise IngestError(exc.code, exc.message) from exc
    d.progress.touch()

    existing = store.find_document(collection["id"], job["root_id"], job["rel_path"])
    current = existing is not None and existing["content_hash"] == info.content_hash and existing["chunker_version"] == CHUNKER_VERSION
    if current and existing["status"] == "ready":
        return {"outcome": "unchanged", "document_id": existing["id"], "chunks": existing["chunks_done"]}
    resume = bool(current and existing["status"] == "ingesting")
    outcome = "replaced" if existing is not None and existing["status"] == "ready" else "ingested"

    def start() -> dict[str, Any]:
        return store.start_document(collection_id=collection["id"], root=job["root_id"], path=job["rel_path"], title=path.name,
                                    fmt=info.format, size_bytes=info.size_bytes, content_hash=info.content_hash,
                                    chunker_version=CHUNKER_VERSION, encoding=info.encoding, pages=info.pages,
                                    created_by=job.get("created_by", "user"))

    doc = existing if resume else start()
    start_at = doc["chunks_done"] if resume else 0
    try:
        for attempt in (1, 2):
            try:
                total = _ingest(d, job, doc, collection["id"], path, info, limits, params, start_at)
                break
            except _Restart:
                if attempt == 2:
                    raise IngestError("resume_mismatch", "The stored chunks could not be matched on resume.") from None
                doc, start_at = start(), 0
        store.finish_document(doc["id"], info.pages)
    except Cancelled:
        store.cancel_document(doc["id"])
        raise
    except IngestError as exc:
        if exc.retryable:
            raise  # keep the checkpoint so that queueing the file again resumes it
        store.fail_document(doc["id"], exc.message)
        raise
    except ParseError as exc:
        store.fail_document(doc["id"], exc.message)
        raise IngestError(exc.code, exc.message) from exc
    except CorpusLimitExceeded as exc:
        store.fail_document(doc["id"], exc.message)
        raise IngestError("corpus_limit", exc.message) from exc
    except LeaseLost:
        raise  # another worker owns the job now; leave the document and its checkpoint alone
    except Exception as exc:
        store.fail_document(doc["id"], f"Unexpected error ({type(exc).__name__}).")
        raise
    return {"outcome": outcome, "document_id": doc["id"], "chunks": total}


def _ingest(d: PipelineDeps, job: dict[str, Any], doc: dict[str, Any], collection_id: int, path: Path, info: SourceInfo,
            limits: Limits, params: ChunkParams, start_at: int) -> int:
    store, template = d.store, d.settings.embedding.document_template
    batch_size = d.settings.embedding.batch_size
    state = {"last_beat": time.monotonic(), "done": start_at}

    def tick() -> None:
        """Called for every block: feeds the watchdog and renews the job lease now and then."""
        d.progress.touch()
        now = time.monotonic()
        if now - state["last_beat"] >= DB_HEARTBEAT_EVERY_S:
            state["last_beat"] = now
            if store.heartbeat_job(job["id"], d.owner, state["done"]):
                raise Cancelled()

    def watched(blocks: Iterator[Block]) -> Iterator[Block]:
        for block in blocks:
            tick()
            yield block

    def flush(batch: list[ChunkRecord]) -> None:
        if store.heartbeat_job(job["id"], d.owner, state["done"]):  # also checks that the lease is still ours
            raise Cancelled()
        vectors = _embed(d, [embedding_input(template, c) for c in batch])
        store.append_chunks(doc["id"], collection_id, batch, vectors)
        state["done"] += len(batch)
        state["last_beat"] = time.monotonic()
        store.heartbeat_job(job["id"], d.owner, state["done"])
        d.progress.touch()

    batch: list[ChunkRecord] = []
    produced = 0
    for chunk in chunk_blocks(watched(iter_blocks(path, info, limits)), params):
        produced = chunk.ordinal + 1
        if chunk.ordinal < start_at:
            if chunk.ordinal == start_at - 1 and store.stored_chunk_hash(doc["id"], chunk.ordinal) != chunk.text_hash:
                raise _Restart()
            continue
        batch.append(chunk)
        if len(batch) >= batch_size:
            flush(batch)
            batch = []
    if batch:
        flush(batch)
    if start_at > produced:
        raise _Restart()  # the file now yields fewer chunks than were stored
    if produced == 0:
        raise IngestError("no_text", "The file contains no text to index.")
    try:
        rehash = hash_file(path, limits.max_file_bytes)[0]
    except (ParseError, OSError) as exc:
        raise IngestError("file_changed", "The file could not be read again to confirm it did not change.") from exc
    if rehash != info.content_hash:
        raise IngestError("file_changed", "The file changed while it was being ingested; queue it again.")
    return produced
