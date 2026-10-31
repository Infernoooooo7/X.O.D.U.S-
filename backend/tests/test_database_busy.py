"""Failure injection F2: another program holds the database's write lock. Writes get a clear refusal, reads keep working,
nothing is corrupted, and everything works again once the lock is gone."""

from __future__ import annotations

import sqlite3

from tests.helpers import ScriptedProvider, api_client
from xodus.api.app import create_app
from xodus.providers.ollama import unload_embedder_sync


def test_a_locked_database_gets_a_clear_refusal_and_recovers(make_settings):
    settings = make_settings(extra="\n[storage]\nbusy_timeout_ms = 200\n")
    with api_client(create_app(settings, provider=ScriptedProvider())) as client:
        holder = sqlite3.connect(settings.data_dir / settings.storage.filename, timeout=1, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            refused = client.post("/api/conversations", json={"title": "while locked"})
            assert refused.status_code == 503 and refused.headers["Retry-After"] == "5"
            error = refused.json()["error"]
            assert error["code"] == "database_busy" and "not saved" in error["message"] and "xodus.db" in error["message"]
            assert client.get("/api/conversations").status_code == 200  # readers are not blocked (WAL)
            assert client.get("/api/health").status_code == 200  # the backend stays up
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        assert client.post("/api/conversations", json={"title": "after release"}).status_code == 201
        assert [c["title"] for c in client.get("/api/conversations").json()] == ["after release"]  # nothing half-written
        assert client.get("/api/knowledge/stats").status_code == 200


def test_unloading_the_embedder_never_loads_it_first(make_settings, fake_ollama):
    """Ollama loads a model to serve even a keep_alive 0 request, by default on the GPU (seen as a 1 GB spike), so a model
    that is not resident gets no request at all."""
    settings = make_settings(base_url=fake_ollama.url)
    fake_ollama.loaded.clear()
    assert unload_embedder_sync(settings.ollama) is True and fake_ollama.embed_calls == []
    fake_ollama.loaded[settings.ollama.embed_model] = 1
    assert unload_embedder_sync(settings.ollama) is True and fake_ollama.embed_calls[-1]["input"] == []


def test_unloading_with_ollama_down_reports_failure(make_settings):
    assert unload_embedder_sync(make_settings(base_url="http://127.0.0.1:9").ollama, timeout_s=0.5) is False
