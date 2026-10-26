"""Memory items: provenance, proposed / approved / rejected, user-only decisions, audit without content."""

from __future__ import annotations

import json
import sqlite3

import pytest

from xodus.memory.manager import MemoryManager
from xodus.security.audit import AuditLog, verify
from xodus.storage.knowledge import Conflict, NotFound
from xodus.storage.memory import MemoryStore
from xodus.storage.migrate import StorageError


@pytest.fixture
def mm(kb_store, tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    manager = MemoryManager(MemoryStore(kb_store.db), audit)
    manager.audit_path = audit.path
    return manager


def propose(mm, content="The user prefers short answers.", kind="preference", **over):
    args = dict(source="task:ab12", model="qwen3:4b", prompt_version="extract-1")
    args.update(over)
    return mm.propose(kind, content, **args)


def test_user_items_are_approved_at_once_with_provenance(mm):
    item = mm.create_user_item("note", "  Water the plants on Sunday.  ")
    assert item["status"] == "approved" and item["created_by"] == "user" and item["source"] == "manual"
    assert item["content"] == "Water the plants on Sunday." and item["decided_at"] and item["model"] is None and not item["edited_by_user"]


def test_model_items_always_start_proposed_and_carry_their_provenance(mm):
    item = propose(mm)
    assert item["status"] == "proposed" and item["created_by"] == "model" and item["decided_at"] is None
    assert (item["model"], item["prompt_version"], item["source"]) == ("qwen3:4b", "extract-1", "task:ab12")
    assert mm.store.approved() == []  # a proposal is not memory yet


def test_there_is_no_way_to_create_an_approved_model_item(mm, kb_store):
    for kwargs in (dict(model="", prompt_version="p"), dict(model="m", prompt_version="")):
        with pytest.raises(StorageError, match="model and prompt version"):
            propose(mm, **kwargs)
    with pytest.raises(TypeError):
        mm.propose("fact", "x", source="manual", model="m", prompt_version="p", status="approved")  # no such parameter
    with kb_store.db.connection() as conn, pytest.raises(sqlite3.IntegrityError, match="CHECK"):  # not even with direct SQL
        conn.execute("INSERT INTO memory_items(kind,content,status,created_by,source,model,prompt_version,created_at,updated_at,decided_at)"
                     " VALUES ('fact','x','approved','model','task:a','m','p',0,0,NULL)")
    assert mm.store.approved() == []


@pytest.mark.parametrize("kind, content, source", [("secret", "x", "manual"), ("note", "", "manual"), ("note", "   ", "manual"),
                                                  ("note", "x" * 10_001, "manual"), ("note", "x", "somewhere"), ("note", "x", "document:abc"),
                                                  ("note", "x", "task:XYZ"), ("note", "x", "manual; DROP TABLE memory_items")])
def test_invalid_items_are_rejected(mm, kind, content, source):
    with pytest.raises(StorageError):
        mm.create_user_item(kind, content, source=source)


def test_approve_and_reject_are_decisions_with_a_timestamp(mm):
    item = propose(mm)
    approved = mm.decide(item["id"], "approve")
    assert approved["status"] == "approved" and approved["decided_at"] >= item["created_at"]
    assert [i["id"] for i in mm.store.approved()] == [item["id"]]
    with pytest.raises(Conflict, match="already approved"):
        mm.decide(item["id"], "approve")
    rejected = mm.decide(item["id"], "reject")  # the user can change their mind
    assert rejected["status"] == "rejected" and mm.store.approved() == []
    with pytest.raises(Conflict):
        mm.decide(item["id"], "reject")
    with pytest.raises(StorageError, match="approve' or 'reject"):
        mm.decide(item["id"], "maybe")
    with pytest.raises(NotFound):
        mm.decide(9999, "approve")


def test_only_approved_items_are_ever_used_as_memory(mm):
    a = mm.create_user_item("fact", "approved by creation")
    b = propose(mm, "proposed only")
    c = propose(mm, "approved later")
    d = propose(mm, "rejected")
    mm.decide(c["id"], "approve")
    mm.decide(d["id"], "reject")
    assert {i["id"] for i in mm.store.approved()} == {a["id"], c["id"]}
    assert {i["id"] for i in mm.store.approved(("preference",))} == {c["id"]}
    assert b["id"] not in {i["id"] for i in mm.store.approved()}


def test_editing_never_changes_the_status_but_marks_the_item_as_edited(mm):
    item = propose(mm, "Draft wording from the model")
    edited = mm.edit(item["id"], "Corrected by the user")
    assert edited["content"] == "Corrected by the user" and edited["status"] == "proposed" and edited["edited_by_user"] == 1
    assert mm.store.approved() == []  # editing a proposal does not approve it
    mm.decide(item["id"], "approve")
    again = mm.edit(item["id"], "Edited after approval")
    assert again["status"] == "approved" and again["edited_by_user"] == 1
    with pytest.raises(StorageError):
        mm.edit(item["id"], "  ")
    with pytest.raises(NotFound):
        mm.edit(9999, "x")


def test_list_filters_pagination_and_export(mm):
    for i in range(7):
        mm.create_user_item("note", f"note {i}")
    p = propose(mm, "a proposal", kind="fact")
    assert len(mm.store.list(limit=3, offset=0)) == 3 and len(mm.store.list(limit=3, offset=6)) == 2
    assert [i["id"] for i in mm.store.list(status="proposed")] == [p["id"]]
    assert len(mm.store.list(created_by="user")) == 7 and len(mm.store.list(kind="fact")) == 1
    assert len(mm.store.list(limit=100_000)) == 8  # the page size is capped, not unlimited
    export = mm.export()
    assert export["format"] == "xodus-memory-export" and len(export["items"]) == 8
    exported = next(i for i in export["items"] if i["id"] == p["id"])
    assert exported["model"] == "qwen3:4b" and exported["prompt_version"] == "extract-1" and exported["status"] == "proposed"


def test_delete(mm):
    item = mm.create_user_item("note", "to delete")
    mm.delete(item["id"])
    with pytest.raises(NotFound):
        mm.store.get(item["id"])
    with pytest.raises(NotFound):
        mm.delete(item["id"])


def test_everything_is_audited_and_the_audit_log_never_contains_the_content(mm):
    secret = "my-api-password-is-hunter2-and-nobody-should-see-this"
    a = mm.create_user_item("note", secret)
    b = propose(mm, f"model says {secret}")
    mm.decide(b["id"], "approve")
    mm.edit(a["id"], secret + " edited")
    mm.export()
    mm.delete(a["id"])
    text = mm.audit_path.read_text(encoding="utf-8")
    events = [json.loads(line)["event"] for line in text.splitlines()]
    assert events == ["memory_created", "memory_proposed", "memory_approved", "memory_edited", "memory_exported", "memory_deleted"]
    assert "hunter2" not in text and verify(mm.audit_path).ok
    proposed = json.loads(text.splitlines()[1])
    assert proposed["model"] == "qwen3:4b" and proposed["prompt_version"] == "extract-1" and len(proposed["content_sha256"]) == 16
