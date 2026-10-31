"""Resource governor (master prompt 3.7): warn, stop and recover behaviour, and what is refused at each level.

The state machine is driven with recorded snapshots and a fake clock; the process rules use the real Supervisor and a
real child process; the HTTP refusals use the real app with a sampler whose snapshot the test controls.
"""

from __future__ import annotations

import sys
import time

import psutil
import pytest

from tests.helpers import ScriptedProvider, api_client
from tests.test_workspace_api import snap
from xodus.api.app import create_app
from xodus.config.settings import ThresholdSettings
from xodus.resources.governor import ResourceGovernor, ResourcePressure
from xodus.resources.sampler import ResourceSampler
from xodus.resources.thresholds import evaluate
from xodus.supervisor.supervisor import Supervisor, SupervisorLimit

OK = snap(1.0, 3.0, 8.0)
WARN_RAM = snap(3.7, 3.0, 8.0)
STOP_VRAM = snap(1.0, 4.9, 8.0)
LOW_AVAILABLE = snap(1.0, 3.0, 1.2)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakeIngestion:
    def __init__(self) -> None:
        self.held: str | None = None
        self.calls: list[str] = []

    def hold(self, reason: str) -> dict:
        self.held = reason
        self.calls.append("hold")
        return {"held": reason, "worker_stopped": True, "requeued_jobs": 1}

    def release(self, reason: str) -> dict:
        self.held = None
        self.calls.append("release")
        return {"released": True, "worker_started": True}


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def append(self, event: str, **fields) -> None:
        self.events.append((event, fields))

    def names(self) -> list[str]:
        return [e for e, _ in self.events]


class FakeSupervisor:
    def __init__(self) -> None:
        self.optional_admission = None
        self.idle: list = []
        self.stopped: list = []

    def idle_optional(self, grace_s: float) -> list:
        return list(self.idle)

    def stop(self, mp, reason: str) -> None:
        self.stopped.append((mp, reason))
        self.idle.remove(mp)


class Proc:
    pid, owner, role = 4242, "text.stats", "plugin"


def governor(**overrides):
    clock, ingestion, supervisor, audit = Clock(), FakeIngestion(), FakeSupervisor(), FakeAudit()
    g = ResourceGovernor(ThresholdSettings(**overrides), lambda: OK, ingestion, supervisor, audit, clock=clock)
    return g, clock, ingestion, supervisor, audit


def feed(g: ResourceGovernor, snapshot: dict) -> dict:
    return g.apply(evaluate(snapshot, g.t, g.state()))


# ---------------------------------------------------------------------------------------------- what is measured


def test_shared_gpu_memory_is_reported_but_not_added_to_uss():
    """Protocol amendment 2: measured, USS + shared GPU memory exceeded the system-wide increase, so it is not added."""
    result = evaluate(snap(3.3, 3.0, 8.0, shared_gb=0.6), ThresholdSettings())
    ram = result["checks"][0]
    assert ram["metric"] == "xodus_uss" and ram["value_gb"] == 3.3 and ram["level"] == "ok"
    assert ram["shared_gpu_gb"] == 0.6 and "0.6 GB of shared GPU memory" in ram["note"]


def test_holding_ingestion_also_unloads_the_embedder_once():
    """Dry-run finding: a held ingestion kept its 928 MiB embedder resident for the keep-alive, so the level never fell."""
    clock, ingestion, supervisor, audit = Clock(), FakeIngestion(), FakeSupervisor(), FakeAudit()
    unloads = []
    g = ResourceGovernor(ThresholdSettings(), lambda: OK, ingestion, supervisor, audit,
                         unload_embedder=lambda: unloads.append(1) or True, clock=clock)
    feed(g, WARN_RAM)
    feed(g, WARN_RAM)
    assert unloads == [1] and [a["action"] for a in g.episode["actions"]] == ["ingestion_held", "embedder_unloaded"]


def test_vram_is_xodus_own_not_the_whole_device():
    """User decision (29 Oct 2026): the desktop's and browser's GPU memory must not push XODUS to warn or stop."""
    result = evaluate(snap(1.0, 3.2, 8.0, device_gb=5.3), ThresholdSettings())
    vram = result["checks"][1]
    assert vram["metric"] == "xodus_vram" and vram["value_gb"] == 3.2 and vram["level"] == "ok"
    assert vram["basis"] == "xodus_processes" and vram["device_used_gb"] == 5.3


