"""Model profiles and switching (master prompt section 6, resource rule 4), against a stateful fake Ollama."""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from xodus.api.app import create_app
from xodus.config.settings import ConfigError

PROFILES = """
[models]
unload_wait_s = 1.0
[[models.profiles]]
id = "small-ctx"
model = "test-model:latest"
num_ctx = 4096
[[models.profiles]]
id = "other"
label = "Other model"
model = "other-model:latest"
[[models.profiles]]
id = "missing"
model = "not-downloaded:latest"
[[models.profiles]]
id = "off"
model = "other-model:latest"
enabled = false
note = "does not fit"
[[models.profiles]]
id = "pinned"
model = "other-model:latest"
expected_digest_prefix = "zzz"
[[models.profiles]]
id = "aliased"
model = "alias-model:latest"
"""
MB = 10**6


class FakeNvml:
    """Device memory as the fake Ollama's loaded models would use it, plus a fixed desktop share."""

    def __init__(self, fake, desktop: int = 500 * MB, total: int = 6000 * MB) -> None:
        self.fake, self.desktop, self.total = fake, desktop, total
        self.error = None
        self.handle = object()

    def memory(self):
        used = self.desktop + sum(self.fake.loaded.values())
        return {"total": self.total, "used": used, "free": self.total - used}

    def close(self):
        pass


@pytest.fixture
def fake(fake_ollama):
    fake_ollama.models = [{"name": "test-model:latest", "digest": "aaa111", "size": 2},
                          {"name": "other-model:latest", "digest": "bbb222", "size": 1},
                          {"name": "fake-embed:latest", "digest": "def", "size": 1},
                          # a local name for a model Ollama runs elsewhere (`ollama cp gpt-oss:120b-cloud alias-model`)
                          {"name": "alias-model:latest", "digest": "ccc333", "size": 384,
                           "remote_model": "gpt-oss:120b", "remote_host": "https://ollama.com:443"}]
    fake_ollama.vram_per_model = 1000 * MB
    return fake_ollama


@pytest.fixture
def client(make_settings, fake):
    clients = []

    def make(extra: str = PROFILES) -> TestClient:
        c = TestClient(create_app(make_settings(extra=extra, base_url=fake.url)), base_url="http://127.0.0.1")
        c.__enter__()
        c.app.state.models.nvml = FakeNvml(fake)
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)


def audit(c) -> list[dict]:
    return [json.loads(line) for line in c.app.state.audit.path.read_text(encoding="utf-8").splitlines()]


def switch(c, profile: str):
    return c.post("/api/models/switch", json={"profile": profile})


def test_profiles_are_listed_with_installation_and_load_state(client, fake):
    c = client()
    c.post("/api/chat", json={"message": "hi"})  # loads the default model
    body = c.get("/api/models").json()
    rows = {p["id"]: p for p in body["profiles"]}
    assert body["active"] == "default" and rows["default"]["active"] and rows["default"]["model"] == "test-model:latest"
    assert rows["default"]["installed"] and rows["default"]["loaded"] and rows["other"]["installed"] and not rows["other"]["loaded"]
    assert rows["missing"]["installed"] is False and rows["off"]["enabled"] is False
    assert rows["pinned"]["digest_ok"] is False and rows["default"]["digest_ok"] is None  # no pin on the test default
    assert rows["other"]["measured_vram_mib"] is None  # nothing is claimed before a switch was measured
    assert rows["aliased"]["remote"] is True and rows["other"]["remote"] is False and rows["missing"]["remote"] is None
    assert body["ollama_reachable"] and {r["name"] for r in body["running"]} == {"test-model:latest"}


