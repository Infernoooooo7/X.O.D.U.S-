"""Routes behind the Gate E views: resources and thresholds, process control, ingestion pause, audit log, experiments,
settings and the extension inventory."""

from __future__ import annotations

import json
import sys
import time

import psutil
import pytest
from fastapi.testclient import TestClient

from tests.kb_helpers import topic_markdown
from tests.test_knowledge_api import create_collection, ingest, wait_for, wait_jobs
from xodus.api import workspace
from xodus.api.app import create_app
from xodus.config.settings import ThresholdSettings
from xodus.resources.thresholds import evaluate
from xodus.security.audit import AuditLog, read_recent

GB = 10**9


# ---------------------------------------------------------------------------------------------- thresholds


def snap(uss_gb: float, vram_gb: float | None, available_gb: float, complete: bool = True) -> dict:
    return {"totals": {"uss": int(uss_gb * GB)}, "uss_complete": complete,
            "device_vram": None if vram_gb is None else {"used": int(vram_gb * GB), "total": 6 * GB},
            "system_memory": {"available": int(available_gb * GB)}}


@pytest.mark.parametrize("uss, vram, avail, level, metric_levels", [
    (1.0, 1.5, 8.0, "ok", ["ok", "ok", "ok"]),
    (3.7, 1.5, 8.0, "warn", ["warn", "ok", "ok"]),
    (1.0, 4.3, 8.0, "warn", ["ok", "warn", "ok"]),
    (5.6, 1.5, 8.0, "stop", ["stop", "ok", "ok"]),
    (1.0, 4.9, 8.0, "stop", ["ok", "stop", "ok"]),
    (1.0, 1.5, 1.2, "stop", ["ok", "ok", "stop"]),
    (1.0, None, 8.0, "ok", ["ok", "unknown", "ok"]),
])
def test_threshold_levels(uss, vram, avail, level, metric_levels):
    result = evaluate(snap(uss, vram, avail), ThresholdSettings())
    assert result["level"] == level and [c["level"] for c in result["checks"]] == metric_levels
    assert result["enforced"] is False and "Gate F" in result["enforcement_note"]


def test_incomplete_uss_is_unknown_not_low():
    result = evaluate(snap(9.0, 1.0, 8.0, complete=False), ThresholdSettings())
    assert result["checks"][0]["level"] == "unknown" and result["checks"][0]["value_gb"] is None
    assert "incomplete" in result["checks"][0]["note"]


def test_warn_must_be_below_stop():
    with pytest.raises(ValueError):
        ThresholdSettings(warn_uss_gb=6.0, stop_uss_gb=5.5)


# ---------------------------------------------------------------------------------------------- resources and processes


@pytest.fixture
def client(make_settings, fake_ollama, monkeypatch):
    monkeypatch.setenv("XODUS_SUPERVISOR__REAPER_INTERVAL_S", "0.3")
    opened = []

    def make(extra: str = "", settings=None) -> TestClient:
        c = TestClient(create_app(settings or make_settings(extra=extra, base_url=fake_ollama.url)), base_url="http://127.0.0.1")
        c.__enter__()
        opened.append(c)
        return c

    yield make
    for c in opened:
        c.__exit__(None, None, None)


def launch_sleeper(c, tier: str = "optional"):
    return c.app.state.supervisor.launch([sys.executable, "-c", "import time; time.sleep(60)"], role="plugin", owner="test.sleeper",
                                         start_reason="test", cwd=c.app.state.db.path.parent, env=None, tier=tier, pipes=False)


