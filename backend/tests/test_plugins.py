"""Out-of-process plugins under the supervisor: scrubbed environment, timeouts, crashes, oversized or
malformed output, cancellation without orphans, and a circuit breaker (security table rows: malformed
manifests and oversized output; timeout and cancellation races; secrets)."""

from __future__ import annotations

import asyncio
import time

import psutil
import pytest

from tests.conftest import FIXTURES
from tests.helpers import ScriptedProvider, audit_events, make_deps, run_task, tool_call
from xodus.capabilities.discovery import validate_plugin_manifest
from xodus.capabilities.plugin_runner import SubprocessCapability
from xodus.capabilities.registry import (
    CapabilityTimeout,
    InvalidOutput,
    InvocationContext,
    OutputTooLarge,
    PluginCrashed,
    PluginError,
)
from xodus.providers.base import Completion
from xodus.supervisor.supervisor import Supervisor

pytestmark = pytest.mark.skipif(not hasattr(psutil, "Process"), reason="psutil required")


def manifest(mode: str, **over):
    data = {
        "id": f"test.plugin_{mode}",
        "name": f"Plugin {mode}",
        "description": f"Test plugin in {mode} mode.",
        "version": "1.0.0",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "properties": {"seconds": {"type": "number"}, "hang": {"type": "boolean"},
                                        "mb": {"type": "integer"}, "text": {"type": "string"}}},
        "output_schema": {"type": "object"},
        "permissions": ["net.none"],
        "execution_mode": "subprocess",
        "process_tier": "optional",
        "timeout_s": 10,
        "max_output_bytes": 100_000,
        "requires_approval": "never",
        "side_effects": "none",
        "entry": {"command": ["python", "plugin_main.py", mode]},
    }
    data.update(over)
    reasons, m = validate_plugin_manifest(data)
    assert m is not None, reasons
    return m


@pytest.fixture
def supervisor():
    sup = Supervisor(max_optional=4, idle_timeout_s=60, memory_limit_bytes=256 * 2**20)
    yield sup
    sup.stop_all("test teardown")


def plugin(supervisor, tmp_path, mode, secrets=None, **over):
    return SubprocessCapability(manifest(mode, **over), FIXTURES, supervisor, tmp_path / "work", secrets)


def ctx():
    return InvocationContext(task_id="t", step_index=1)


