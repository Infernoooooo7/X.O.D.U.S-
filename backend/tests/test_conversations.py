"""Conversations: storage, bounded history, cascading deletion, and how chat, ask and tasks save their turns.

A saved assistant turn says how it really ended: 'complete' only after the backend finished it, otherwise
'partial', 'error' or 'cancelled'. Nothing here claims a success the backend did not confirm.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.helpers import ScriptedProvider, api_client
from tests.kb_helpers import topic_markdown
from tests.test_api import parse_sse
from tests.test_knowledge_api import create_collection, fake_chat, ingest, wait_jobs  # noqa: F401
from xodus.api.app import create_app
from xodus.providers.base import ProviderError, StreamChunk
from xodus.retrieval.citations import INSUFFICIENT_PHRASE
from xodus.storage.conversations import ConversationStore, title_from
from xodus.storage.knowledge import NotFound
from xodus.storage.memory import MemoryStore
from xodus.storage.migrate import StorageError
from xodus.storage.sqlite import Database

CANARY = "qqzcanaryconversation4242"


@pytest.fixture
def store(tmp_path):
    db = Database(tmp_path / "c.db", embedding_dim=8)
    db.open()
    yield ConversationStore(db)
    db.close()


# ---------------------------------------------------------------------------------------------- the store


def test_create_list_rename_and_messages(store):
    a = store.create("First")
    b = store.create("  Second  ")
    store.add_message(a["id"], role="user", mode="chat", content="hello")
    assert [c["title"] for c in store.list()] == ["First", "Second"]  # newest activity first
    listed = store.list()[0]
    assert listed["messages"] == 1 and listed["last_message"] == "hello"
    assert store.rename(b["id"], "Renamed")["title"] == "Renamed"
    with pytest.raises(StorageError):
        store.rename(b["id"], "   ")
    with pytest.raises(NotFound):
        store.rename(999, "x")
    msgs = store.messages(a["id"])
    assert msgs[0]["role"] == "user" and msgs[0]["meta"] == {} and msgs[0]["status"] == "complete"
    with pytest.raises(StorageError):
        store.add_message(a["id"], role="system", mode="chat", content="x")  # CHECK constraint
    with pytest.raises(NotFound):
        store.add_message(999, role="user", mode="chat", content="x")


def test_search_treats_wildcards_literally(store):
    store.create("100% done")
    store.create("100 percent")
    store.create("a_b")
    store.create("axb")
    assert [c["title"] for c in store.list(query="100%")] == ["100% done"]
    assert [c["title"] for c in store.list(query="a_b")] == ["a_b"]


def test_title_from_first_message():
    assert title_from("  how   do I\nparse JSON? ") == "how do I parse JSON?"
    assert title_from("x" * 100).endswith("…") and len(title_from("x" * 100)) == 60
    assert title_from("   ") == "Untitled"


def test_history_keeps_the_newest_turns_within_the_budget(store):
    c = store.create("h")["id"]
    for i in range(10):
        store.add_message(c, role="user", mode="chat", content=f"question {i} " + "w" * 40)
        store.add_message(c, role="assistant", mode="chat", content=f"answer {i} " + "w" * 40)
    store.add_message(c, role="user", mode="chat", content="failed q")
    store.add_message(c, role="assistant", mode="chat", content="", status="error")
    history = store.history(c, max_tokens=60)  # about 4 turns of ~14 tokens
    assert history[0]["role"] == "user" and history[-1]["content"] == "failed q"
    assert all("question 0" not in h["content"] for h in history)  # the oldest were dropped
    assert all(h["content"] for h in history)  # the failed (empty) reply is not sent back to the model
    assert sum(len(h["content"]) / 4 + 4 for h in history) <= 60
    assert store.history(c, max_tokens=0) == []


def test_deleting_removes_messages_and_derived_memory_and_leaves_no_residue(store, tmp_path):
    keep, gone = store.create("keep")["id"], store.create("gone")["id"]
    store.add_message(keep, role="user", mode="chat", content="unrelated text")
    for i in range(30):
        store.add_message(gone, role="user", mode="chat", content=f"{CANARY} line {i} " + "filler " * 40)
    memory = MemoryStore(store.db)
    memory.create(kind="fact", content=f"{CANARY} derived", created_by="model", source=f"conversation:{gone}", model="m",
                  prompt_version="p")
    other = memory.create(kind="fact", content="kept fact", created_by="user", source=f"conversation:{keep}")
    report = store.delete(gone)
    assert report == {"conversation_id": gone, "messages": 30, "memory_items": 1, "messages_left": 0}
    assert [m["id"] for m in memory.list()] == [other["id"]]
    assert len(store.messages(keep)) == 1
    with pytest.raises(NotFound):
        store.get(gone)
    raw = Path(store.db.path).read_bytes()
    wal = Path(str(store.db.path) + "-wal")
    raw += wal.read_bytes() if wal.exists() else b""
    assert CANARY.encode() not in raw  # secure_delete + checkpoint: the text left the file


# ---------------------------------------------------------------------------------------------- chat over HTTP


@pytest.fixture
def client(make_settings, fake_ollama):
    fake_ollama.models = [{"name": "test-model:latest", "digest": "aaa", "size": 1}]
    opened = []

    def make(extra: str = "", provider=None) -> TestClient:
        c = api_client(create_app(make_settings(extra=extra, base_url=fake_ollama.url), provider=provider))
        c.__enter__()
        opened.append(c)
        return c

    yield make
    for c in opened:
        c.__exit__(None, None, None)


def new_conversation(c, title="Test") -> int:
    r = c.post("/api/conversations", json={"title": title})
    assert r.status_code == 201
    return r.json()["id"]


def test_chat_saves_both_turns_and_sends_earlier_turns_as_history(client, fake_ollama):
    fake_ollama.chat_answer = lambda messages: f"reply to {messages[-1]['content']}"
    c = client()
    conv = new_conversation(c)
    first = parse_sse(c.post("/api/chat", json={"message": "My name is Ada.", "conversation_id": conv}).text)
    assert first[0][1]["conversation_id"] == conv and first[0][1]["history_turns"] == 0
    assert first[-1][0] == "done" and first[-1][1]["message_id"]
    second = parse_sse(c.post("/api/chat", json={"message": "What is my name?", "conversation_id": conv}).text)
    assert second[0][1]["history_turns"] == 2
    sent = fake_ollama.chat_requests[-1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"] and sent[0]["content"] == "My name is Ada."
    items = c.get(f"/api/conversations/{conv}").json()["items"]
    assert [(m["role"], m["status"]) for m in items] == [("user", "complete"), ("assistant", "complete")] * 2
    assert items[1]["content"].strip() == "reply to My name is Ada." and items[1]["model"] == "test-model:latest"
    assert items[1]["meta"]["usage"]["output_tokens"] == 3 and items[1]["meta"]["num_ctx"] == 8192


def test_chat_without_a_conversation_saves_nothing(client, fake_ollama):
    c = client()
    parse_sse(c.post("/api/chat", json={"message": "hi"}).text)
    assert c.get("/api/conversations").json() == []
    assert fake_ollama.chat_requests[-1]["messages"][-1]["content"] == "hi"


def test_history_is_cut_to_the_configured_budget(client, fake_ollama):
    c = client("[chat]\nmax_history_tokens = 40\n")
    conv = new_conversation(c)
    for i in range(6):
        c.post("/api/chat", json={"message": f"message number {i} " + "x" * 60, "conversation_id": conv})
    sent = fake_ollama.chat_requests[-1]["messages"]
    assert len(sent) < 12 and sent[-1]["content"].startswith("message number 5")
    assert not any("message number 0" in m["content"] for m in sent)


def test_unknown_conversation_is_refused_before_the_model_is_called(client, fake_ollama):
    c = client()
    r = c.post("/api/chat", json={"message": "hi", "conversation_id": 999})
    assert r.status_code == 404 and fake_ollama.chat_requests == []
    assert c.post("/api/tasks", json={"goal": "x", "conversation_id": 999}).status_code == 404
    assert c.get("/api/tasks").json() == []  # no task was started


def test_a_provider_error_is_saved_as_an_error_not_a_reply(client, fake_ollama):
    c = client()
    conv = new_conversation(c)
    fake_ollama.load_fail = {"test-model:latest"}
    events = parse_sse(c.post("/api/chat", json={"message": "hi", "conversation_id": conv}).text)
    assert events[-1][0] == "error"
    reply = c.get(f"/api/conversations/{conv}").json()["items"][-1]
    assert reply["role"] == "assistant" and reply["status"] == "error" and reply["content"] == ""
    assert reply["meta"]["code"] and "terminated" in reply["meta"]["message"]


class BreaksMidStream(ScriptedProvider):
    async def stream(self, messages, budget, *, seed=None):
        yield StreamChunk(text="Half an ")
        yield StreamChunk(text="answer")
        raise ProviderError("The connection to Ollama was lost (was it stopped?).")


def test_a_stream_that_breaks_is_saved_as_partial(client):
    c = client(provider=BreaksMidStream())
    conv = new_conversation(c)
    events = parse_sse(c.post("/api/chat", json={"message": "hi", "conversation_id": conv}).text)
    assert events[-1][0] == "error" and events[-1][1]["partial"] is True
    reply = c.get(f"/api/conversations/{conv}").json()["items"][-1]
    assert reply["status"] == "partial" and reply["content"] == "Half an answer" and "lost" in reply["meta"]["message"]


# ---------------------------------------------------------------------------------------------- tasks and ask


def wait_task(c, task_id: str, timeout: float = 30) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = c.get(f"/api/tasks/{task_id}").json()
        if task["status"] not in ("queued", "running", "planning", "awaiting_approval"):
            return task
        time.sleep(0.05)
    raise AssertionError("task did not finish")


def conversation_items(c, conv: int, n: int, timeout: float = 10) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        items = c.get(f"/api/conversations/{conv}").json()["items"]
        if len(items) >= n:
            return items
        time.sleep(0.05)
    raise AssertionError("the outcome was not recorded")


def test_a_task_outcome_is_recorded_only_when_the_task_ends(client):
    c = client(provider=ScriptedProvider(answer="The sandbox holds two files."))
    conv = new_conversation(c)
    r = c.post("/api/tasks", json={"goal": "What is in my sandbox?", "conversation_id": conv})
    assert r.status_code == 202 and r.json()["conversation_id"] == conv
    task = wait_task(c, r.json()["task_id"])
    items = conversation_items(c, conv, 2)
    assert items[0]["mode"] == "task" and items[0]["task_id"] == task["id"] and items[0]["content"] == "What is in my sandbox?"
    assert items[1]["status"] == "complete" and items[1]["content"] == "The sandbox holds two files."
    assert items[1]["meta"]["task_status"] == task["status"] == "completed"


class SlowModel(ScriptedProvider):
    async def chat_with_tools(self, messages, tools, budget, *, seed=None):
        await asyncio.sleep(30)  # a slow model call, cancellable like a real HTTP request
        return await super().chat_with_tools(messages, tools, budget, seed=seed)


def test_a_cancelled_task_is_recorded_as_cancelled(client):
    c = client(provider=SlowModel())
    conv = new_conversation(c)
    task_id = c.post("/api/tasks", json={"goal": "Long job", "conversation_id": conv}).json()["task_id"]
    time.sleep(0.3)  # let it reach the model call
    assert c.post(f"/api/tasks/{task_id}/cancel").status_code == 202
    items = conversation_items(c, conv, 2, timeout=20)
    assert items[1]["status"] == "cancelled" and items[1]["meta"]["task_status"] == "cancelled"


def test_ask_saves_the_answer_with_its_validated_citations(kb_settings, fake_ollama, docs_root, monkeypatch):
    monkeypatch.setenv("XODUS_INGESTION__MONITOR_INTERVAL_S", "0.2")
    fake_ollama.chat_answer = fake_chat
    (docs_root / "astro.md").write_text(topic_markdown("astronomy", 3, 3), encoding="utf-8")
    with api_client(create_app(kb_settings(base_url=fake_ollama.url))) as c:
        create_collection(c)
        wait_jobs(c, [j["id"] for j in ingest(c, path="astro.md").json()["jobs"]])
        conv = new_conversation(c, "Space")
        events = parse_sse(c.post("/api/knowledge/ask", json={"question": "telescope nebula redshift supernova",
                                                               "conversation_id": conv}).text)
        done = events[-1][1]
        assert events[-1][0] == "done" and done["message_id"]
        miss = parse_sse(c.post("/api/knowledge/ask", json={"question": "quantum chromodynamics lagrangian gauge",
                                                             "conversation_id": conv}).text)
        items = c.get(f"/api/conversations/{conv}").json()["items"]
    assert [(m["role"], m["mode"]) for m in items] == [("user", "ask"), ("assistant", "ask")] * 2
    answer = items[1]
    assert answer["status"] == "complete" and answer["meta"]["citations"]["valid"] == done["citations"]["valid"]
    assert answer["meta"]["evidence"][0]["document"] == "astro.md" and answer["meta"]["model_called"] is True
    assert miss[-1][1]["answer"] == INSUFFICIENT_PHRASE
    assert items[3]["content"] == INSUFFICIENT_PHRASE and items[3]["meta"]["insufficient"] and items[3]["meta"]["model_called"] is False


# ---------------------------------------------------------------------------------------------- the conversation API


def test_conversation_api_and_audit(client):
    c = client()
    conv = new_conversation(c, "Plans")
    c.post("/api/chat", json={"message": f"remember {CANARY}", "conversation_id": conv})
    assert c.patch(f"/api/conversations/{conv}", json={"title": "Holiday plans"}).json()["title"] == "Holiday plans"
    assert [x["title"] for x in c.get("/api/conversations?q=holiday").json()] == ["Holiday plans"]
    for bad in ({"title": ""}, {"title": "x" * 201}, {"title": "ok", "id": 5}):
        assert c.post("/api/conversations", json=bad).status_code == 422
    assert c.patch(f"/api/conversations/{conv}", json={"title": "   "}).status_code == 422
    assert c.get("/api/conversations?limit=0").status_code == 422 and c.get("/api/conversations?limit=201").status_code == 422
    deleted = c.delete(f"/api/conversations/{conv}")
    assert deleted.status_code == 200 and deleted.json()["deleted"]["messages"] == 2 and deleted.json()["deleted"]["messages_left"] == 0
    assert c.get(f"/api/conversations/{conv}").status_code == 404 and c.delete(f"/api/conversations/{conv}").status_code == 404
    assert c.delete(f"/api/conversations/{new_conversation(c)}", headers={"Origin": "http://evil.example"}).status_code == 403
    records = [json.loads(line) for line in c.app.state.audit.path.read_text(encoding="utf-8").splitlines()]
    event = next(r for r in records if r["event"] == "conversation_deleted")
    assert event["messages"] == 2 and event["verified_clean"] is True
    assert CANARY not in c.app.state.audit.path.read_text(encoding="utf-8")  # the audit log holds counts, not content
