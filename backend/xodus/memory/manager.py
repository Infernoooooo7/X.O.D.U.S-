"""Memory manager: the only place memory items are created or decided, with every change audited.

Rules (master prompt section 7):
  * A user creates items; they are approved at once.
  * A model can only *propose*: propose() always yields status 'proposed', needs the model name and prompt
    version as provenance, and nothing proposed is used as memory until the user approves it.
  * Approve and reject are user decisions made through the API; nothing in the model path calls them.
  * Deleting a document deletes the facts derived from it (database cascade).
The audit log records ids, kinds and a content hash, never the content itself.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from xodus.security.audit import AuditLog
from xodus.storage.memory import KINDS, MemoryStore
from xodus.storage.migrate import StorageError

_SOURCE = re.compile(r"^(manual|task:[0-9a-f]{1,32}|document:\d{1,12}|conversation:\d{1,12})$")
MAX_CONTENT = 10_000


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


class MemoryManager:
    def __init__(self, store: MemoryStore, audit: AuditLog) -> None:
        self.store = store
        self.audit = audit

    @staticmethod
    def _check(kind: str, content: str, source: str) -> str:
        if kind not in KINDS:
            raise StorageError(f"kind must be one of {list(KINDS)}.")
        content = content.strip()
        if not content or len(content) > MAX_CONTENT:
            raise StorageError(f"content must be 1 to {MAX_CONTENT} characters.")
        if not _SOURCE.match(source):
            raise StorageError("source must be 'manual', 'task:<id>', 'document:<n>' or 'conversation:<n>'.")
        return content

    def create_user_item(self, kind: str, content: str, *, source: str = "manual") -> dict[str, Any]:
        content = self._check(kind, content, source)
        item = self.store.create(kind=kind, content=content, created_by="user", source=source)
        self.audit.append("memory_created", item_id=item["id"], kind=kind, created_by="user", content_sha256=_digest(content))
        return item

    def propose(self, kind: str, content: str, *, source: str, model: str, prompt_version: str,
                document_id: int | None = None) -> dict[str, Any]:
        """A model-derived item. Always 'proposed'; there is no way to create it already approved."""
        content = self._check(kind, content, source)
        if not model or not prompt_version:
            raise StorageError("A model-derived item needs the model and prompt version it came from.")
        item = self.store.create(kind=kind, content=content, created_by="model", source=source, document_id=document_id,
                                 model=model, prompt_version=prompt_version)
        self.audit.append("memory_proposed", item_id=item["id"], kind=kind, source=source, model=model,
                          prompt_version=prompt_version, document_id=document_id, content_sha256=_digest(content))
        return item

    def decide(self, item_id: int, decision: str) -> dict[str, Any]:
        item = self.store.decide(item_id, decision)
        self.audit.append("memory_approved" if item["status"] == "approved" else "memory_rejected", item_id=item_id,
                          kind=item["kind"], created_by=item["created_by"], content_sha256=_digest(item["content"]))
        return item

    def edit(self, item_id: int, content: str) -> dict[str, Any]:
        content = content.strip()
        if not content or len(content) > MAX_CONTENT:
            raise StorageError(f"content must be 1 to {MAX_CONTENT} characters.")
        item = self.store.edit(item_id, content)
        self.audit.append("memory_edited", item_id=item_id, kind=item["kind"], status=item["status"],
                          content_sha256=_digest(content))
        return item

    def delete(self, item_id: int) -> None:
        item = self.store.get(item_id)
        self.store.delete(item_id)
        self.audit.append("memory_deleted", item_id=item_id, kind=item["kind"], status=item["status"],
                          created_by=item["created_by"])

    def export(self) -> dict[str, Any]:
        items = self.store.export_all()
        self.audit.append("memory_exported", items=len(items))
        return {"format": "xodus-memory-export", "version": 1, "items": items}