def gone(pids, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not [p for p in pids if psutil.pid_exists(p)]:
            return True
        time.sleep(0.05)
    return False


async def test_echo_round_trip_runs_in_its_own_work_folder(supervisor, tmp_path):
    cap = plugin(supervisor, tmp_path, "echo")
    result = await cap.invoke({"text": "hi"}, ctx())
    assert result["echo"] == {"text": "hi"}
    assert result["cwd"].lower() == str((tmp_path / "work" / "test.plugin_echo").resolve()).lower()
    mp = cap.process
    assert mp is not None and mp.tier == "optional" and mp.role == "plugin" and mp.owner == "test.plugin_echo"
    assert result["pid"] in mp.job.pids()  # the interpreter really runs inside the job


async def test_environment_is_scrubbed_and_only_approved_secrets_pass(supervisor, tmp_path, monkeypatch):
    monkeypatch.setenv("XODUS_TEST_CANARY", "canary-should-not-leak")
    monkeypatch.setenv("XODUS_SECRET_OTHER", "nope")
    cap = plugin(supervisor, tmp_path, "env", secrets={"APPROVED_TOKEN": "tok-123"})
    result = await cap.invoke({}, ctx())
    assert result["canary"] is None
    assert result["approved"] == "tok-123"
    leaked = [k for k in result["keys"] if k.startswith("XODUS") or k in ("USERPROFILE", "APPDATA", "HOME")]
    assert leaked == []


async def test_timeout_kills_the_plugin(supervisor, tmp_path):
    cap = plugin(supervisor, tmp_path, "slow", timeout_s=1)
    with pytest.raises(CapabilityTimeout):
        await cap.invoke({"seconds": 30}, ctx())
    assert cap.process is None and supervisor.managed() == []


async def test_crash_is_reported_and_the_plugin_restarts(supervisor, tmp_path):
    cap = plugin(supervisor, tmp_path, "crash")
    with pytest.raises(PluginCrashed, match="exit code 3"):
        await cap.invoke({}, ctx())
    with pytest.raises(PluginCrashed):  # a fresh process is started for the next call
        await cap.invoke({}, ctx())
    assert supervisor.managed() == []


async def test_circuit_breaker_pauses_a_failing_plugin(supervisor, tmp_path):
    cap = plugin(supervisor, tmp_path, "crash")
    for _ in range(3):
        with pytest.raises(PluginCrashed):
            await cap.invoke({}, ctx())
    assert cap.health() and "paused" in cap.health()[0]


async def test_oversized_output_is_discarded_without_crashing_the_backend(supervisor, tmp_path):
    cap = plugin(supervisor, tmp_path, "bigout", max_output_bytes=50_000)
    with pytest.raises(OutputTooLarge, match="discarded"):
        await cap.invoke({}, ctx())
    assert supervisor.managed() == []
    echo = plugin(supervisor, tmp_path, "echo")
    assert (await echo.invoke({}, ctx()))["echo"] == {}  # the backend is fine


@pytest.mark.parametrize("mode, error, stopped", [("badjson", InvalidOutput, True), ("wrongid", InvalidOutput, True),
                                                  ("error", PluginError, False)])
async def test_protocol_violations_and_plugin_errors(supervisor, tmp_path, mode, error, stopped):
    cap = plugin(supervisor, tmp_path, mode)
    with pytest.raises(error):
        await cap.invoke({}, ctx())
    # a protocol violation leaves the process out of sync, so it is stopped; a clean error reply is not
    assert (cap.process is None and supervisor.managed() == []) == stopped


async def test_cancel_kills_plugin_and_its_grandchild(supervisor, tmp_path):
    cap = plugin(supervisor, tmp_path, "spawn")
    first = await cap.invoke({}, ctx())  # spawns a long-sleeping grandchild and returns its pid
    grandchild = first["grandchild"]
    job_pids = cap.process.job.pids()
    assert grandchild in job_pids and psutil.pid_exists(grandchild)
    call = asyncio.create_task(cap.invoke({"hang": True}, ctx()))
    await asyncio.sleep(0.5)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert gone(job_pids), [p for p in job_pids if psutil.pid_exists(p)]
    assert supervisor.managed() == []


async def test_plugin_error_text_never_reaches_the_planner(tmp_path, sandbox, supervisor):
    """An out-of-process plugin's error message is untrusted: a re-plan gets only its error code."""
    cap = plugin(supervisor, tmp_path, "error")
    plan = {"steps": [{"capability": "test.plugin_error", "args": {}}]}
    provider = ScriptedProvider(plans=[plan, {"steps": []}], answer="ok")
    deps = make_deps(tmp_path, sandbox, provider, extra=[cap])
    task = await run_task(deps, "plan_execute", "run the plugin")
    replan = [c for c in provider.calls if c["kind"] == "structured"][1]
    assert "plugin_error" in str(replan["messages"])
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" not in str(replan["messages"])
    assert task.status == "completed"


async def test_react_gets_plugin_errors_but_cannot_act_on_them(tmp_path, sandbox, supervisor):
    from tests.helpers import WriteNote

    cap = plugin(supervisor, tmp_path, "error")
    writer = WriteNote("sandbox", sandbox)
    provider = ScriptedProvider(tool_turns=[tool_call("test__plugin_error", {}),
                                            tool_call("test__write_note", {"path": "..\\..\\pwned.txt", "text": "x"}),
                                            Completion("done")])
    deps = make_deps(tmp_path, sandbox, provider, extra=[cap, writer], ttl_s=0.3)
    await run_task(deps, "react", "run the plugin")
    assert writer.executions == 0
    assert [e["capability"] for e in audit_events(deps) if e["event"] == "capability_call"] == ["test.plugin_error"]


async def test_job_memory_limit_stops_runaway_allocation(tmp_path):
    """[VERIFY] the per-job memory limit: with 128 MB allowed, 64 MB commits, 512 MB is refused, and the
    plugin process survives the refusal (it gets MemoryError rather than being killed)."""
    sup = Supervisor(max_optional=2, memory_limit_bytes=128 * 2**20)
    try:
        cap = SubprocessCapability(manifest("alloc"), FIXTURES, sup, tmp_path / "work")
        assert await cap.invoke({"mb": 64}, ctx()) == {"allocated_mb": 64}
        assert await cap.invoke({"mb": 512}, ctx()) == {"memory_error": True}
        assert await cap.invoke({"mb": 64}, ctx()) == {"allocated_mb": 64}  # same process, still working
        assert len(sup.managed()) == 1
    finally:
        sup.stop_all()