def test_without_per_process_counters_vram_falls_back_to_device_wide_and_says_so():
    result = evaluate(snap(1.0, None, 8.0, device_gb=4.5), ThresholdSettings())
    vram = result["checks"][1]
    assert vram["basis"] == "device_fallback" and vram["level"] == "warn" and "overstates" in vram["note"]


# ---------------------------------------------------------------------------------------------- levels and actions


def test_warn_holds_ingestion_stops_idle_optional_processes_and_refuses_new_ones():
    g, _, ingestion, supervisor, audit = governor()
    supervisor.idle = [Proc()]
    feed(g, WARN_RAM)
    assert g.level == "warn" and ingestion.held and supervisor.stopped and not supervisor.idle
    assert "warn" in (supervisor.optional_admission() or "")  # new optional processes refused
    g.check("tasks")  # heavy work is still allowed at warn
    assert {"resource_level_changed", "resource_action"} <= set(audit.names())
    assert g.episode and [a["action"] for a in g.episode["actions"]] == ["ingestion_held", "optional_process_stopped"]


@pytest.mark.parametrize("snapshot, breach", [(STOP_VRAM, "XODUS VRAM"), (LOW_AVAILABLE, "System memory available")])
def test_stop_refuses_heavy_work_with_a_clear_message(snapshot, breach):
    g, *_ = governor()
    feed(g, snapshot)
    assert g.level == "stop"
    for kind in ("tasks", "ingest", "model_switch", "capability"):
        with pytest.raises(ResourcePressure) as info:
            g.check(kind)
        assert breach in info.value.message and "Chat stays available" in info.value.message
        assert info.value.retry_after_s == 30


def test_recovery_needs_30_s_below_the_level_then_releases_and_records_the_episode():
    g, clock, ingestion, supervisor, audit = governor()
    feed(g, STOP_VRAM)
    feed(g, OK)
    clock.t += 29.9
    feed(g, OK)
    assert g.level == "stop" and g.state()["stepping_down_in_s"] == pytest.approx(0.1, abs=0.05)
    clock.t += 0.2
    feed(g, OK)
    assert g.level == "ok" and ingestion.calls == ["hold", "release"] and g.episode is None
    episode = next(f for e, f in audit.events if e == "resource_episode")
    assert episode["highest"] == "stop" and episode["peaks"]["xodus_vram_max_gb"] == 4.9 and episode["duration_s"] >= 0
    assert [a["action"] for a in episode["actions"]] == ["ingestion_held", "ingestion_released"]
    assert supervisor.optional_admission() is None  # optional processes may start again
    g.check("tasks")  # allowed again


def test_steps_down_to_the_highest_level_seen_while_waiting():
    g, clock, ingestion, *_ = governor()
    feed(g, STOP_VRAM)
    for _ in range(4):
        clock.t += 10
        feed(g, WARN_RAM)
    assert g.level == "warn" and ingestion.held  # stop -> warn, not straight to ok
    g.check("tasks")
    for _ in range(4):
        clock.t += 10
        feed(g, OK)
    assert g.level == "ok" and ingestion.held is None


def test_going_back_up_restarts_the_recovery_wait():
    g, clock, *_ = governor()
    feed(g, STOP_VRAM)
    feed(g, OK)
    clock.t += 20
    feed(g, STOP_VRAM)  # back at the level: the wait starts again
    clock.t += 20
    feed(g, OK)
    clock.t += 20
    feed(g, OK)
    assert g.level == "stop"
    clock.t += 11
    feed(g, OK)
    assert g.level == "ok"


def test_a_check_that_cannot_be_measured_never_raises_the_level():
    g, *_ = governor()
    feed(g, snap(9.0, 3.0, 8.0, complete=False))  # incomplete USS: unknown, not "stop"
    assert g.level == "ok"


def test_switched_off_it_only_classifies():
    g, _, ingestion, supervisor, _ = governor(enforce=False)
    supervisor.idle = [Proc()]
    result = feed(g, STOP_VRAM)
    assert result["level"] == "stop" and g.level == "ok" and ingestion.held is None and not supervisor.stopped
    assert supervisor.optional_admission is None
    g.check("tasks")


# ---------------------------------------------------------------------------------------------- real processes


def sleeper(sup: Supervisor, tmp_path):
    return sup.launch([sys.executable, "-c", "import time; time.sleep(60)"], role="plugin", owner="test.sleeper",
                      start_reason="test", cwd=tmp_path, env=None, tier="optional", pipes=False)


