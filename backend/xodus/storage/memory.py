"""Memory items (notes, facts, preferences) with provenance and a proposed / approved / rejected status."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from xodus.storage.knowledge import Conflict, NotFound, _marks, _page
from xodus.storage.migrate import StorageError
from xodus.storage.sqlite import Database

KINDS = ("note", "fact", "preference")
STATUSES = ("proposed", "approved", "rejected")


class MemoryStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, *, kind: str, content: str, created_by: str, source: str, document_id: int | None = None,
               model: str | None = None, prompt_version: str | None = None) -> dict[str, Any]:
        """User-created items are approved at once. Model-created items are always 'proposed'."""
        now = time.time()
        status = "approved" if created_by == "user" else "proposed"
        decided = now if created_by == "user" else None
        try:
            with self.db.transaction() as conn:
                cur = conn.execute(
                    "INSERT INTO memory_items(kind, content, status, created_by, source, document_id, model, prompt_version,"
                    " created_at, updated_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (kind, content, status, created_by, source, document_id, model, prompt_version, now, now, decided))
                return dict(conn.execute("SELECT * FROM memory_items WHERE id = ?", (cur.lastrowid,)).fetchone())
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"Invalid memory item: {exc}") from exc

    def get(self, item_id: int) -> dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM memory_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise NotFound(f"No memory item with id {item_id}.")
        return dict(row)

    def list(self, *, status: str | None = None, kind: str | None = None, created_by: str | None = None, limit: int = 50,
             offset: int = 0) -> list[dict[str, Any]]:
        limit, offset = _page(limit, offset)
        where, args = [], []
        for column, value in (("status", status), ("kind", kind), ("created_by", created_by)):
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)
        sql = "SELECT * FROM memory_items" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id LIMIT ? OFFSET ?"
        with self.db.connection() as conn:
            return [dict(r) for r in conn.execute(sql, (*args, limit, offset))]

    def approved(self, kinds: tuple[str, ...] = KINDS, limit: int = 200) -> list[dict[str, Any]]:
        """The only items that may be used as long-term memory."""
        with self.db.connection() as conn:
            return [dict(r) for r in conn.execute(
                f"SELECT * FROM memory_items WHERE status = 'approved' AND kind IN ({_marks(len(kinds))}) ORDER BY id LIMIT ?",
                (*kinds, limit))]

    def edit(self, item_id: int, content: str) -> dict[str, Any]:
        """A user edit. The status does not change; the item is marked as edited by the user."""
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM memory_items WHERE id = ?", (item_id,)).fetchone() is None:
                raise NotFound(f"No memory item with id {item_id}.")
            try:
                conn.execute("UPDATE memory_items SET content = ?, edited_by_user = 1, updated_at = ? WHERE id = ?",
                             (content, time.time(), item_id))
            except sqlite3.IntegrityError as exc:
                raise StorageError(f"Invalid memory item: {exc}") from exc
        return self.get(item_id)

    def decide(self, item_id: int, decision: str) -> dict[str, Any]:
        """Record the user's decision ('approve' or 'reject'). Deciding the same way twice is a conflict."""
        new_status = {"approve": "approved", "reject": "rejected"}.get(decision)
        if new_status is None:
            raise StorageError("decision must be 'approve' or 'reject'.")
        with self.db.transaction() as conn:
            row = conn.execute("SELECT status FROM memory_items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise NotFound(f"No memory item with id {item_id}.")
            if row["status"] == new_status:
                raise Conflict(f"Memory item {item_id} is already {new_status}.")
            now = time.time()
            conn.execute("UPDATE memory_items SET status = ?, decided_at = ?, updated_at = ? WHERE id = ?",
                         (new_status, now, now, item_id))
        return self.get(item_id)

    def delete(self, item_id: int) -> None:
        with self.db.transaction() as conn:
            if conn.execute("DELETE FROM memory_items WHERE id = ?", (item_id,)).rowcount == 0:
                raise NotFound(f"No memory item with id {item_id}.")

    def export_all(self) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM memory_items ORDER BY id")]