def test_switching_unloads_the_old_model_loads_the_new_one_and_records_it(client, fake):
    c = client()
    c.post("/api/chat", json={"message": "hi"})
    resp = switch(c, "other")
    assert resp.status_code == 200, resp.text
    record = resp.json()["switch"]
    assert record["outcome"] == "switched" and record["from_profile"] == "default" and record["to_profile"] == "other"
    assert record["footprint_mib"] == pytest.approx(1000 * MB / 2**20, abs=1)  # device VRAM after load minus after unload
    assert list(fake.loaded) == ["other-model:latest"]  # never two generation models resident
    unloads = [g for g in fake.generate_requests if g.get("keep_alive") == 0]
    assert [g["model"] for g in unloads] == ["test-model:latest"]
    assert any(r.get("stream") is False and r["model"] == "other-model:latest" for r in fake.chat_requests)  # warm-up
    assert c.get("/api/health").json()["models"]["active"] == "other"
    assert c.get("/api/health").json()["ollama"]["model"] == "other-model:latest"
    c.post("/api/chat", json={"message": "and now?"})
    assert fake.chat_requests[-1]["model"] == "other-model:latest"  # chat follows the active profile
    rows = {p["id"]: p for p in c.get("/api/models").json()["profiles"]}
    assert rows["other"]["active"] and rows["other"]["measured_vram_mib"] == pytest.approx(953.7, abs=1)
    events = [a for a in audit(c) if a["event"] == "model_switched"]
    assert events and events[-1]["to_profile"] == "other" and events[-1]["model"] == "other-model:latest"


def test_a_context_size_profile_changes_num_ctx_for_chat_and_tasks(client, fake):
    c = client()
    assert switch(c, "small-ctx").status_code == 200
    c.post("/api/chat", json={"message": "hi"})
    assert fake.chat_requests[-1]["options"]["num_ctx"] == 4096
    assert c.app.state.tasks.budget_for("react").num_ctx == 4096  # tasks read the same in-memory [ollama] section


@pytest.mark.parametrize("profile, code, status, words", [
    ("missing", "not_installed", 409, "ollama pull not-downloaded:latest"),
    ("off", "profile_disabled", 409, "does not fit"),
    ("pinned", "digest_mismatch", 409, "does not match the pinned zzz"),
    ("default", "already_active", 409, "already the active profile"),
    ("aliased", "remote_model", 409, "does not run on this computer (Ollama forwards it to https://ollama.com:443)"),
])
def test_refusals_change_nothing_and_are_recorded(client, fake, profile, code, status, words):
    c = client()
    resp = switch(c, profile)
    assert resp.status_code == status and resp.json()["error"]["code"] == code and words in resp.json()["error"]["message"]
    assert resp.json()["record"]["outcome"] == "refused"
    assert c.app.state.models.active_id == "default"
    assert c.get("/api/health").json()["ollama"]["model"] == "test-model:latest"
    assert not [g for g in fake.generate_requests if g.get("keep_alive") == 0]  # nothing was unloaded
    assert any(a["event"] == "model_switch_refused" and a["code"] == code for a in audit(c))
    assert c.get("/api/models").json()["recent_switches"][0]["code"] == code


def test_unknown_profile_and_bad_input(client):
    c = client()
    assert switch(c, "nope").status_code == 404
    assert c.post("/api/models/switch", json={"profile": "Other; DROP"}).status_code == 422
    assert c.post("/api/models/switch", json={"profile": "other", "force": True}).status_code == 422


def test_no_switch_while_a_task_is_running(client, fake):
    c = client()
    fake.chat_delay_s = 1.5
    task = c.post("/api/tasks", json={"goal": "Say hello."}).json()
    resp = switch(c, "other")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "busy" and "task" in resp.json()["error"]["message"]
    deadline = time.time() + 20
    while c.get(f"/api/tasks/{task['task_id']}").json()["status"] not in ("completed", "error", "failed_safe") and time.time() < deadline:
        time.sleep(0.1)
    fake.chat_delay_s = 0
    assert switch(c, "other").status_code == 200  # once nothing generates, the switch goes ahead