def test_a_process_serving_a_call_or_recently_used_is_never_idle(tmp_path):
    clock = Clock()
    sup = Supervisor(clock=clock)
    mp = sleeper(sup, tmp_path)
    try:
        with sup.in_use(mp):
            clock.t += 600
            assert sup.idle_optional(10) == []  # mid-call, however long
        assert sup.idle_optional(10) == []  # just used
        clock.t += 10
        assert sup.idle_optional(10) == [mp]
    finally:
        sup.stop_all()


def test_refused_optional_launch_starts_nothing(tmp_path):
    sup = Supervisor()
    sup.optional_admission = lambda: "XODUS is at its resource warn level"
    before = {p.pid for p in psutil.Process().children(recursive=True)}
    with pytest.raises(SupervisorLimit, match="warn level"):
        sleeper(sup, tmp_path)
    assert sup.managed() == [] and {p.pid for p in psutil.Process().children(recursive=True)} == before
    core = sup.launch([sys.executable, "-c", "pass"], role="ingestion", owner="ingestion", start_reason="test",
                      cwd=tmp_path, env=None, tier="core", pipes=False)  # core processes are not refused by the hook
    sup.stop(core, "test")


# ---------------------------------------------------------------------------------------------- over HTTP


class SteerableSampler(ResourceSampler):
    def __init__(self, port: int) -> None:
        super().__init__(ollama_port=port)
        self.next: dict | None = None

    def snapshot(self) -> dict:
        real = super().snapshot()
        if self.next is None:
            return real
        merged = {**real, "uss_complete": self.next["uss_complete"], "errors": {**real["errors"], **self.next["errors"]}}
        for key in ("totals", "system_memory"):  # keep the real keys the resource view reads; override the measured ones
            merged[key] = {**real[key], **self.next[key]}
        return merged


@pytest.fixture
def governed(make_settings, monkeypatch):
    monkeypatch.setenv("XODUS_THRESHOLDS__ENFORCE", "true")
    monkeypatch.setenv("XODUS_THRESHOLDS__CHECK_INTERVAL_S", "60")  # the test calls tick() itself
    monkeypatch.setenv("XODUS_THRESHOLDS__RECOVER_AFTER_S", "0.3")
    settings = make_settings()
    sampler = SteerableSampler(1)
    app = create_app(settings, provider=ScriptedProvider(answer="still here"), sampler=sampler)
    with api_client(app) as client:
        yield client, app.state.governor, sampler


def test_over_http_stop_refuses_heavy_work_keeps_chat_and_recovers(governed):
    client, gov, sampler = governed
    sampler.next = STOP_VRAM
    gov.tick()
    assert client.get("/api/health").json()["resources"]["level"] == "stop"
    for method, path, body in [("POST", "/api/tasks", {"goal": "list the files"}),
                               ("POST", "/api/models/switch", {"profile": "other"}),
                               ("POST", "/api/knowledge/ingest", {"collection": "x", "root": "sandbox", "path": "."}),
                               ("POST", "/api/knowledge/search", {"query": "anything"})]:
        r = client.request(method, path, json=body)
        assert r.status_code == 503, path
        assert r.json()["error"]["code"] == "resource_pressure" and "XODUS VRAM" in r.json()["error"]["message"]
        assert r.headers["Retry-After"] == "1"  # never "retry now"
    chat = client.post("/api/chat", json={"message": "hello"})
    assert chat.status_code == 200 and "event: token" in chat.text and "event: done" in chat.text  # chat stays available
    view = client.get("/api/resources").json()["thresholds"]
    assert view["enforced"] is True and view["governor"]["level"] == "stop" and "Enforced" in view["enforcement_note"]

    sampler.next = OK
    gov.tick()
    time.sleep(0.35)
    gov.tick()
    assert gov.level == "ok"
    assert client.post("/api/tasks", json={"goal": "list the files"}).status_code == 202
    events = [r["event"] for r in client.get("/api/audit", params={"limit": 50}).json()["records"]]
    assert {"resource_level_changed", "resource_action", "resource_episode"} <= set(events)


def test_over_http_warn_holds_ingestion_but_not_tasks(governed):
    client, gov, sampler = governed
    sampler.next = WARN_RAM
    gov.tick()
    health = client.get("/api/health").json()
    assert health["resources"]["level"] == "warn" and health["knowledge"]["ingestion"]["held"] == "resource warn level"
    assert client.post("/api/tasks", json={"goal": "list the files"}).status_code == 202


def test_real_sampler_values_reach_the_governor(governed):
    _, gov, sampler = governed
    sampler.next = None
    result = gov.tick()
    assert result["checks"][0]["uss_gb"] is not None and result["checks"][0]["uss_gb"] > 0
    assert result["governor"]["enabled"] is True and gov.level in ("ok", "warn", "stop")
