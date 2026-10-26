"""Knowledge store: collections, documents, chunks (+ keyword and vector indexes), ingestion jobs.

All writes that must stay consistent (a chunk row, its keyword entry and its vector; a checkpoint) happen
in one transaction. Deletion removes a document's chunks, keyword entries, vectors and derived memory
items together and can then prove that nothing is left (verify_gone).
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import sqlite_vec

from xodus.storage.migrate import StorageError, current_version
from xodus.storage.sqlite import Database

MAX_PAGE = 200


class NotFound(StorageError):
    code = "not_found"


class Conflict(StorageError):
    code = "conflict"


class CorpusLimitExceeded(StorageError):
    code = "corpus_limit"


class LeaseLost(StorageError):
    code = "lease_lost"


@dataclass(frozen=True)
class ChunkRecord:
    ordinal: int
    text: str
    text_hash: str
    heading_path: str
    locator: str | None
    page: int | None
    char_start: int | None
    char_end: int | None
    token_estimate: int


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def _page(limit: int, offset: int, max_limit: int = MAX_PAGE) -> tuple[int, int]:
    return max(1, min(int(limit), max_limit)), max(0, int(offset))


def _marks(n: int) -> str:
    return ",".join("?" * n)


class KnowledgeStore:
    def __init__(self, db: Database, *, max_chunks: int = 100_000) -> None:
        self.db = db
        self.max_chunks = max_chunks

    # ------------------------------------------------------------------ collections

    def create_collection(self, name: str, description: str = "") -> dict[str, Any]:
        try:
            with self.db.transaction() as conn:
                cur = conn.execute("INSERT INTO collections(name, description, created_at) VALUES (?,?,?)",
                                   (name, description, time.time()))
                return self._collection(conn, cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc):
                raise Conflict(f"A collection named {name!r} already exists.") from exc
            raise StorageError(f"Invalid collection: {exc}") from exc

    def _collection(self, conn: sqlite3.Connection, collection_id: int) -> dict[str, Any]:
        row = conn.execute(
            "SELECT c.*, (SELECT count(*) FROM documents d WHERE d.collection_id = c.id) AS documents,"
            " (SELECT count(*) FROM documents d WHERE d.collection_id = c.id AND d.status = 'ready') AS documents_ready,"
            " (SELECT count(*) FROM chunks k JOIN documents d ON d.id = k.document_id WHERE d.collection_id = c.id) AS chunks"
            " FROM collections c WHERE c.id = ?", (collection_id,)).fetchone()
        if row is None:
            raise NotFound(f"No collection with id {collection_id}.")
        return dict(row)

    def get_collection(self, collection_id: int) -> dict[str, Any]:
        with self.db.connection() as conn:
            return self._collection(conn, collection_id)

    def collection_by_name(self, name: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute("SELECT id FROM collections WHERE name = ?", (name,)).fetchone()
            return self._collection(conn, row[0]) if row else None

    def collection_id_map(self, names: Iterable[str]) -> dict[str, int]:
        names = list(names)
        if not names:
            return {}
        with self.db.connection() as conn:
            return {r[1]: r[0] for r in conn.execute(
                f"SELECT id, name FROM collections WHERE name IN ({_marks(len(names))})", names)}

    def collection_ids(self, names: Iterable[str]) -> list[int]:
        return list(self.collection_id_map(names).values())

    def list_collections(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        limit, offset = _page(limit, offset)
        with self.db.connection() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM collections ORDER BY name LIMIT ? OFFSET ?", (limit, offset))]
            return [self._collection(conn, i) for i in ids]

    def delete_collection(self, collection_id: int) -> dict[str, Any]:
        """Delete a collection with all its documents, chunks, vectors, keyword entries and derived memory."""
        self.get_collection(collection_id)
        report = {"collection_id": collection_id, "documents": 0, "chunks": 0, "vectors": 0, "memory_items": 0}
        with self.db.transaction() as conn:
            for (doc_id,) in conn.execute("SELECT id FROM documents WHERE collection_id = ?", (collection_id,)).fetchall():
                part = self._delete_document(conn, doc_id)
                for key in ("chunks", "vectors", "memory_items"):
                    report[key] += part[key]
                report["documents"] += 1
            conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
        self.checkpoint()
        return report

    # ------------------------------------------------------------------ documents

    def get_document(self, doc_id: int) -> dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT d.*, c.name AS collection FROM documents d JOIN collections c ON c.id = d.collection_id"
                               " WHERE d.id = ?", (doc_id,)).fetchone()
        if row is None:
            raise NotFound(f"No document with id {doc_id}.")
        return dict(row)

    def find_document(self, collection_id: int, root: str, path: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            return _row(conn.execute("SELECT * FROM documents WHERE collection_id = ? AND source_root = ? AND source_path = ?",
                                     (collection_id, root, path)).fetchone())

    def list_documents(self, collection_id: int | None = None, status: str | None = None, limit: int = 50,
                       offset: int = 0) -> list[dict[str, Any]]:
        limit, offset = _page(limit, offset)
        where, args = [], []
        if collection_id is not None:
            where.append("d.collection_id = ?")
            args.append(collection_id)
        if status is not None:
            where.append("d.status = ?")
            args.append(status)
        sql = ("SELECT d.*, c.name AS collection FROM documents d JOIN collections c ON c.id = d.collection_id"
               + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY d.id LIMIT ? OFFSET ?")
        with self.db.connection() as conn:
            return [dict(r) for r in conn.execute(sql, (*args, limit, offset))]

    def start_document(self, *, collection_id: int, root: str, path: str, title: str, fmt: str, size_bytes: int,
                       content_hash: str, chunker_version: int, encoding: str | None, pages: int | None = None,
                       created_by: str = "user") -> dict[str, Any]:
        """Create the document row, or reset an existing one (its old chunks and vectors are removed)."""
        now = time.time()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT id FROM documents WHERE collection_id = ? AND source_root = ? AND source_path = ?",
                               (collection_id, root, path)).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO documents(collection_id, source_root, source_path, title, format, size_bytes, content_hash,"
                    " chunker_version, encoding, status, chunks_done, pages, created_by, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,'ingesting',0,?,?,?,?)",
                    (collection_id, root, path, title, fmt, size_bytes, content_hash, chunker_version, encoding, pages,
                     created_by, now, now))
                doc_id = cur.lastrowid
            else:
                doc_id = row[0]
                self._purge_chunks(conn, doc_id)
                conn.execute(
                    "UPDATE documents SET title=?, format=?, size_bytes=?, content_hash=?, chunker_version=?, encoding=?,"
                    " status='ingesting', error=NULL, chunks_done=0, pages=?, updated_at=? WHERE id=?",
                    (title, fmt, size_bytes, content_hash, chunker_version, encoding, pages, now, doc_id))
            return dict(conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone())

    def count_chunks(self) -> int:
        with self.db.connection() as conn:
            return int(conn.execute("SELECT count(*) FROM chunks").fetchone()[0])

    def append_chunks(self, doc_id: int, collection_id: int, records: Sequence[ChunkRecord],
                      vectors: Sequence[Sequence[float]]) -> list[int]:
        """Store one batch (rows, keyword entries, vectors) and advance the checkpoint, atomically."""
        if len(records) != len(vectors):
            raise ValueError("one vector per chunk is required")
        if not records:
            return []
        for vector in vectors:
            if len(vector) != self.db.embedding_dim:
                raise StorageError(f"A vector has {len(vector)} dimensions but the database uses {self.db.embedding_dim}.")
        ids: list[int] = []
        with self.db.transaction() as conn:
            doc = conn.execute("SELECT status, chunks_done FROM documents WHERE id = ?", (doc_id,)).fetchone()
            if doc is None:
                raise NotFound(f"No document with id {doc_id}.")
            if doc["status"] != "ingesting":
                raise Conflict(f"Document {doc_id} is {doc['status']}, not ingesting.")
            if doc["chunks_done"] != records[0].ordinal:
                raise Conflict(f"Chunk batch starts at {records[0].ordinal} but the checkpoint is {doc['chunks_done']}.")
            total = int(conn.execute("SELECT count(*) FROM chunks").fetchone()[0])
            if total + len(records) > self.max_chunks:
                raise CorpusLimitExceeded(f"The index would exceed its limit of {self.max_chunks} chunks "
                                          f"({total} stored, {len(records)} more in this batch).")
            for record, vector in zip(records, vectors, strict=True):
                cur = conn.execute(
                    "INSERT INTO chunks(document_id, ordinal, text, text_hash, heading_path, locator, page, char_start,"
                    " char_end, token_estimate) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (doc_id, record.ordinal, record.text, record.text_hash, record.heading_path, record.locator,
                     record.page, record.char_start, record.char_end, record.token_estimate))
                chunk_id = cur.lastrowid
                conn.execute("INSERT INTO chunk_vectors(rowid, embedding, collection_id) VALUES (?,?,?)",
                             (chunk_id, sqlite_vec.serialize_float32(list(vector)), collection_id))
                ids.append(chunk_id)
            conn.execute("UPDATE documents SET chunks_done = chunks_done + ?, updated_at = ? WHERE id = ?",
                         (len(records), time.time(), doc_id))
        return ids

    def stored_chunk_hash(self, doc_id: int, ordinal: int) -> str | None:
        with self.db.connection() as conn:
            row = conn.execute("SELECT text_hash FROM chunks WHERE document_id = ? AND ordinal = ?", (doc_id, ordinal)).fetchone()
        return row[0] if row else None

    def finish_document(self, doc_id: int, pages: int | None = None) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE documents SET status='ready', error=NULL, pages=COALESCE(?, pages), updated_at=? WHERE id=?",
                         (pages, time.time(), doc_id))

    def fail_document(self, doc_id: int, error: str, *, purge: bool = True) -> None:
        with self.db.transaction() as conn:
            if purge:
                self._purge_chunks(conn, doc_id)
            conn.execute("UPDATE documents SET status='failed', error=?, chunks_done=CASE WHEN ? THEN 0 ELSE chunks_done END,"
                         " updated_at=? WHERE id=?", (error[:500], 1 if purge else 0, time.time(), doc_id))

    def cancel_document(self, doc_id: int) -> None:
        with self.db.transaction() as conn:
            self._purge_chunks(conn, doc_id)
            conn.execute("UPDATE documents SET status='cancelled', error=NULL, chunks_done=0, updated_at=? WHERE id=?",
                         (time.time(), doc_id))

    def _purge_chunks(self, conn: sqlite3.Connection, doc_id: int) -> int:
        ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE document_id = ?", (doc_id,))]
        for i in range(0, len(ids), 200):
            part = ids[i:i + 200]
            for chunk_id in part:  # vec0 deletes by rowid
                conn.execute("DELETE FROM chunk_vectors WHERE rowid = ?", (chunk_id,))
        conn.execute("DELETE FROM chunks WHERE document_id = ?", (doc_id,))
        return len(ids)

    def list_chunks(self, doc_id: int, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        limit, offset = _page(limit, offset)
        with self.db.connection() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, document_id, ordinal, text, heading_path, locator, page, char_start, char_end, token_estimate"
                " FROM chunks WHERE document_id = ? ORDER BY ordinal LIMIT ? OFFSET ?", (doc_id, limit, offset))]

    def get_chunks(self, chunk_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        """Chunks with their document and collection, only from documents that are ready."""
        if not chunk_ids:
            return {}
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT k.id, k.document_id, k.ordinal, k.text, k.heading_path, k.locator, k.page, k.token_estimate,"
                " d.title AS document, d.source_path, c.id AS collection_id, c.name AS collection"
                f" FROM chunks k JOIN documents d ON d.id = k.document_id JOIN collections c ON c.id = d.collection_id"
                f" WHERE k.id IN ({_marks(len(chunk_ids))}) AND d.status = 'ready'", list(chunk_ids)).fetchall()
        return {r["id"]: dict(r) for r in rows}

    # ------------------------------------------------------------------ deletion

    def _delete_document(self, conn: sqlite3.Connection, doc_id: int) -> dict[str, Any]:
        chunk_ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE document_id = ?", (doc_id,))]
        memory = int(conn.execute("SELECT count(*) FROM memory_items WHERE document_id = ?", (doc_id,)).fetchone()[0])
        vectors = 0
        for chunk_id in chunk_ids:
            vectors += conn.execute("DELETE FROM chunk_vectors WHERE rowid = ?", (chunk_id,)).rowcount or 0
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))  # cascades to chunks (+ keyword entries) and memory
        return {"document_id": doc_id, "chunk_ids": chunk_ids, "chunks": len(chunk_ids), "vectors": vectors,
                "memory_items": memory}

    def delete_document(self, doc_id: int) -> dict[str, Any]:
        """Remove a document and everything derived from it. Returns a report incl. the deleted chunk ids."""
        self.get_document(doc_id)
        with self.db.transaction() as conn:
            report = self._delete_document(conn, doc_id)
        report["checkpoint_complete"] = self.checkpoint()
        return report

    def checkpoint(self) -> bool:
        """Fold the write-ahead log into the database file and truncate it (so deleted text leaves the WAL)."""
        with self.db.connection() as conn:
            busy, _log, _done = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return busy == 0

    def verify_gone(self, doc_id: int, chunk_ids: Sequence[int], probe_terms: Sequence[str] = ()) -> dict[str, Any]:
        """Count what is left of a deleted document in every table and index; all zeros means fully removed."""
        with self.db.connection() as conn:
            left = {
                "documents": conn.execute("SELECT count(*) FROM documents WHERE id = ?", (doc_id,)).fetchone()[0],
                "chunks": conn.execute("SELECT count(*) FROM chunks WHERE document_id = ?", (doc_id,)).fetchone()[0],
                "memory_items": conn.execute("SELECT count(*) FROM memory_items WHERE document_id = ?", (doc_id,)).fetchone()[0],
                "jobs_pointing_at_it": conn.execute("SELECT count(*) FROM ingestion_jobs WHERE document_id = ?", (doc_id,)).fetchone()[0],
                "vectors": 0,
                "fts_hits": 0,
            }
            if chunk_ids:
                left["vectors"] = conn.execute(
                    f"SELECT count(*) FROM chunk_vectors_rowids WHERE rowid IN ({_marks(len(chunk_ids))})", list(chunk_ids)).fetchone()[0]
            for term in probe_terms:
                left["fts_hits"] += conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                                                 ('"' + term.replace('"', '""') + '"',)).fetchone()[0]
            try:
                conn.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES ('integrity-check', 1)")
                left["fts_index_consistent"] = True
            except sqlite3.DatabaseError as exc:
                left["fts_index_consistent"] = False
                left["fts_error"] = str(exc)
        left["clean"] = all(left[k] == 0 for k in ("documents", "chunks", "memory_items", "jobs_pointing_at_it", "vectors",
                                                    "fts_hits")) and left["fts_index_consistent"]
        return left

    # ------------------------------------------------------------------ search primitives

    def fts_candidates(self, match: str, collection_ids: Sequence[int], limit: int) -> list[tuple[int, float]]:
        """(chunk id, bm25 score) best first; only chunks of ready documents in the given collections."""
        if not match or not collection_ids:
            return []
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT c.id, bm25(chunks_fts, 1.0, 0.5) AS score FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
                " JOIN documents d ON d.id = c.document_id"
                f" WHERE chunks_fts MATCH ? AND d.status = 'ready' AND d.collection_id IN ({_marks(len(collection_ids))})"
                " ORDER BY score LIMIT ?", (match, *collection_ids, limit)).fetchall()
        return [(int(r[0]), float(r[1])) for r in rows]

    def vector_candidates(self, query_vector: Sequence[float], collection_ids: Sequence[int], limit: int) -> list[tuple[int, float]]:
        """(chunk id, cosine distance) nearest first, restricted to ready documents in the given collections.

        The collection filter runs inside the nearest-neighbour search; the 'ready' filter runs afterwards, so a
        collection that mostly holds documents still being ingested can return fewer than `limit` results.
        """
        if not collection_ids:
            return []
        blob = sqlite_vec.serialize_float32(list(query_vector))
        with self.db.connection() as conn:
            hits = conn.execute(
                "SELECT rowid, distance FROM chunk_vectors WHERE embedding MATCH ? AND k = ?"
                f" AND collection_id IN ({_marks(len(collection_ids))})", (blob, limit, *collection_ids)).fetchall()
            if not hits:
                return []
            ready = {r[0] for r in conn.execute(
                f"SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id WHERE d.status = 'ready'"
                f" AND c.id IN ({_marks(len(hits))})", [int(h[0]) for h in hits])}
        return [(int(h[0]), float(h[1])) for h in hits if int(h[0]) in ready]

    def log_retrieval(self, *, request_id: str | None, origin: str, query_hash: str, query_chars: int, collections: str,
                      mode: str, n_vector: int, n_fts: int, chunk_ids: Sequence[int], sufficient: bool,
                      embed_ms: float | None, vector_ms: float | None, fts_ms: float | None, total_ms: float) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO retrieval_log(ts, request_id, origin, query_hash, query_chars, collections, mode, n_vector, n_fts,"
                " n_returned, chunk_ids, sufficient, embed_ms, vector_ms, fts_ms, total_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), request_id, origin, query_hash, query_chars, collections, mode, n_vector, n_fts, len(chunk_ids),
                 ",".join(map(str, chunk_ids)), 1 if sufficient else 0, embed_ms, vector_ms, fts_ms, total_ms))
            conn.execute("DELETE FROM retrieval_log WHERE id <= (SELECT max(id) FROM retrieval_log) - 1000")

    # ------------------------------------------------------------------ ingestion jobs

    def enqueue_job(self, collection_id: int, root_id: str, rel_path: str, created_by: str = "user") -> dict[str, Any]:
        try:
            with self.db.transaction() as conn:
                cur = conn.execute("INSERT INTO ingestion_jobs(collection_id, root_id, rel_path, status, created_by, created_at)"
                                   " VALUES (?,?,?, 'queued', ?, ?)", (collection_id, root_id, rel_path, created_by, time.time()))
                return dict(conn.execute("SELECT * FROM ingestion_jobs WHERE id = ?", (cur.lastrowid,)).fetchone())
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc):
                raise Conflict("This file is already queued or running.") from exc
            raise StorageError(f"Could not queue the job: {exc}") from exc

    def get_job(self, job_id: int) -> dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT j.*, c.name AS collection FROM ingestion_jobs j JOIN collections c ON c.id = j.collection_id"
                               " WHERE j.id = ?", (job_id,)).fetchone()
        if row is None:
            raise NotFound(f"No ingestion job with id {job_id}.")
        return dict(row)

    def list_jobs(self, status: str | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        limit, offset = _page(limit, offset)
        with self.db.connection() as conn:
            sql = ("SELECT j.*, c.name AS collection FROM ingestion_jobs j JOIN collections c ON c.id = j.collection_id"
                   + (" WHERE j.status = ?" if status else "") + " ORDER BY j.id DESC LIMIT ? OFFSET ?")
            args = (status, limit, offset) if status else (limit, offset)
            return [dict(r) for r in conn.execute(sql, args)]

    def has_active_job(self, collection_id: int, root_id: str, rel_path: str) -> bool:
        with self.db.connection() as conn:
            return conn.execute("SELECT 1 FROM ingestion_jobs WHERE collection_id = ? AND root_id = ? AND rel_path = ?"
                                " AND status IN ('queued','running') LIMIT 1", (collection_id, root_id, rel_path)).fetchone() is not None

    def active_jobs_in_collection(self, collection_id: int) -> int:
        with self.db.connection() as conn:
            return int(conn.execute("SELECT count(*) FROM ingestion_jobs WHERE collection_id = ?"
                                    " AND status IN ('queued','running')", (collection_id,)).fetchone()[0])

    def pending_jobs(self) -> int:
        with self.db.connection() as conn:
            return int(conn.execute("SELECT count(*) FROM ingestion_jobs WHERE status IN ('queued','running')").fetchone()[0])

    def running_jobs(self) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM ingestion_jobs WHERE status = 'running'")]

    def claim_next_job(self, owner: str) -> dict[str, Any] | None:
        now = time.time()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT id FROM ingestion_jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return None
            conn.execute("UPDATE ingestion_jobs SET status='running', claimed_by=?, heartbeat_at=?,"
                         " started_at=COALESCE(started_at, ?), attempts=attempts+1 WHERE id=?", (owner, now, now, row[0]))
            return dict(conn.execute("SELECT * FROM ingestion_jobs WHERE id = ?", (row[0],)).fetchone())

    def heartbeat_job(self, job_id: int, owner: str, chunks_done: int | None = None) -> bool:
        """Renew the lease; returns True if cancellation was requested. Raises LeaseLost if the job is no longer ours."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT claimed_by, status, cancel_requested FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None or row["status"] != "running" or row["claimed_by"] != owner:
                raise LeaseLost(f"Job {job_id} is no longer leased to this worker.")
            conn.execute("UPDATE ingestion_jobs SET heartbeat_at=?, chunks_done=COALESCE(?, chunks_done) WHERE id=?",
                         (time.time(), chunks_done, job_id))
            return bool(row["cancel_requested"])

    def finish_job(self, job_id: int, owner: str | None, status: str, *, outcome: str | None = None,
                   error_code: str | None = None, error: str | None = None, document_id: int | None = None,
                   chunks_done: int | None = None) -> None:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE ingestion_jobs SET status=?, outcome=?, error_code=?, error=?, document_id=COALESCE(?, document_id),"
                " chunks_done=COALESCE(?, chunks_done), finished_at=?, claimed_by=NULL WHERE id=?"
                + (" AND claimed_by = ?" if owner else ""),
                (status, outcome, error_code, (error or None) and error[:500], document_id, chunks_done, time.time(), job_id,
                 *([owner] if owner else [])))
            if owner and cur.rowcount == 0:
                raise LeaseLost(f"Job {job_id} is no longer leased to this worker.")

    def request_cancel(self, job_id: int) -> dict[str, Any]:
        """Queued jobs are cancelled at once; running jobs stop at their next checkpoint."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT status FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise NotFound(f"No ingestion job with id {job_id}.")
            if row["status"] == "queued":
                conn.execute("UPDATE ingestion_jobs SET status='cancelled', finished_at=? WHERE id=?", (time.time(), job_id))
            elif row["status"] == "running":
                conn.execute("UPDATE ingestion_jobs SET cancel_requested=1 WHERE id=?", (job_id,))
            else:
                raise Conflict(f"Job {job_id} is already {row['status']}.")
        return self.get_job(job_id)

    def requeue_orphans(self, is_alive: Callable[[str], bool], max_attempts: int) -> dict[str, list[int]]:
        """Jobs leased to a worker that no longer exists go back to the queue, or fail after max_attempts."""
        result: dict[str, list[int]] = {"requeued": [], "failed": []}
        with self.db.transaction() as conn:
            for row in conn.execute("SELECT id, claimed_by, attempts FROM ingestion_jobs WHERE status = 'running'").fetchall():
                if row["claimed_by"] and is_alive(row["claimed_by"]):
                    continue
                if row["attempts"] >= max_attempts:
                    conn.execute(
                        "UPDATE ingestion_jobs SET status='failed', error_code='worker_died', claimed_by=NULL, finished_at=?,"
                        " error=? WHERE id=?",
                        (time.time(), f"The ingestion worker stopped unexpectedly {row['attempts']} times while processing this "
                                      "file (it may be malformed); not retrying.", row["id"]))
                    result["failed"].append(row["id"])
                else:
                    conn.execute("UPDATE ingestion_jobs SET status='queued', claimed_by=NULL WHERE id=?", (row["id"],))
                    result["requeued"].append(row["id"])
        return result

    def ingestion_paused(self) -> bool:
        with self.db.connection() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'ingestion_paused'").fetchone()
        return row is not None and row[0] == "1"

    def set_ingestion_paused(self, paused: bool) -> None:
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO meta(key, value) VALUES ('ingestion_paused', ?)"
                         " ON CONFLICT(key) DO UPDATE SET value = excluded.value", ("1" if paused else "0",))

    def requeue_running(self) -> list[int]:
        """Put running jobs back in the queue after the user paused ingestion. The attempt is not counted: the worker was
        stopped on purpose, so a pause can never push a file over the attempt limit. Progress resumes from the checkpoint."""
        with self.db.transaction() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM ingestion_jobs WHERE status = 'running'")]
            conn.execute("UPDATE ingestion_jobs SET status='queued', claimed_by=NULL, attempts=MAX(attempts - 1, 0)"
                         " WHERE status = 'running'")
        return ids

    # ------------------------------------------------------------------ statistics

    def stats(self) -> dict[str, Any]:
        with self.db.connection() as conn:
            def count(sql: str, *args: Any) -> int:
                return int(conn.execute(sql, args).fetchone()[0])

            by_status = lambda table: {r[0]: r[1] for r in conn.execute(f"SELECT status, count(*) FROM {table} GROUP BY status")}  # noqa: E731
            chunks = count("SELECT count(*) FROM chunks")
            sizes = self.db.file_sizes()
            page_count, page_size = conn.execute("PRAGMA page_count").fetchone()[0], conn.execute("PRAGMA page_size").fetchone()[0]
            free = conn.execute("PRAGMA freelist_count").fetchone()[0]
            return {
                "schema_version": current_version(conn),
                "embedding_dimensions": self.db.embedding_dim,
                "collections": count("SELECT count(*) FROM collections"),
                "documents": by_status("documents"),
                "chunks": chunks,
                "vectors": count("SELECT count(*) FROM chunk_vectors_rowids"),
                "chunk_limit": self.max_chunks,
                "jobs": by_status("ingestion_jobs"),
                "memory_items": by_status("memory_items"),
                "file_bytes": sizes,
                "database_bytes_per_chunk": round(sizes["db"] / chunks) if chunks else None,
                "page_size": page_size,
                "pages": page_count,
                "free_pages": free,
            }