def test_resource_view_lists_every_line_and_process_with_its_tier(client):
    c = client()
    mp = launch_sleeper(c)
    view = c.get("/api/resources").json()
    lines = {line["line"]: line for line in view["lines"]}
    assert set(lines) == {"xodus_app", "ollama", "supporting", "optional", "system", "browser"}
    assert lines["optional"]["processes"] >= 1 and lines["optional"]["uss_mib"] > 0
    assert lines["browser"]["included"] is False and "excluded" in lines["browser"]["note"]
    assert "SQLite" in lines["supporting"]["note"]
    row = next(p for p in view["processes"] if p["pid"] == mp.pid)
    assert row["tier"] == "optional" and row["owner"] == "test.sleeper" and row["supervised"] and row["uss_mib"] > 0
    backend = [p for p in view["processes"] if p["group"] == "xodus_app"]
    assert backend and all(p["tier"] == "core" for p in backend)
    assert view["thresholds"]["level"] in ("ok", "warn", "stop", "unknown") and view["thresholds"]["enforced"] is False
    assert view["limits"]["max_optional_processes"] == 2 and view["totals"]["included"] == "core and optional tiers"
    assert view["measurement"]["sample_ms"] > 0


def test_stopping_an_optional_process_and_refusing_a_core_one(client):
    c = client()
    mp = launch_sleeper(c)
    assert c.post(f"/api/processes/{mp.pid}/stop", headers={"Origin": "http://evil.example"}).status_code == 403
    r = c.post(f"/api/processes/{mp.pid}/stop")
    assert r.status_code == 200 and r.json()["owner"] == "test.sleeper"
    wait_for(lambda: not psutil.pid_exists(mp.pid), 10, what="the process to end")
    core = launch_sleeper(c, tier="core")
    r = c.post(f"/api/processes/{core.pid}/stop")
    assert r.status_code == 409 and "pause ingestion" in r.json()["detail"]
    assert psutil.pid_exists(core.pid)
    assert c.post(f"/api/processes/{psutil.Process().pid}/stop").status_code == 404  # not launched by XODUS: never touched
    audit = [json.loads(line) for line in c.app.state.audit.path.read_text(encoding="utf-8").splitlines()]
    assert any(a["event"] == "process_stopped_by_user" and a["pid"] == mp.pid for a in audit)


def test_pausing_ingestion_stops_the_worker_and_resuming_continues_from_the_checkpoint(kb_settings, fake_ollama, docs_root,
                                                                                        monkeypatch):
    monkeypatch.setenv("XODUS_INGESTION__MONITOR_INTERVAL_S", "0.2")
    fake_ollama.embed_delay_s = 0.4
    (docs_root / "big.md").write_text(topic_markdown("astronomy", 8, 4), encoding="utf-8")
    settings = kb_settings(base_url=fake_ollama.url)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        create_collection(c)
        job = ingest(c, path="big.md").json()["jobs"][0]
        wait_for(lambda: len(fake_ollama.embed_calls) >= 2, what="two batches")
        pid = c.get("/api/knowledge/stats").json()["ingestion"]["worker"]["pid"]
        paused = c.post("/api/ingestion/pause").json()
        assert paused["paused"] and paused["requeued_jobs"] == [job["id"]]
        wait_for(lambda: not psutil.pid_exists(pid), 10, what="the worker to stop")
        state = c.get(f"/api/knowledge/jobs/{job['id']}").json()
        assert state["status"] == "queued" and state["attempts"] == 0  # a pause is not a failed attempt
        done_before = state["chunks_done"]
        time.sleep(1.5)  # the monitor runs every 0.2 s: it must not restart the worker while paused
        assert c.get("/api/knowledge/stats").json()["ingestion"]["worker"]["running"] is False
        assert c.get("/api/resources").json()["ingestion"]["paused"] is True
        (docs_root / "more.md").write_text(topic_markdown("music", 1, 2), encoding="utf-8")
        queued = ingest(c, path="more.md")
        assert queued.status_code == 202 and queued.json()["paused"] is True
        time.sleep(0.6)
        assert c.get("/api/knowledge/stats").json()["ingestion"]["worker"]["running"] is False
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:  # still paused after a restart
        time.sleep(0.6)
        assert c.get("/api/knowledge/stats").json()["ingestion"]["paused"] is True
        assert c.get("/api/knowledge/stats").json()["ingestion"]["worker"]["running"] is False
        fake_ollama.embed_delay_s = 0
        assert c.post("/api/ingestion/resume").json()["worker_started"] is True
        jobs = wait_jobs(c, [job["id"], queued.json()["jobs"][0]["id"]], timeout=60)
        assert [j["status"] for j in jobs] == ["completed", "completed"]
        assert jobs[0]["attempts"] == 1 and jobs[0]["chunks_done"] >= done_before
        stats = c.get("/api/knowledge/stats").json()
        assert stats["chunks"] == stats["vectors"] == sum(j["chunks_done"] for j in jobs)
        events = [json.loads(line)["event"] for line in c.app.state.audit.path.read_text(encoding="utf-8").splitlines()]
        assert "ingestion_paused" in events and "ingestion_resumed" in events


