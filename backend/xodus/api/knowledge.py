"""HTTP routes for knowledge (collections, ingestion, search, ask, documents) and memory.

Everything here is a user action: the user's own calls are not limited by the model-facing kb.read grants.
Destructive routes (delete) refuse while ingestion of the same file or collection is queued or running, write
the audit log, and return a verification of what was removed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from xodus.config.settings import Settings
from xodus.ingestion.service import IngestionService, IngestRequestError
from xodus.memory.manager import MemoryManager
from xodus.obs import log_event
from xodus.retrieval.answer import AnswerService
from xodus.retrieval.hybrid import QueryError, Retriever
from xodus.security.audit import AuditLog
from xodus.storage.knowledge import Conflict, CorpusLimitExceeded, KnowledgeStore, LeaseLost, NotFound
from xodus.storage.migrate import StorageError

Name = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]
STATUS = {NotFound: 404, Conflict: 409, CorpusLimitExceeded: 413, LeaseLost: 409}


@dataclass
class KnowledgeContext:
    settings: Settings
    store: KnowledgeStore
    memory: MemoryManager
    ingestion: IngestionService
    retriever: Retriever
    answers: AnswerService
    audit: AuditLog


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CollectionCreate(_Strict):
    name: Name
    description: str = Field("", max_length=500)


class IngestRequest(_Strict):
    collection: Name
    root: Name
    path: str = Field(".", max_length=260)
    recursive: bool = False


class SearchRequest(_Strict):
    query: str = Field(min_length=1, max_length=4000)
    collections: list[Name] | None = Field(None, max_length=20)
    k: int | None = Field(None, ge=1, le=50)


class AskRequest(_Strict):
    question: str = Field(min_length=1, max_length=4000)
    collections: list[Name] | None = Field(None, max_length=20)
    k: int | None = Field(None, ge=1, le=50)


class MemoryCreate(_Strict):
    kind: Literal["note", "fact", "preference"]
    content: str = Field(min_length=1, max_length=10_000)


class MemoryEdit(_Strict):
    content: str = Field(min_length=1, max_length=10_000)


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def register_knowledge_routes(app: FastAPI, k: KnowledgeContext) -> None:
    store, memory, audit = k.store, k.memory, k.audit

    @app.exception_handler(StorageError)
    async def storage_error(_: Request, exc: StorageError) -> JSONResponse:
        status = next((code for cls, code in STATUS.items() if isinstance(exc, cls)), 500 if "migration" in exc.code or
                      "dimension" in exc.code else 422)
        return JSONResponse({"ok": False, "error": {"code": exc.code, "message": exc.message}}, status)

    @app.exception_handler(IngestRequestError)
    async def ingest_error(_: Request, exc: IngestRequestError) -> JSONResponse:
        log_event("ingest_request_refused", logging.WARNING, code=exc.code)
        return JSONResponse({"ok": False, "error": {"code": exc.code, "message": exc.message}}, exc.status)

    @app.exception_handler(QueryError)
    async def query_error(_: Request, exc: QueryError) -> JSONResponse:
        return JSONResponse({"ok": False, "error": {"code": "invalid_query", "message": str(exc)}}, 422)

    async def collection_ids(names: list[str] | None) -> list[int]:
        if names is None:
            return [c["id"] for c in await asyncio.to_thread(store.list_collections, 200, 0)]
        wanted = list(dict.fromkeys(names))
        found = await asyncio.to_thread(store.collection_id_map, wanted)
        missing = [n for n in wanted if n not in found]
        if missing:
            raise NotFound("No collection named " + ", ".join(repr(n) for n in missing) + ".")
        return [found[n] for n in wanted]

    # ------------------------------------------------------------------ collections

    @app.get("/api/knowledge/collections")
    async def list_collections(limit: Limit = 50, offset: Offset = 0) -> list[dict[str, Any]]:
        return await asyncio.to_thread(store.list_collections, limit, offset)

    @app.post("/api/knowledge/collections", status_code=201)
    async def create_collection(req: CollectionCreate) -> dict[str, Any]:
        created = await asyncio.to_thread(store.create_collection, req.name, req.description)
        audit.append("collection_created", collection=req.name)
        return created

    @app.delete("/api/knowledge/collections/{collection_id}")
    async def delete_collection(collection_id: int) -> dict[str, Any]:
        coll = await asyncio.to_thread(store.get_collection, collection_id)
        if await asyncio.to_thread(store.active_jobs_in_collection, collection_id):
            raise Conflict("Ingestion is queued or running for this collection; cancel those jobs first.")
        report = await asyncio.to_thread(store.delete_collection, collection_id)
        audit.append("collection_deleted", collection=coll["name"], documents=report["documents"], chunks=report["chunks"],
                     vectors=report["vectors"], memory_items=report["memory_items"])
        return {"deleted": report}

    # ------------------------------------------------------------------ ingestion

    @app.post("/api/knowledge/ingest", status_code=202)
    async def ingest(req: IngestRequest) -> JSONResponse:
        queued = await asyncio.to_thread(k.ingestion.enqueue, req.collection, req.root, req.path, req.recursive)
        status = 202 if queued.jobs else 422
        return JSONResponse({"jobs": queued.jobs, "rejected": queued.rejected, "paused": queued.paused}, status)

    @app.post("/api/ingestion/pause")
    async def pause_ingestion() -> dict[str, Any]:
        return await asyncio.to_thread(k.ingestion.pause, "user")

    @app.post("/api/ingestion/resume")
    async def resume_ingestion() -> dict[str, Any]:
        return await asyncio.to_thread(k.ingestion.resume, "user")

    @app.get("/api/knowledge/jobs")
    async def list_jobs(status: Literal["queued", "running", "completed", "failed", "cancelled"] | None = None,
                        limit: Limit = 50, offset: Offset = 0) -> list[dict[str, Any]]:
        return await asyncio.to_thread(store.list_jobs, status, limit, offset)

    @app.get("/api/knowledge/jobs/{job_id}")
    async def get_job(job_id: int) -> dict[str, Any]:
        return await asyncio.to_thread(store.get_job, job_id)

    @app.post("/api/knowledge/jobs/{job_id}/cancel", status_code=202)
    async def cancel_job(job_id: int) -> dict[str, Any]:
        job = await asyncio.to_thread(store.request_cancel, job_id)
        audit.append("ingest_cancel_requested", job_id=job_id, status=job["status"])
        return job

    @app.get("/api/knowledge/stats")
    async def stats() -> dict[str, Any]:
        return {**await asyncio.to_thread(store.stats), "ingestion": k.ingestion.status()}

    # ------------------------------------------------------------------ documents and chunks

    @app.get("/api/knowledge/documents")
    async def list_documents(collection: Name | None = None, status: Literal["ingesting", "ready", "failed", "cancelled"] | None = None,
                             limit: Limit = 50, offset: Offset = 0) -> list[dict[str, Any]]:
        cid = None
        if collection is not None:
            coll = await asyncio.to_thread(store.collection_by_name, collection)
            if coll is None:
                raise NotFound(f"No collection named {collection!r}.")
            cid = coll["id"]
        return await asyncio.to_thread(store.list_documents, cid, status, limit, offset)

    @app.get("/api/knowledge/documents/{doc_id}")
    async def get_document(doc_id: int) -> dict[str, Any]:
        return await asyncio.to_thread(store.get_document, doc_id)

    @app.get("/api/knowledge/documents/{doc_id}/chunks")
    async def document_chunks(doc_id: int, limit: Limit = 50, offset: Offset = 0) -> list[dict[str, Any]]:
        await asyncio.to_thread(store.get_document, doc_id)
        return await asyncio.to_thread(store.list_chunks, doc_id, limit, offset)

    @app.get("/api/knowledge/chunks/{chunk_id}")
    async def get_chunk(chunk_id: int) -> dict[str, Any]:
        rows = await asyncio.to_thread(store.get_chunks, [chunk_id])
        if chunk_id not in rows:
            raise NotFound(f"No chunk with id {chunk_id} (or its document is not ready).")
        return rows[chunk_id]

    @app.delete("/api/knowledge/documents/{doc_id}")
    async def delete_document(doc_id: int) -> dict[str, Any]:
        doc = await asyncio.to_thread(store.get_document, doc_id)
        if await asyncio.to_thread(store.has_active_job, doc["collection_id"], doc["source_root"], doc["source_path"]):
            raise Conflict("Ingestion of this file is queued or running; cancel the job first.")
        report = await asyncio.to_thread(store.delete_document, doc_id)
        verification = await asyncio.to_thread(store.verify_gone, doc_id, report["chunk_ids"])
        audit.append("document_deleted", document_id=doc_id, title=doc["title"], collection=doc["collection"],
                     chunks=report["chunks"], vectors=report["vectors"], memory_items=report["memory_items"],
                     verified_clean=verification["clean"])
        return {"deleted": {key: report[key] for key in ("document_id", "chunks", "vectors", "memory_items", "checkpoint_complete")},
                "verification": verification}

    # ------------------------------------------------------------------ search and ask

    @app.post("/api/knowledge/search")
    async def search(req: SearchRequest) -> dict[str, Any]:
        result = await k.retriever.search(req.query, await collection_ids(req.collections), k=req.k, origin="user")
        return {"mode": result.mode, "sufficient": result.sufficient, "degraded": result.degraded, "reasons": result.reasons,
                "stats": result.stats, "results": [{**e.summary(), "text": e.text} for e in result.evidence]}

    @app.post("/api/knowledge/ask")
    async def ask(req: AskRequest) -> StreamingResponse:
        ids = await collection_ids(req.collections)

        async def events() -> AsyncIterator[str]:
            async for name, data in k.answers.stream(req.question, ids, req.k):
                yield _sse(name, data)

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------ memory

    @app.get("/api/memory/items")
    async def list_memory(status: Literal["proposed", "approved", "rejected"] | None = None,
                          kind: Literal["note", "fact", "preference"] | None = None,
                          created_by: Literal["user", "model"] | None = None, limit: Limit = 50,
                          offset: Offset = 0) -> list[dict[str, Any]]:
        return await asyncio.to_thread(lambda: memory.store.list(status=status, kind=kind, created_by=created_by,
                                                                 limit=limit, offset=offset))

    @app.post("/api/memory/items", status_code=201)
    async def create_memory(req: MemoryCreate) -> dict[str, Any]:
        return await asyncio.to_thread(memory.create_user_item, req.kind, req.content)

    @app.get("/api/memory/items/{item_id}")
    async def get_memory(item_id: int) -> dict[str, Any]:
        return await asyncio.to_thread(memory.store.get, item_id)

    @app.patch("/api/memory/items/{item_id}")
    async def edit_memory(item_id: int, req: MemoryEdit) -> dict[str, Any]:
        return await asyncio.to_thread(memory.edit, item_id, req.content)

    @app.post("/api/memory/items/{item_id}/approve")
    async def approve_memory(item_id: int) -> dict[str, Any]:
        return await asyncio.to_thread(memory.decide, item_id, "approve")

    @app.post("/api/memory/items/{item_id}/reject")
    async def reject_memory(item_id: int) -> dict[str, Any]:
        return await asyncio.to_thread(memory.decide, item_id, "reject")

    @app.delete("/api/memory/items/{item_id}")
    async def delete_memory(item_id: int) -> dict[str, Any]:
        await asyncio.to_thread(memory.delete, item_id)
        return {"deleted": item_id}

    @app.get("/api/memory/export")
    async def export_memory() -> dict[str, Any]:
        return await asyncio.to_thread(memory.export)
