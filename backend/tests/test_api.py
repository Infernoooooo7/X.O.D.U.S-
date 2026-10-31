"""API tests. Ollama is replaced by an httpx MockTransport except where noted; the
'Ollama down' tests use a real closed loopback port."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import free_port
from xodus.api.app import create_app
from xodus.providers.ollama import OllamaProvider
from xodus.resources.admission import AdmissionGate, QueueFull
from tests.helpers import api_client


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def fake_ollama(chat_lines: list[dict] | None = None, chat_status: int = 200, chat_body: dict | None = None,
                capabilities: list[str] | None = None):
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.0-test"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "test-model:latest", "digest": "abc123", "size": 1}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": capabilities or ["completion", "tools"],
                                             "details": {"family": "test"}, "model_info": {"test.context_length": 32768}})
        if request.url.path == "/api/chat":
            calls.append(json.loads(request.content))
            if chat_status != 200:
                return httpx.Response(chat_status, json=chat_body or {})
            return httpx.Response(200, content="\n".join(json.dumps(x) for x in chat_lines or []).encode())
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


STREAM = [
    {"message": {"content": "Hel"}, "done": False},
    {"message": {"content": "lo"}, "done": False},
    {"done": True, "done_reason": "stop", "eval_count": 2, "eval_duration": 1_000_000_000,
     "prompt_eval_count": 9, "load_duration": 5_000_000},
]


def client_for(settings, transport=None) -> TestClient:
    provider = OllamaProvider(settings.ollama, transport=transport)
    return api_client(create_app(settings, provider=provider))


def test_health_ok_with_mock(make_settings):
    transport, _ = fake_ollama()
    with client_for(make_settings(), transport) as client:
        body = client.get("/api/health").json()
    assert body["backend"]["status"] == "ok"
    assert body["ollama"]["status"] == "ok" and body["ollama"]["digest"] == "abc123"
    # declared [completion, structured, tools]; Ollama reports completion + tools -> all three confirmed
    assert body["model_capabilities"]["confirmed"] == ["completion", "structured", "tools"]


def test_undeclared_or_unreported_capabilities_are_not_confirmed(make_settings):
    transport, _ = fake_ollama(capabilities=["completion"])  # Ollama does not report tool support
    with client_for(make_settings(), transport) as client:
        body = client.get("/api/health").json()
    assert body["model_capabilities"]["confirmed"] == ["completion", "structured"]


def test_chat_streams_tokens_then_done(make_settings):
    transport, calls = fake_ollama(STREAM)
    with client_for(make_settings(), transport) as client:
        with client.stream("POST", "/api/chat", json={"message": "hi", "num_ctx": 4096, "seed": 7}) as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            events = parse_sse(resp.read().decode())
    kinds = [e for e, _ in events]
    assert kinds == ["meta", "token", "token", "done"]
    assert "".join(d["t"] for e, d in events if e == "token") == "Hello"
    done = events[-1][1]
    assert done["usage"]["output_tokens"] == 2 and done["usage"]["tokens_per_s"] == 2.0
    assert done["timing"]["ttft_ms"] is not None and done["timing"]["total_ms"] >= done["timing"]["ttft_ms"]
    assert "uss_total_mib" in done["resources"]
    sent = calls[0]
    assert sent["options"]["num_ctx"] == 4096 and sent["options"]["seed"] == 7  # num_ctx always explicit
    assert sent["messages"][-1] == {"role": "user", "content": "hi"}


def test_chat_uses_default_num_ctx(make_settings):
    transport, calls = fake_ollama(STREAM)
    with client_for(make_settings(), transport) as client:
        client.post("/api/chat", json={"message": "hi"})
    assert calls[0]["options"]["num_ctx"] == 8192


@pytest.mark.parametrize("payload", [{"message": ""}, {"message": "hi", "num_ctx": 999}, {"message": "hi", "x": 1}])
def test_chat_rejects_bad_requests(make_settings, payload):
    transport, calls = fake_ollama(STREAM)
    with client_for(make_settings(), transport) as client:
        assert client.post("/api/chat", json=payload).status_code == 422
    assert calls == []


def test_missing_model_is_reported_clearly(make_settings):
    transport, _ = fake_ollama(chat_status=404, chat_body={"error": "model 'test-model:latest' not found"})
    with client_for(make_settings(), transport) as client:
        events = parse_sse(client.post("/api/chat", json={"message": "hi"}).text)
    assert events[-1][0] == "error" and events[-1][1]["code"] == "model_not_available"
    assert "ollama pull" in events[-1][1]["message"]


def test_ollama_down_gives_clear_error_and_backend_stays_up(make_settings):
    settings = make_settings(base_url=f"http://127.0.0.1:{free_port()}")  # nothing listens here
    with client_for(settings) as client:
        events = parse_sse(client.post("/api/chat", json={"message": "hi"}).text)
        assert [e for e, _ in events] == ["meta", "error"]
        err = events[1][1]
        assert err["code"] == "provider_unavailable"
        assert "not reachable" in err["message"] and "still running" in err["message"]
        health = client.get("/api/health").json()
        assert health["backend"]["status"] == "ok" and health["ollama"]["status"] == "unreachable"
        # and the backend keeps serving other routes
        assert client.get("/api/capabilities").status_code == 200


def test_foreign_host_header_is_refused(make_settings):
    transport, _ = fake_ollama()
    with client_for(make_settings(), transport) as client:
        assert client.get("/api/health", headers={"Host": "evil.example:8765"}).status_code == 400
        assert client.get("/api/health", headers={"Host": "localhost:8765"}).status_code == 200


def test_capability_endpoints(make_settings):
    transport, _ = fake_ollama()
    with client_for(make_settings(), transport) as client:
        caps = client.get("/api/capabilities").json()
        assert [c["id"] for c in caps] == ["fs.list_files"]
        ok = client.post("/api/capabilities/fs.list_files/invoke", json={"args": {"path": "sub"}})
        assert ok.status_code == 200 and ok.json()["result"]["entries"][0]["name"] == "b.txt"
        denied = client.post("/api/capabilities/fs.list_files/invoke", json={"args": {"path": "../outside"}})
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "path_violation"
        missing = client.post("/api/capabilities/nope/invoke", json={"args": {}})
        assert missing.status_code == 404
        extra = client.post("/api/capabilities/fs.list_files/invoke", json={"args": {"path": ".", "root": "C:\\"}})
        assert extra.status_code == 422 and extra.json()["error"]["code"] == "invalid_arguments"


def test_logs_written_as_json(make_settings, tmp_path):
    from xodus.obs import configure_logging

    settings = make_settings()
    configure_logging(settings.log_dir)
    transport, _ = fake_ollama(STREAM)
    with client_for(settings, transport) as client:
        client.post("/api/chat", json={"message": "hi"})
    records = [json.loads(line) for line in (settings.log_dir / "xodus.jsonl").read_text().splitlines()]
    chat = [r for r in records if r["event"] == "chat_request"]
    assert chat and chat[0]["outcome"] == "ok" and "ttft_ms" in chat[0]["timing"]
    assert "processes" in chat[0]["resource_snapshot"]


async def test_admission_gate_bounds_queue():
    gate = AdmissionGate(max_running=1, max_queued=1)
    release = asyncio.Event()

    async def hold():
        async with gate.slot():
            await release.wait()

    first = asyncio.create_task(hold())
    await asyncio.sleep(0)
    second = asyncio.create_task(hold())  # queued
    await asyncio.sleep(0)
    assert gate.running == 1 and gate.waiting == 1
    with pytest.raises(QueueFull):
        async with gate.slot():
            pass
    release.set()
    await asyncio.gather(first, second)
    assert gate.running == 0 and gate.waiting == 0