def test_no_switch_while_a_reply_is_streaming(client, fake):
    c = client()
    fake.chat_delay_s = 1.5
    t = threading.Thread(target=lambda: c.post("/api/chat", json={"message": "slow"}))
    t.start()
    deadline = time.time() + 5
    while not fake.chat_requests and time.time() < deadline:
        time.sleep(0.02)
    resp = switch(c, "other")
    t.join()
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "busy"


def test_insufficient_free_vram_against_the_measured_footprint_is_refused(client, fake):
    c = client()
    assert switch(c, "other").status_code == 200  # measures other at 1000 MB
    assert switch(c, "default").status_code == 200
    c.app.state.models.nvml.desktop = 5200 * MB  # something else now holds most of the card
    resp = switch(c, "other")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "insufficient_vram"
    assert "was measured to need about 954 MiB" in resp.json()["error"]["message"]
    assert c.app.state.models.active_id == "default"
    assert "other-model:latest" not in fake.loaded


def test_a_failed_load_reverts_to_the_previous_profile(client, fake):
    c = client()
    fake.load_fail = {"other-model:latest"}
    resp = switch(c, "other")
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "load_failed" and "Default stays active" in resp.json()["error"]["message"]
    assert c.app.state.models.active_id == "default"
    assert c.get("/api/health").json()["ollama"]["model"] == "test-model:latest"
    assert resp.json()["record"]["outcome"] == "failed"
    fake.load_fail = set()
    assert c.post("/api/chat", json={"message": "still works?"}).status_code == 200
    assert fake.chat_requests[-1]["model"] == "test-model:latest"


def test_a_model_that_will_not_unload_stops_the_switch(client, fake):
    c = client()
    c.post("/api/chat", json={"message": "hi"})
    fake.keep_loaded_after_unload = {"test-model:latest"}
    resp = switch(c, "other")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "unload_timeout"
    assert "other-model:latest" not in fake.loaded and c.app.state.models.active_id == "default"


def test_startup_unloads_a_generation_model_left_by_an_earlier_run(make_settings, fake):
    fake.loaded = {"other-model:latest": 1000 * MB, "fake-embed:latest": 500 * MB, "some-unrelated:latest": 1}
    with TestClient(create_app(make_settings(extra=PROFILES, base_url=fake.url)), base_url="http://127.0.0.1") as c:
        assert set(fake.loaded) == {"fake-embed:latest", "some-unrelated:latest"}  # only XODUS's own generators
        assert any(a["event"] == "model_unloaded_at_startup" and a["models"] == ["other-model:latest"] for a in audit(c))


def test_ollama_down_is_reported_not_crashed(make_settings):
    from tests.conftest import free_port

    with TestClient(create_app(make_settings(extra=PROFILES, base_url=f"http://127.0.0.1:{free_port()}")),
                    base_url="http://127.0.0.1") as c:
        body = c.get("/api/models").json()
        assert body["ollama_reachable"] is False and all(p["installed"] is None for p in body["profiles"])
        resp = switch(c, "other")
        assert resp.status_code == 503 and resp.json()["error"]["code"] == "provider_unavailable"


@pytest.mark.parametrize("extra, words", [
    ('[[models.profiles]]\nid = "default"\nmodel = "x"\n', "'default' is reserved"),
    ('[[models.profiles]]\nid = "a"\nmodel = "x"\n[[models.profiles]]\nid = "a"\nmodel = "y"\n', "must be unique"),
    ('[[models.profiles]]\nid = "a"\nmodel = "x"\nnum_ctx = 16384\n', "not in ollama.allowed_num_ctx"),
    ('[[models.profiles]]\nid = "Bad Id"\nmodel = "x"\n', "string should match pattern"),
])
def test_invalid_profiles_stop_startup_with_a_reason(make_settings, extra, words):
    with pytest.raises(ConfigError) as exc:
        make_settings(extra=extra)
    assert words.lower() in str(exc.value).lower()
