"""The Ollama cloud guard (master prompt rules 13-14, 3.8 and section 8): XODUS refuses models that Ollama would run on
another computer, keeps document text away from a remote embedder, and reports whether the running Ollama server was
started with its cloud features off, on loopback, without remote hosts and without wildcard origins."""

from __future__ import annotations

import os
from types import SimpleNamespace

import psutil
import pytest
from fastapi.testclient import TestClient

from xodus.api.app import create_app
from xodus.ingestion.pipeline import IngestError, _embed
from xodus.providers.base import RemoteModelRefused
from xodus.providers.ollama import SyncOllamaEmbedder
from xodus.providers.ollama_env import check_ollama_environment

REMOTE = {"remote_model": "gpt-oss:120b", "remote_host": "https://ollama.com:443"}
GOOD_ENV = {"OLLAMA_HOST": "127.0.0.1:11434", "OLLAMA_NO_CLOUD": "1", "OLLAMA_NUM_PARALLEL": "1"}


def listening(*ips: str, pid: int = 4242, port: int = 11434):
    """psutil.net_connections() rows for a server listening on `ips`, plus an unrelated connection."""
    rows = [SimpleNamespace(fd=-1, family=2, type=1, laddr=SimpleNamespace(ip=ip, port=port), raddr=(), status="LISTEN", pid=pid)
            for ip in ips]
    rows.append(SimpleNamespace(fd=-1, family=2, type=1, laddr=SimpleNamespace(ip="127.0.0.1", port=51000),
                                raddr=SimpleNamespace(ip="127.0.0.1", port=port), status="ESTABLISHED", pid=9999))
    return lambda: rows


def check(conns, env, port: int = 11434):
    def environ(pid: int) -> dict[str, str]:
        assert pid == 4242  # the listener's environment, not some other process's
        if isinstance(env, Exception):
            raise env
        return env

    return check_ollama_environment(port, connections=conns, environ=environ)


def failing(result) -> list[str]:
    return [c["id"] for c in result["checks"] if c["ok"] is False]


# ---------------------------------------------------------------------------------------------- environment check


def test_a_server_started_like_the_start_script_passes():
    result = check(listening("127.0.0.1"), GOOD_ENV)
    assert result["status"] == "ok" and result["pid"] == 4242
    assert {c["id"]: c["ok"] for c in result["checks"]} == {"loopback": True, "no_cloud": True, "remotes": True,
                                                            "origins": True}


@pytest.mark.parametrize("env, failed", [
    ({"OLLAMA_HOST": "127.0.0.1:11434"}, ["no_cloud"]),
    ({**GOOD_ENV, "OLLAMA_NO_CLOUD": "0"}, ["no_cloud"]),
    ({**GOOD_ENV, "OLLAMA_NO_CLOUD": "false"}, ["no_cloud"]),
    ({**GOOD_ENV, "OLLAMA_NO_CLOUD": ""}, ["no_cloud"]),
    ({**GOOD_ENV, "OLLAMA_REMOTES": "ollama.com"}, ["remotes"]),
    ({**GOOD_ENV, "OLLAMA_ORIGINS": "*"}, ["origins"]),
    ({**GOOD_ENV, "OLLAMA_ORIGINS": "http://localhost:3000,chrome-extension://*"}, ["origins"]),
    ({"OLLAMA_REMOTES": "a.example", "OLLAMA_ORIGINS": "*"}, ["no_cloud", "remotes", "origins"]),
])
def test_each_unsafe_setting_is_reported(env, failed):
    result = check(listening("127.0.0.1"), env)
    assert result["status"] == "problem" and failing(result) == failed
    assert "start-ollama.ps1" in result["fix"]


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "t", " 1 ", '"1"'])
def test_spellings_of_true_that_ollama_accepts_count_as_cloud_off(value):
    assert check(listening("127.0.0.1"), {**GOOD_ENV, "OLLAMA_NO_CLOUD": value})["status"] == "ok"


def test_harmless_origins_are_accepted():
    result = check(listening("127.0.0.1"), {**GOOD_ENV, "OLLAMA_ORIGINS": "http://127.0.0.1:8765"})
    assert result["status"] == "ok"


