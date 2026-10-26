"""Task, approval, process and audit endpoints (scripted model), plus cross-origin refusal."""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from tests.helpers import ScriptedProvider, WriteNote, tool_call
from xodus.api.app import create_app
from xodus.providers.base import Completion


def parse_sse(body: str) -> list[dict]:
    return [json.loads(block.split("data: ", 1)[1]) for block in body.strip().split("\n\n") if "data: " in block]


def client_for(settings, provider, extra=None):
    app = create_app(settings, provider=provider)
    for cap in extra or []:
        app.state.registry.register(cap, "test")
    return TestClient(app, base_url="http://127.0.0.1")


def wait_status(client, task_id, statuses, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/tasks/{task_id}").json()
        if body["status"] in statuses:
            return body
        time.sleep(0.05)
    raise AssertionError(f"task stuck in {body['status']}")


def test_task_runs_and_streams_events(make_settings):
    provider = ScriptedProvider(tool_turns=[tool_call("fs__list_files", {"path": "sub"})], answer="b.txt is there")
    with client_for(make_settings(), provider) as client:
        created = client.post("/api/tasks", json={"goal": "What is in sub?"})
        assert created.status_code == 202 and created.json()["arm"] == "react"
        task_id = created.json()["task_id"]
        events = parse_sse(client.get(f"/api/tasks/{task_id}/events").text)  # follows until the task ends
        assert events[0]["type"] == "task_started" and events[-1]["type"] == "task_finished"
        assert events[-1]["status"] == "completed" and events[-1]["answer"] == "b.txt is there"
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
        replay = parse_sse(client.get(f"/api/tasks/{task_id}/events?after=3").text)
        assert replay[0]["seq"] == 4


def test_approval_flow_over_http(make_settings, sandbox):
    settings = make_settings(extra='\n[[permissions.roots]]\nid = "notes"\npath = "%s"\ngrants = ["fs.read", "fs.write"]\n'
                                   % sandbox.as_posix())
    writer = WriteNote("notes", sandbox)
    provider = ScriptedProvider(tool_turns=[tool_call("test__write_note", {"path": "n.txt", "text": "hi"}), Completion("Saved.")])
    with client_for(settings, provider, extra=[writer]) as client:
        task_id = client.post("/api/tasks", json={"goal": "save a note"}).json()["task_id"]
        wait_status(client, task_id, {"awaiting_approval"})
        pending = client.get("/api/approvals").json()
        assert len(pending) == 1
        approval = pending[0]
        assert approval["args"] == {"path": "n.txt", "text": "hi"} and approval["undoable"] is False
        wrong = client.post(f"/api/approvals/{approval['id']}", json={"decision": "approve", "action_hash": "0" * 64})
        assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "hash_mismatch"
        assert writer.executions == 0
        ok = client.post(f"/api/approvals/{approval['id']}", json={"decision": "approve", "action_hash": approval["action_hash"]})
        assert ok.status_code == 200
        replay = client.post(f"/api/approvals/{approval['id']}", json={"decision": "approve", "action_hash": approval["action_hash"]})
        assert replay.status_code == 409 and replay.json()["error"]["code"] == "already_resolved"
        assert wait_status(client, task_id, {"completed"})["steps"][0]["status"] == "ok"
        assert (sandbox / "n.txt").read_text() == "hi" and writer.executions == 1
        assert client.get("/api/audit/verify").json()["ok"] is True


def test_direct_invoke_refuses_side_effects(make_settings, sandbox):
    settings = make_settings(extra='\n[[permissions.roots]]\nid = "notes"\npath = "%s"\ngrants = ["fs.write"]\n' % sandbox.as_posix())
    writer = WriteNote("notes", sandbox)
    with client_for(settings, ScriptedProvider(), extra=[writer]) as client:
        r = client.post("/api/capabilities/test.write_note/invoke", json={"args": {"path": "x.txt", "text": "y"}})
        assert r.status_code == 409 and r.json()["error"]["code"] == "approval_required"
    assert writer.executions == 0 and not (sandbox / "x.txt").exists()


def test_cancel_endpoint(make_settings, sandbox):
    settings = make_settings(extra='\n[[permissions.roots]]\nid = "notes"\npath = "%s"\ngrants = ["fs.write"]\n' % sandbox.as_posix())
    writer = WriteNote("notes", sandbox)
    provider = ScriptedProvider(tool_turns=[tool_call("test__write_note", {"path": "n.txt", "text": "hi"})])
    with client_for(settings, provider, extra=[writer]) as client:
        task_id = client.post("/api/tasks", json={"goal": "save"}).json()["task_id"]
        wait_status(client, task_id, {"awaiting_approval"})
        assert client.post(f"/api/tasks/{task_id}/cancel").json()["status"] == "cancelled"
        assert client.get("/api/approvals").json() == []  # withdrawn
        assert client.post(f"/api/tasks/{task_id}/cancel").status_code == 409
    assert writer.executions == 0


def test_task_limits_and_validation(make_settings):
    provider = ScriptedProvider(tool_turns=[tool_call("test__nothing", {})] * 3)
    with client_for(make_settings(extra="\n[executor]\nmax_active_tasks = 1\n"), provider) as client:
        assert client.post("/api/tasks", json={"goal": "x", "arm": "yolo"}).status_code == 422
        assert client.post("/api/tasks", json={"goal": "x", "max_steps": 100}).status_code == 422  # no budget override
        assert client.get("/api/tasks/nope").status_code == 404


def test_cross_origin_state_changes_are_refused(make_settings):
    with client_for(make_settings(), ScriptedProvider()) as client:
        evil = {"Origin": "http://evil.example"}
        assert client.post("/api/tasks", json={"goal": "x"}, headers=evil).status_code == 403
        assert client.post("/api/approvals/abc", json={"decision": "approve", "action_hash": "x"}, headers=evil).status_code == 403
        assert client.post("/api/tasks", json={"goal": "x"}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert client.post("/api/tasks", json={"goal": "x"}, headers={"Origin": "null"}).status_code == 403
        same = {"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"}
        assert client.post("/api/tasks", json={"goal": "x"}, headers=same).status_code == 202
        assert client.get("/api/health", headers=evil).status_code == 200  # reads are not state-changing
        form = client.post("/api/tasks", content="goal=x", headers={"Content-Type": "text/plain"})
        assert form.status_code == 422  # a simple (no-preflight) cross-site form body is not accepted as JSON


def test_processes_and_rejected_endpoints(make_settings):
    with client_for(make_settings(), ScriptedProvider()) as client:
        body = client.get("/api/processes").json()
        assert body["supervised"] == []
        assert any(p["group"] == "xodus_app" and p["tier"] == "core" for p in body["processes"])
        assert "browser" in body["excluded"]
        assert client.get("/api/capabilities/rejected").json() == []
