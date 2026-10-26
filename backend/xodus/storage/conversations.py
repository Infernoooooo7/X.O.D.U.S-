"""Conversations and their messages (master prompt 7: conversations are a typed store, inspectable and deletable).

Deleting a conversation removes its messages (foreign-key cascade) and the memory facts derived from it
(source 'conversation:<id>') in one transaction; the database runs with secure_delete, like the knowledge store.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from xodus.storage.knowledge import NotFound, _page
from xodus.storage.migrate import StorageError
from xodus.storage.sqlite import Database

MODES = ("chat", "ask", "task")
STATUSES = ("complete", "partial", "error", "cancelled")
MAX_TITLE = 200


def title_from(text: str, limit: int = 60) -> str:
    """A conversation title from its first message: one line, at most `limit` characters."""
    line = " ".join(text.split())
    if not line:
        return "Untitled"
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _message(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["meta"] = json.loads(out["meta"])
    return out


class ConversationStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, title: str) -> dict[str, Any]:
        title = title.strip()[:MAX_TITLE] or "Untitled"
        now = time.time()
        with self.db.transaction() as conn:
            cur = conn.execute("INSERT INTO conversations(title, created_at, updated_at) VALUES (?,?,?)", (title, now, now))
            return self._get(conn, cur.lastrowid)

    def _get(self, conn: sqlite3.Connection, conversation_id: int) -> dict[str, Any]:
        row = conn.execute(
            "SELECT c.*, (SELECT count(*) FROM messages m WHERE m.conversation_id = c.id) AS messages"
            " FROM conversations c WHERE c.id = ?", (conversation_id,)).fetchone()
        if row is None:
            raise NotFound(f"No conversation with id {conversation_id}.")
        return dict(row)

    def get(self, conversation_id: int) -> dict[str, Any]:
        with self.db.connection() as conn:
            return self._get(conn, conversation_id)

    def list(self, limit: int = 50, offset: int = 0, query: str | None = None) -> list[dict[str, Any]]:
        """Newest first, with the message count and the start of the last message."""
        limit, offset = _page(limit, offset)
        where, args = "", []
        if query:
            where = " WHERE c.title LIKE ? ESCAPE '\\'"
            args.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        sql = ("SELECT c.*, (SELECT count(*) FROM messages m WHERE m.conversation_id = c.id) AS messages,"
               " (SELECT substr(m.content, 1, 160) FROM messages m WHERE m.conversation_id = c.id ORDER BY m.id DESC LIMIT 1)"
               " AS last_message FROM conversations c" + where + " ORDER BY c.updated_at DESC, c.id DESC LIMIT ? OFFSET ?")
        with self.db.connection() as conn:
            return [dict(r) for r in conn.execute(sql, (*args, limit, offset))]

    def rename(self, conversation_id: int, title: str) -> dict[str, Any]:
        title = title.strip()[:MAX_TITLE]
        if not title:
            raise StorageError("The title cannot be empty.")
        with self.db.transaction() as conn:
            if conn.execute("UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                            (title, time.time(), conversation_id)).rowcount == 0:
                raise NotFound(f"No conversation with id {conversation_id}.")
            return self._get(conn, conversation_id)

    def messages(self, conversation_id: int, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        limit, offset = _page(limit, offset, max_limit=500)
        with self.db.connection() as conn:
            self._get(conn, conversation_id)
            return [_message(r) for r in conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id LIMIT ? OFFSET ?", (conversation_id, limit, offset))]

    def add_message(self, conversation_id: int, *, role: str, mode: str, content: str, status: str = "complete",
                    model: str | None = None, request_id: str | None = None, task_id: str | None = None,
                    meta: dict[str, Any] | None = None) -> dict[str, Any]:
        now = time.time()
        try:
            with self.db.transaction() as conn:
                self._get(conn, conversation_id)
                cur = conn.execute(
                    "INSERT INTO messages(conversation_id, role, mode, content, status, model, request_id, task_id, meta, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (conversation_id, role, mode, content, status, model, request_id, task_id,
                     json.dumps(meta or {}, default=str), now))
                conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
                return _message(conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone())
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"Invalid message: {exc}") from exc

    def history(self, conversation_id: int, max_tokens: int, chars_per_token: float = 4.0,
                max_messages: int = 200) -> list[dict[str, str]]:
        """Earlier turns for the prompt, newest kept first, oldest dropped once the token budget is spent (resource
        rule 6: history is truncated, never unbounded). Failed replies are left out; partial ones are kept."""
        with self.db.connection() as conn:
            self._get(conn, conversation_id)
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? AND status IN ('complete', 'partial')"
                " AND content != '' ORDER BY id DESC LIMIT ?", (conversation_id, max_messages)).fetchall()
        kept: list[dict[str, str]] = []
        used = 0
        for row in rows:
            cost = int(len(row["content"]) / chars_per_token) + 4
            if used + cost > max_tokens:
                break
            kept.append({"role": row["role"], "content": row["content"]})
            used += cost
        kept.reverse()
        while kept and kept[0]["role"] == "assistant":  # a prompt should not open with an orphaned reply
            kept.pop(0)
        return kept

    def delete(self, conversation_id: int) -> dict[str, Any]:
        with self.db.transaction() as conn:
            self._get(conn, conversation_id)
            messages = conn.execute("SELECT count(*) FROM messages WHERE conversation_id = ?", (conversation_id,)).fetchone()[0]
            memory = conn.execute("DELETE FROM memory_items WHERE source = ?", (f"conversation:{conversation_id}",)).rowcount
            conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            left = conn.execute("SELECT count(*) FROM messages WHERE conversation_id = ?", (conversation_id,)).fetchone()[0]
        with self.db.connection() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"conversation_id": conversation_id, "messages": messages, "memory_items": memory, "messages_left": left}