# ---------------------------------------------------------------------------------------------- audit log reader


def test_read_recent_walks_backwards_across_blocks(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(400):
        log.append("tick" if i % 2 else "tock", n=i, filler="x" * 300)  # ~150 KB: several 64 KB blocks
    newest = read_recent(log.path, 5)
    assert [r["seq"] for r in newest] == [400, 399, 398, 397, 396]
    seen, before = [], None
    while True:
        page = read_recent(log.path, 50, before)
        if not page:
            break
        seen += [r["seq"] for r in page]
        before = page[-1]["seq"]
    assert seen == list(range(400, 0, -1))  # every record once, in order, across block boundaries
    assert {r["event"] for r in read_recent(log.path, 30, event_prefix="tic")} == {"tick"}


def test_read_recent_skips_damaged_lines(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text('{"seq": 1, "event": "a"}\nnot json at all\n[1, 2]\n\n{"seq": 2, "event": "b"}\n', encoding="utf-8")
    assert [r["seq"] for r in read_recent(path, 10)] == [2, 1]
    assert read_recent(tmp_path / "missing.jsonl", 10) == []


def test_audit_api_pages_and_filters(client):
    c = client()
    for i in range(3):
        c.post("/api/memory/items", json={"kind": "note", "content": f"note {i}"})  # audited as memory_created
    c.delete(f"/api/conversations/{c.post('/api/conversations', json={'title': 'x'}).json()['id']}")
    page = c.get("/api/audit?limit=2").json()
    assert len(page["records"]) == 2 and page["next_before"] == page["records"][-1]["seq"]
    assert page["records"][0]["seq"] == page["head_seq"]
    rest = c.get(f"/api/audit?limit=200&before={page['next_before']}").json()["records"]
    assert all(r["seq"] < page["next_before"] for r in rest)
    only = c.get("/api/audit?event=conversation_").json()["records"]
    assert [r["event"] for r in only] == ["conversation_deleted"]
    assert c.get("/api/audit?event=Bad;Event").status_code == 422 and c.get("/api/audit?limit=999").status_code == 422


# ---------------------------------------------------------------------------------------------- experiments


@pytest.fixture
def experiments(tmp_path, monkeypatch):
    root = tmp_path / "experiments"
    run = root / "results" / "smoke-1"
    run.mkdir(parents=True)
    (run / "summary.md").write_text("# Smoke\n\nAll good.", encoding="utf-8")
    (run / "results.json").write_text('{"ok": true}', encoding="utf-8")
    (run / "shot.png").write_bytes(b"\x89PNG")
    (run / "big.txt").write_text("y" * 1_200_000, encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside", encoding="utf-8")
    monkeypatch.setattr(workspace, "EXPERIMENTS_DIR", root)
    return root


def test_experiment_results_are_listed_and_readable(client, experiments):
    c = client()
    body = c.get("/api/experiments").json()
    assert body["preregistration"].startswith("not written yet") and [r["name"] for r in body["runs"]] == ["smoke-1"]
    files = {f["name"]: f for f in body["runs"][0]["files"]}
    assert files["summary.md"]["viewable"] and not files["shot.png"]["viewable"]
    assert c.get("/api/experiments/file", params={"path": "results/smoke-1/summary.md"}).json()["text"].startswith("# Smoke")
    big = c.get("/api/experiments/file", params={"path": "results/smoke-1/big.txt"}).json()
    assert big["truncated"] and len(big["text"]) == 1_000_000
    assert c.get("/api/experiments/file", params={"path": "results/smoke-1/shot.png"}).status_code == 415


@pytest.mark.parametrize("path", ["../secret.txt", "results/../../secret.txt", "C:\\Windows\\win.ini", "\\\\server\\share\\x.md",
                                  "results/smoke-1/summary.md:stream", "/etc/passwd", "results/nope.md"])
def test_experiment_files_cannot_be_read_outside_the_folder(client, experiments, path):
    r = client().get("/api/experiments/file", params={"path": path})
    assert r.status_code in (403, 404) and "outside" not in r.text


# ---------------------------------------------------------------------------------------------- static UI files


def test_segment_prefetch_files_are_found_and_stay_inside_the_ui_folder(make_settings, fake_ollama, tmp_path, monkeypatch):
    """Next.js 16 asks for '<view>/__next.<view>.__PAGE__.txt' but exports '<view>/__next.<view>/__PAGE__.txt'."""
    from xodus.api import app as app_module

    ui = tmp_path / "ui"
    (ui / "history" / "__next.history").mkdir(parents=True)
    (ui / "index.html").write_text("<p>home</p>", encoding="utf-8")
    (ui / "history" / "index.html").write_text("<p>history</p>", encoding="utf-8")
    (ui / "history" / "__next.history" / "__PAGE__.txt").write_text("segment payload", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside the UI folder", encoding="utf-8")
    monkeypatch.setattr(app_module, "UI_DIR", ui)
    with TestClient(create_app(make_settings(base_url=fake_ollama.url)), base_url="http://127.0.0.1") as c:
        assert c.get("/history/__next.history.__PAGE__.txt").text == "segment payload"
        assert c.get("/history/").text == "<p>history</p>"
        for probe in ("/history/__next.missing.__PAGE__.txt", "/__next...secret.txt", "/history/__next....secret.txt",
                      "/history/__next.%2e%2e.secret.txt", "/history/__next..%2F..%2Fsecret.txt", "/__next.history.txt"):
            r = c.get(probe)
            assert r.status_code == 404 and "outside" not in r.text, probe


# ---------------------------------------------------------------------------------------------- settings and extensions


def test_settings_view_is_read_only_and_explains_how_to_change(client):
    c = client()
    body = c.get("/api/settings").json()
    assert body["roots"][0]["id"] == "sandbox" and body["roots"][0]["grants"] == ["fs.read"]
    assert len(body["config_hash"]) == 16 and "local.toml" in body["how_to_change"]
    assert body["thresholds"]["warn_uss_gb"] == 3.6 and body["effective"]["server"]["host"] == "127.0.0.1"
    assert c.post("/api/settings", json={}).status_code == 405  # there is no way to change settings over HTTP


def test_extension_inventory_lists_plugins_and_rejections(client, tmp_path):
    plugins = tmp_path / "plugins"
    (plugins / "broken").mkdir(parents=True)
    (plugins / "broken" / "manifest.json").write_text('{"kind": "mcp_server", "id": "Bad"}', encoding="utf-8")
    c = client()
    body = c.get("/api/extensions").json()
    assert body["extensions"] == [] and body["max_optional_processes"] == 2
    rejected = body["rejected"][0]
    assert rejected["source"] == "mcp_server" and rejected["ref"] == "broken" and rejected["reasons"]
    assert "not a sandbox" in body["note"]
