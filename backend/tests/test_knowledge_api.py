"""Knowledge and memory over HTTP, with a real backend, a real supervised ingestion worker process and a fake Ollama."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient

from tests.kb_helpers import DIM, TOPICS, topic_markdown, write_docx, write_pdf
from xodus.api.app import create_app
from xodus.retrieval.citations import INSUFFICIENT_PHRASE
from xodus.security.audit import verify
from xodus.storage.migrate import load_migrations


def wait_for(condition, timeout: float = 30.0, interval: float = 0.1, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}")


def parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        out.append((lines["event"], json.loads(lines["data"])))
    return out


@pytest.fixture
def env(monkeypatch):
    """Quicker supervision loops for tests (environment variables override the config files)."""
    monkeypatch.setenv("XODUS_INGESTION__MONITOR_INTERVAL_S", "0.2")
    monkeypatch.setenv("XODUS_SUPERVISOR__REAPER_INTERVAL_S", "0.3")
    return monkeypatch


@pytest.fixture
def make_client(env, kb_settings, fake_ollama):
    opened = []

    def _make(extra: str = "") -> TestClient:
        settings = kb_settings(extra=extra, base_url=fake_ollama.url)
        client = TestClient(create_app(settings), base_url="http://127.0.0.1")
        client.__enter__()
        opened.append(client)
        return client

    yield _make
    for client in reversed(opened):
        client.__exit__(None, None, None)


def fake_chat(messages) -> str:
    ids = re.findall(r"Evidence \[chunk:(\d+)\]", messages[-1]["content"])
    return f"According to the documents the answer is so [chunk:{ids[0]}] and also [chunk:{ids[-1]}]." if ids else "no evidence"


def create_collection(client, name="main"):
    r = client.post("/api/knowledge/collections", json={"name": name, "description": "test"})
    assert r.status_code == 201, r.text
    return r.json()


def ingest(client, path=".", collection="main", root="docs", recursive=False):
    return client.post("/api/knowledge/ingest", json={"collection": collection, "root": root, "path": path, "recursive": recursive})


def wait_jobs(client, ids, timeout=40):
    def done():
        jobs = [client.get(f"/api/knowledge/jobs/{i}").json() for i in ids]
        return jobs if all(j["status"] in ("completed", "failed", "cancelled") for j in jobs) else None

    return wait_for(done, timeout, what="jobs to finish")


# ---------------------------------------------------------------------------------------------- the whole flow


def test_ingest_search_ask_delete_end_to_end(make_client, docs_root, fake_ollama):
    fake_ollama.chat_answer = fake_chat
    (docs_root / "astro.md").write_text(topic_markdown("astronomy", 3, 3), encoding="utf-8")
    write_pdf(docs_root / "cook.pdf", [TOPICS["cooking"] * 4] * 3)
    write_docx(docs_root / "money.docx", [("h1", "Finance memo"), ("p", TOPICS["finance"] * 5)])
    client = make_client()
    create_collection(client)
    r = ingest(client)
    assert r.status_code == 202 and len(r.json()["jobs"]) == 3 and r.json()["rejected"] == []

    # while jobs run, the worker is a supervised core-tier process in its own job object
    procs = client.get("/api/processes").json()
    worker = next((s for s in procs["supervised"] if s["role"] == "ingestion"), None)
    assert worker and worker["tier"] == "core" and worker["owner"] == "ingestion" and worker["memory_limit_bytes"] == 1536 * 2**20
    assert any(p["tier"] == "core" and p["group"] == "xodus_app" and p["pid"] in worker["job_pids"] for p in procs["processes"])

    jobs = wait_jobs(client, [j["id"] for j in r.json()["jobs"]])
    assert [j["status"] for j in jobs] == ["completed"] * 3 and all(j["outcome"] == "ingested" and j["chunks_done"] > 0 for j in jobs)

    stats = client.get("/api/knowledge/stats").json()
    assert stats["documents"] == {"ready": 3} and stats["chunks"] == stats["vectors"] > 6 and stats["schema_version"] == len(load_migrations())
    assert stats["embedding_dimensions"] == DIM and stats["file_bytes"]["db"] > 0 and stats["ingestion"]["worker"]["starts_total"] == 1

    docs = client.get("/api/knowledge/documents?collection=main&limit=2").json()
    assert len(docs) == 2 and len(client.get("/api/knowledge/documents?limit=2&offset=2").json()) == 1
    doc = next(d for d in client.get("/api/knowledge/documents").json() if d["format"] == "pdf")
    chunks = client.get(f"/api/knowledge/documents/{doc['id']}/chunks").json()
    assert chunks and chunks[0]["locator"].startswith("page") and client.get(f"/api/knowledge/chunks/{chunks[0]['id']}").json()["document"] == "cook.pdf"

    found = client.post("/api/knowledge/search", json={"query": "telescope nebula redshift supernova", "k": 3}).json()
    assert found["mode"] == "hybrid" and found["sufficient"] and found["results"][0]["document"] == "astro.md" and "text" in found["results"][0]

    with client.stream("POST", "/api/knowledge/ask", json={"question": "How does compound interest work for a portfolio?",
                                                           "collections": ["main"]}) as resp:
        events = parse_sse(resp.read().decode())
    names = [n for n, _ in events]
    assert names[0] == "meta" and names[-1] == "done" and "token" in names
    done = events[-1][1]
    assert done["model_called"] and done["citations"]["valid"] and done["citations"]["invalid"] == []
    assert events[0][1]["evidence"][0]["document"] == "money.docx"
    sent = fake_ollama.chat_requests[-1]
    assert "tools" not in sent  # the model call carries no tools

    unanswerable = parse_sse(client.post("/api/knowledge/ask", json={"question": "quantum chromodynamics lagrangian gauge symmetry"}).text)
    assert [n for n, _ in unanswerable] == ["meta", "done"] and unanswerable[-1][1]["answer"] == INSUFFICIENT_PHRASE
    assert len(fake_ollama.chat_requests) == 1  # no second model call

    gone = next(d for d in client.get("/api/knowledge/documents").json() if d["format"] == "docx")
    deleted = client.delete(f"/api/knowledge/documents/{gone['id']}")
    assert deleted.status_code == 200 and deleted.json()["verification"]["clean"] is True
    assert deleted.json()["deleted"]["chunks"] == deleted.json()["deleted"]["vectors"] > 0
    assert client.get(f"/api/knowledge/documents/{gone['id']}").status_code == 404
    assert client.delete(f"/api/knowledge/documents/{gone['id']}").status_code == 404
    after = client.get("/api/knowledge/stats").json()
    assert after["documents"] == {"ready": 2} and after["chunks"] == after["vectors"]

    audit = [json.loads(line) for line in client.app.state.audit.path.read_text().splitlines()]
    events_seen = [(a["event"]) for a in audit]
    assert {"collection_created", "ingest_requested", "document_deleted", "process_started"} <= set(events_seen)
    deletion = next(a for a in audit if a["event"] == "document_deleted")
    assert deletion["title"] == "money.docx" and deletion["verified_clean"] is True
    assert verify(client.app.state.audit.path).ok


def test_health_reports_the_knowledge_state(make_client):
    client = make_client()
    health = client.get("/api/health").json()["knowledge"]
    assert health["embedding"]["status"] == "ok" and health["embedding"]["native_dimensions"] == DIM and health["embedding"]["num_gpu"] == 0
    assert health["store"]["schema_version"] == len(load_migrations()) and health["ingestion"]["worker"]["running"] is False


def test_embedding_model_problems_show_up_in_health(make_client, fake_ollama):
    fake_ollama.show_missing_for = {"qwen3-embedding:0.6b"}
    health = make_client().get("/api/health").json()["knowledge"]["embedding"]
    assert health["status"] == "unavailable" and "ollama pull qwen3-embedding:0.6b" in health["message"]


def test_a_database_for_another_embedding_size_stops_startup_with_advice(kb_settings, fake_ollama):
    from xodus.storage.migrate import EmbeddingDimensionMismatch

    create_app(kb_settings(base_url=fake_ollama.url)).state.db.close()  # creates the database at 128 dimensions
    other = kb_settings(base_url=fake_ollama.url)
    other = other.model_copy(update={"embedding": other.embedding.model_copy(update={"dimensions": 64})})
    with pytest.raises(EmbeddingDimensionMismatch, match="re-ingest"):
        create_app(other)


def test_the_command_line_reports_a_storage_error_cleanly(tmp_path, kb_settings, fake_ollama):
    settings = kb_settings(base_url=fake_ollama.url)
    create_app(settings).state.db.close()
    import os

    env = {**os.environ, "XODUS_DATA_DIR": str(settings.data_dir), "XODUS_LOG_DIR": str(tmp_path / "logs2"), "XODUS_EMBEDDING__DIMENSIONS": "64",
           "XODUS_SERVER__PORT": "58321"}
    result = subprocess.run([sys.executable, "-m", "xodus"], cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 2 and "storage error" in result.stderr and "embedding_dimension_mismatch" in result.stderr
    assert "Traceback" not in result.stderr


# ---------------------------------------------------------------------------------------------- ingest requests


def test_ingest_request_validation(make_client, docs_root):
    (docs_root / "a.md").write_text(topic_markdown("music", 1, 2), encoding="utf-8")
    (docs_root / "tool.exe").write_bytes(b"MZ")
    client = make_client()
    create_collection(client)
    assert ingest(client, collection="nope").status_code == 404
    assert ingest(client, root="elsewhere").status_code == 403
    assert ingest(client, path="missing.md").status_code == 404
    only_bad = ingest(client, path="tool.exe")
    assert only_bad.status_code == 422 and only_bad.json()["rejected"][0]["code"] == "unsupported_format" and only_bad.json()["jobs"] == []
    for bad in ({"collection": "main", "root": "docs", "path": "a.md", "bogus": 1}, {"collection": "Main", "root": "docs"}, {"collection": "main"},
                {"collection": "main", "root": "docs", "path": "x" * 400}):
        assert client.post("/api/knowledge/ingest", json=bad).status_code == 422, bad
    assert client.post("/api/knowledge/collections", json={"name": "Has Space"}).status_code == 422
    assert client.post("/api/knowledge/collections", json={"name": "main"}).status_code == 409


def test_a_file_is_not_queued_twice_and_too_many_files_are_refused(make_client, docs_root, fake_ollama, env):
    fake_ollama.embed_delay_s = 1.0
    for i in range(5):
        (docs_root / f"d{i}.md").write_text(topic_markdown("music", 2, 3), encoding="utf-8")
    env.setenv("XODUS_KNOWLEDGE__MAX_FILES_PER_REQUEST", "4")
    client = make_client()
    create_collection(client)
    too_many = ingest(client)
    assert too_many.status_code == 413 and too_many.json()["error"]["code"] == "too_many_files"
    first = ingest(client, path="d0.md")
    again = ingest(client, path="d0.md")
    assert first.status_code == 202 and again.status_code == 422 and again.json()["rejected"][0]["code"] == "already_queued"


def test_folder_ingest_skips_links_temp_files_and_unsupported_types(make_client, docs_root, tmp_path):
    (docs_root / "keep.md").write_text(topic_markdown("music", 1, 2), encoding="utf-8")
    (docs_root / "~$lock.docx").write_bytes(b"PK")
    (docs_root / "half.md.xodus-tmp").write_text("partial")
    (docs_root / "photo.png").write_bytes(b"\x89PNG")
    (docs_root / "nested").mkdir()
    (docs_root / "nested" / "deep.md").write_text(topic_markdown("finance", 1, 2), encoding="utf-8")
    (tmp_path / "elsewhere").mkdir(exist_ok=True)
    (tmp_path / "elsewhere" / "secret.md").write_text("# secret\n\n" + "classified " * 50)
    junction = subprocess.run(["cmd", "/c", "mklink", "/J", str(docs_root / "jump"), str(tmp_path / "elsewhere")], capture_output=True)
    client = make_client()
    create_collection(client)
    flat = ingest(client)
    assert [j["rel_path"] for j in flat.json()["jobs"]] == ["keep.md"]  # not recursive: nested/ is not entered
    codes = {r["path"]: r["code"] for r in flat.json()["rejected"]}
    assert codes["photo.png"] == "unsupported_format" and "~$lock.docx" not in codes and "half.md.xodus-tmp" not in codes
    if junction.returncode == 0:
        assert codes["jump"] == "link"
    deep = ingest(client, recursive=True)
    paths = sorted(j["rel_path"] for j in deep.json()["jobs"])
    assert paths == ["nested/deep.md"]  # keep.md is already queued; the junction was not followed
    assert not any("secret" in p for p in paths)
    wait_jobs(client, [j["id"] for j in flat.json()["jobs"] + deep.json()["jobs"]])
    texts = " ".join(c["text"] for d in client.get("/api/knowledge/documents").json() for c in client.get(f"/api/knowledge/documents/{d['id']}/chunks").json())
    assert "classified" not in texts


def test_cancel_and_delete_rules_while_a_job_is_running(make_client, docs_root, fake_ollama):
    fake_ollama.embed_delay_s = 0.4
    (docs_root / "big.md").write_text(topic_markdown("astronomy", 8, 4), encoding="utf-8")
    (docs_root / "queued.md").write_text(topic_markdown("music", 2, 3), encoding="utf-8")
    client = make_client()
    coll = create_collection(client)
    running = ingest(client, path="big.md").json()["jobs"][0]
    queued = ingest(client, path="queued.md").json()["jobs"][0]
    wait_for(lambda: client.get(f"/api/knowledge/jobs/{running['id']}").json()["status"] == "running", what="the job to start")
    wait_for(lambda: client.get("/api/knowledge/documents?status=ingesting").json(), what="the document row")
    doc = client.get("/api/knowledge/documents?status=ingesting").json()[0]
    assert client.delete(f"/api/knowledge/documents/{doc['id']}").status_code == 409  # not while ingesting
    assert client.delete(f"/api/knowledge/collections/{coll['id']}").status_code == 409
    assert client.post(f"/api/knowledge/jobs/{queued['id']}/cancel").json()["status"] == "cancelled"  # queued: immediate
    assert client.post(f"/api/knowledge/jobs/{running['id']}/cancel").status_code == 202
    jobs = wait_jobs(client, [running["id"], queued["id"]])
    assert [j["status"] for j in jobs] == ["cancelled", "cancelled"]
    assert client.get("/api/knowledge/stats").json()["chunks"] == 0
    assert client.post(f"/api/knowledge/jobs/{running['id']}/cancel").status_code == 409  # already finished
    assert client.post("/api/knowledge/jobs/9999/cancel").status_code == 404
    assert client.delete(f"/api/knowledge/collections/{coll['id']}").status_code == 200  # nothing active any more


# ---------------------------------------------------------------------------------------------- worker lifecycle


def test_the_idle_worker_is_stopped_and_started_again_on_demand(make_client, docs_root, env):
    env.setenv("XODUS_INGESTION__WORKER_IDLE_TIMEOUT_S", "1.5")
    (docs_root / "a.md").write_text(topic_markdown("music", 2, 3), encoding="utf-8")
    (docs_root / "b.md").write_text(topic_markdown("finance", 2, 3), encoding="utf-8")
    client = make_client()
    create_collection(client)
    first = ingest(client, path="a.md").json()["jobs"][0]
    wait_jobs(client, [first["id"]])
    pid = client.get("/api/knowledge/stats").json()["ingestion"]["worker"]["pid"]
    assert pid and psutil.pid_exists(pid)
    wait_for(lambda: client.get("/api/processes").json()["supervised"] == [], timeout=20, what="the idle worker to be stopped")
    assert not psutil.pid_exists(pid)
    stats = client.get("/api/knowledge/stats").json()["ingestion"]["worker"]
    assert stats["running"] is False and stats["starts_total"] == 1
    second = ingest(client, path="b.md").json()["jobs"][0]
    wait_jobs(client, [second["id"]])
    assert client.get("/api/knowledge/stats").json()["ingestion"]["worker"]["starts_total"] == 2
    audit = [json.loads(line) for line in client.app.state.audit.path.read_text().splitlines()]
    assert any(a["event"] == "process_stopped" and a["reason"] == "idle" and a["role"] == "ingestion" for a in audit)


def test_a_killed_worker_is_restarted_and_the_job_resumes(make_client, docs_root, fake_ollama):
    fake_ollama.embed_delay_s = 0.5
    (docs_root / "big.md").write_text(topic_markdown("astronomy", 8, 4), encoding="utf-8")
    client = make_client()
    create_collection(client)
    job = ingest(client, path="big.md").json()["jobs"][0]
    pid = wait_for(lambda: client.get("/api/knowledge/stats").json()["ingestion"]["worker"]["pid"], what="the worker")
    wait_for(lambda: len(fake_ollama.embed_calls) >= 2, what="two batches")
    psutil.Process(pid).kill()
    fake_ollama.embed_delay_s = 0.0
    done = wait_jobs(client, [job["id"]], timeout=60)[0]
    assert done["status"] == "completed" and done["attempts"] == 2
    stats = client.get("/api/knowledge/stats").json()
    assert stats["ingestion"]["worker"]["starts_total"] == 2 and stats["chunks"] == stats["vectors"] == done["chunks_done"]


def test_backend_shutdown_stops_the_worker(env, kb_settings, docs_root, fake_ollama):
    fake_ollama.embed_delay_s = 1.0
    (docs_root / "big.md").write_text(topic_markdown("astronomy", 8, 4), encoding="utf-8")
    with TestClient(create_app(kb_settings(base_url=fake_ollama.url)), base_url="http://127.0.0.1") as client:
        create_collection(client)
        ingest(client, path="big.md")
        worker = wait_for(lambda: next((s for s in client.get("/api/processes").json()["supervised"] if s["role"] == "ingestion"), None),
                          what="the worker")
        pids = worker["job_pids"]
        assert pids and all(psutil.pid_exists(p) for p in pids)
    # leaving the block ran the backend's shutdown: the supervisor stopped the worker's whole job
    wait_for(lambda: not any(psutil.pid_exists(p) for p in pids), timeout=15, what="worker processes to end")


# ---------------------------------------------------------------------------------------------- kb.search capability


def test_the_search_capability_needs_a_grant_and_only_sees_granted_collections(make_client, docs_root):
    (docs_root / "m.md").write_text(topic_markdown("music", 2, 3), encoding="utf-8")
    (docs_root / "f.md").write_text(topic_markdown("finance", 2, 3), encoding="utf-8")
    ungranted = make_client()
    assert "kb.search" not in {c["id"] for c in ungranted.get("/api/capabilities").json()}  # deny by default
    granted = make_client('\n[[permissions.collections]]\nid = "public"\ngrants = ["kb.read"]\n')
    for name, path in (("public", "m.md"), ("private", "f.md")):
        create_collection(granted, name)
        wait_jobs(granted, [j["id"] for j in ingest(granted, path=path, collection=name).json()["jobs"]])
    cap = next(c for c in granted.get("/api/capabilities").json() if c["id"] == "kb.search")
    assert cap["available"] and "kb.read:public" in cap["permissions"] and cap["side_effects"] == "none" and cap["input_schema"]["properties"]["collection"]["enum"] == ["public"]
    ok = granted.post("/api/capabilities/kb.search/invoke", json={"args": {"query": "orchestra oboe violins fugue", "collection": "public"}})
    assert ok.status_code == 200 and ok.json()["result"]["results"][0]["document"] == "m.md" and ok.json()["result"]["sufficient"]
    denied = granted.post("/api/capabilities/kb.search/invoke", json={"args": {"query": "compound interest", "collection": "private"}})
    assert denied.status_code == 422 and denied.json()["error"]["code"] == "invalid_arguments"  # not an allowed value
    smuggle = granted.post("/api/capabilities/kb.search/invoke", json={"args": {"query": "x", "collection": "public", "collections": ["private"]}})
    assert smuggle.status_code == 422
    assert granted.post("/api/capabilities/kb.search/invoke", json={"args": {"query": "x" * 600, "collection": "public"}}).status_code == 422
    # the user's own search is not limited by the model-facing grants
    own = granted.post("/api/knowledge/search", json={"query": "compound interest portfolio", "collections": ["private"]}).json()
    assert own["results"][0]["document"] == "f.md"


# ---------------------------------------------------------------------------------------------- memory over HTTP


def test_memory_endpoints(make_client):
    client = make_client()
    created = client.post("/api/memory/items", json={"kind": "preference", "content": "Keep answers short."})
    assert created.status_code == 201 and created.json()["status"] == "approved" and created.json()["created_by"] == "user"
    for forged in ({"kind": "fact", "content": "x", "status": "approved"}, {"kind": "fact", "content": "x", "created_by": "model"},
                   {"kind": "fact", "content": "x", "model": "m"}, {"kind": "weird", "content": "x"}, {"kind": "fact", "content": ""}):
        assert client.post("/api/memory/items", json=forged).status_code == 422, forged
    item_id = created.json()["id"]
    app = client.app
    proposed = app.state.knowledge.memory.propose("fact", "Ann lives in Oslo.", source="task:ab12", model="qwen3:4b", prompt_version="extract-1")
    assert [i["id"] for i in client.get("/api/memory/items?status=proposed").json()] == [proposed["id"]]
    assert client.get("/api/memory/items?created_by=model&kind=fact").json()[0]["model"] == "qwen3:4b"
    assert client.post(f"/api/memory/items/{proposed['id']}/approve").json()["status"] == "approved"
    assert client.post(f"/api/memory/items/{proposed['id']}/approve").status_code == 409
    assert client.post(f"/api/memory/items/{proposed['id']}/reject").json()["status"] == "rejected"
    assert client.patch(f"/api/memory/items/{item_id}", json={"content": "Keep answers very short."}).json()["edited_by_user"] == 1
    assert client.patch(f"/api/memory/items/{item_id}", json={"content": "x", "status": "proposed"}).status_code == 422
    export = client.get("/api/memory/export").json()
    assert export["format"] == "xodus-memory-export" and {i["status"] for i in export["items"]} == {"approved", "rejected"}
    assert client.delete(f"/api/memory/items/{item_id}").json() == {"deleted": item_id}
    for missing in (client.get("/api/memory/items/9999"), client.delete("/api/memory/items/9999"), client.post("/api/memory/items/9999/approve")):
        assert missing.status_code == 404
    audit = [json.loads(line)["event"] for line in app.state.audit.path.read_text().splitlines() if line.startswith('{"seq')]
    assert {"memory_created", "memory_proposed", "memory_approved", "memory_rejected", "memory_edited", "memory_exported", "memory_deleted"} <= set(audit)


def test_pagination_is_bounded_everywhere(make_client):
    client = make_client()
    create_collection(client)
    for path in ("/api/knowledge/collections", "/api/knowledge/documents", "/api/knowledge/jobs", "/api/memory/items"):
        assert client.get(path + "?limit=0").status_code == 422
        assert client.get(path + "?limit=201").status_code == 422
        assert client.get(path + "?offset=-1").status_code == 422
        assert client.get(path + "?limit=200&offset=0").status_code == 200


def test_destructive_routes_refuse_cross_origin_requests(make_client, docs_root):
    client = make_client()
    coll = create_collection(client)
    evil = {"Origin": "http://evil.example"}
    assert client.delete(f"/api/knowledge/collections/{coll['id']}", headers=evil).status_code == 403
    assert client.post("/api/memory/items", json={"kind": "note", "content": "x"}, headers=evil).status_code == 403
    assert client.post("/api/knowledge/ingest", json={"collection": "main", "root": "docs"}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get(f"/api/knowledge/collections", headers=evil).status_code == 200  # reads are not state changes
    assert client.get("/api/knowledge/collections").json()[0]["name"] == "main"


def test_the_search_and_ask_routes_validate_their_input(make_client):
    client = make_client()
    create_collection(client)
    assert client.post("/api/knowledge/search", json={"query": ""}).status_code == 422
    assert client.post("/api/knowledge/search", json={"query": "x", "collections": ["nope"]}).status_code == 404
    assert client.post("/api/knowledge/search", json={"query": "x", "k": 999}).status_code == 422
    assert client.post("/api/knowledge/search", json={"query": "x" * 600}).json()["error"]["code"] == "invalid_query"
    assert client.post("/api/knowledge/ask", json={"question": "x", "collections": ["nope"]}).status_code == 404
    empty = client.post("/api/knowledge/search", json={"query": "anything at all"}).json()
    assert empty["results"] == [] and empty["sufficient"] is False  # an empty collection is not an error
