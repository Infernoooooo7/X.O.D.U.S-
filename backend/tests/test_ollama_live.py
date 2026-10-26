"""Integration tests against the real local Ollama server (skipped when it is not running)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.test_api import parse_sse
from xodus.api.app import create_app

pytestmark = pytest.mark.ollama


def test_configured_model_present_with_pinned_digest(real_settings):
    with TestClient(create_app(real_settings), base_url="http://127.0.0.1") as client:
        ollama = client.get("/api/health").json()["ollama"]
    assert ollama["status"] == "ok", ollama
    assert ollama["digest"].startswith(real_settings.ollama.expected_digest_prefix)


def test_real_streamed_chat(real_settings):
    with TestClient(create_app(real_settings), base_url="http://127.0.0.1") as client:
        body = client.post("/api/chat", json={"message": "Reply with exactly the word: ready", "seed": 1}).text
    events = parse_sse(body)
    kinds = [e for e, _ in events]
    assert kinds[0] == "meta" and kinds[-1] == "done", events[-1]
    assert kinds.count("token") >= 1
    text = "".join(d["t"] for e, d in events if e == "token")
    assert "ready" in text.lower()
    done = events[-1][1]
    assert done["usage"]["output_tokens"] >= 1
    assert done["resources"]["uss_ollama_mib"] and done["resources"]["device_vram_peak_mib"]