@pytest.mark.parametrize("ips", [("0.0.0.0",), ("::",), ("127.0.0.1", "192.168.1.20")])
def test_listening_beyond_loopback_is_reported(ips):
    result = check(listening(*ips), GOOD_ENV)
    assert result["status"] == "problem" and failing(result) == ["loopback"]


def test_ipv6_loopback_is_loopback():
    assert check(listening("127.0.0.1", "::1"), GOOD_ENV)["status"] == "ok"


def test_nothing_listening_is_unknown_not_ok():
    result = check(listening(port=12345), GOOD_ENV)
    assert result["status"] == "unknown" and result["pid"] is None and "11434" in result["message"]


def test_an_unreadable_environment_is_unknown_but_the_address_is_still_checked():
    result = check(listening("127.0.0.1"), psutil.AccessDenied(4242))
    assert result["status"] == "unknown"
    assert {c["id"]: c["ok"] for c in result["checks"]} == {"loopback": True, "no_cloud": None, "remotes": None,
                                                            "origins": None}
    exposed = check(listening("0.0.0.0"), psutil.AccessDenied(4242))
    assert exposed["status"] == "problem" and failing(exposed) == ["loopback"]


# ---------------------------------------------------------------------------------------------- through the API


def test_health_reads_the_real_ollama_process_environment(make_settings, fake_ollama, monkeypatch):
    """The fake Ollama listens inside this pytest process, so the check reads this process's real environment."""
    with TestClient(create_app(make_settings(base_url=fake_ollama.url)), base_url="http://127.0.0.1") as c:
        good = c.get("/api/health").json()["ollama_environment"]
        assert good["status"] == "ok" and good["pid"] == os.getpid()
        monkeypatch.delenv("OLLAMA_NO_CLOUD")
        monkeypatch.setenv("OLLAMA_ORIGINS", "*")
        bad = c.get("/api/health").json()["ollama_environment"]
        assert bad["status"] == "problem" and failing(bad) == ["no_cloud", "origins"]


def test_a_remote_embedding_model_is_reported_at_startup(make_settings, fake_ollama):
    fake_ollama.models = [{"name": "test-model:latest", "digest": "aaa", "size": 1},
                          {"name": "qwen3-embedding:0.6b", "digest": "bbb", "size": 384, **REMOTE}]
    with TestClient(create_app(make_settings(base_url=fake_ollama.url)), base_url="http://127.0.0.1") as c:
        health = c.get("/api/health").json()
        assert health["knowledge"]["embedding"]["status"] == "remote_model"
        assert health["ollama"]["status"] == "remote_model"
    assert fake_ollama.embed_calls == []


def test_the_ingestion_embedder_never_sends_text_to_a_remote_model(kb_settings, fake_ollama):
    settings = kb_settings(base_url=fake_ollama.url)
    fake_ollama.models = [{"name": settings.ollama.embed_model, "digest": "bbb", "size": 384, **REMOTE}]
    embedder = SyncOllamaEmbedder(settings.ollama, settings.embedding)
    try:
        with pytest.raises(RemoteModelRefused, match="https://ollama.com:443"):
            embedder.embed(["private document text"])
    finally:
        embedder.close()
    assert fake_ollama.embed_calls == []


def test_the_ingestion_embedder_still_embeds_with_a_local_model(kb_settings, fake_ollama):
    settings = kb_settings(base_url=fake_ollama.url)
    fake_ollama.models = [{"name": settings.ollama.embed_model, "digest": "bbb", "size": 600_000_000}]
    embedder = SyncOllamaEmbedder(settings.ollama, settings.embedding)
    try:
        assert len(embedder.embed(["a", "b"])) == 2 and len(embedder.embed(["c"])) == 1
    finally:
        embedder.close()
    assert len(fake_ollama.embed_calls) == 2


def test_a_remote_embedder_fails_the_ingestion_job_without_retrying(kb_settings):
    calls = []

    class Remote:
        def embed(self, texts):
            calls.append(texts)
            raise RemoteModelRefused("Model 'x' does not run on this computer.")

    deps = SimpleNamespace(settings=kb_settings(), embedder=Remote(), sleep=lambda s: None,
                           progress=SimpleNamespace(touch=lambda: None))
    with pytest.raises(IngestError) as exc:
        _embed(deps, ["text"])
    assert exc.value.code == "embedding_model_remote" and exc.value.retryable is False and len(calls) == 1
