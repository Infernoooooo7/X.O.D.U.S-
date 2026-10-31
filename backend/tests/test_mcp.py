"""MCP servers as capabilities: manifest validation, the stdio client, pinned tool definitions, refused server
requests, process supervision and hostile servers. Runs real subprocesses (tests/fixtures/mcp/fixture_server.py)."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient

from tests.helpers import ScriptedProvider, tool_call, api_client
from tests.test_knowledge_api import wait_for
from xodus.api.app import create_app
from xodus.capabilities.discovery import validate_mcp_manifest, validate_plugin_manifest
from xodus.capabilities.mcp_client import comparable_schema
from xodus.providers.base import Completion

SERVER = Path(__file__).parent / "fixtures" / "mcp" / "fixture_server.py"
ECHO = {"type": "object", "additionalProperties": False, "properties": {"text": {"type": "string", "maxLength": 1000}},
        "required": ["text"]}
ADD = {"type": "object", "additionalProperties": False, "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
       "required": ["a", "b"]}


def tool(cap_id: str, mcp_tool: str, schema: dict, output: dict, **over) -> dict:
    return {"id": cap_id, "name": cap_id, "description": f"Reviewed description of {mcp_tool}.", "version": "1.0.0",
            "input_schema": schema, "output_schema": output, "permissions": ["net.none", "exec.none"],
            "execution_mode": "mcp", "process_tier": "optional", "timeout_s": 3, "max_output_bytes": 20000,
            "requires_approval": "never", "side_effects": "none", "mcp_tool": mcp_tool, **over}


def manifest(mode: str = "normal", **over) -> dict:
    return {"kind": "mcp_server", "id": "fixture", "name": "Fixture server", "description": "Test MCP server.",
            "version": "0.1.0", "entry": {"command": ["python", "server.py", mode]}, "init_timeout_s": 3,
            "tools": [tool("fixture.echo", "echo", ECHO, {"type": "object", "required": ["text"],
                                                          "properties": {"text": {"type": "string"}, "non_text_items": {"type": "integer"}}}),
                      tool("fixture.add", "add", ADD, {"type": "object", "required": ["sum"], "properties": {"sum": {"type": "number"}}})],
            **over}


def install(plugins: Path, data: dict, folder: str = "fixture") -> Path:
    target = plugins / folder
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(SERVER, target / "server.py")
    (target / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------------------------- manifests


def test_a_valid_server_manifest():
    reasons, m = validate_mcp_manifest(manifest())
    assert reasons == [] and m is not None and [t.id for t in m.tools] == ["fixture.echo", "fixture.add"]


@pytest.mark.parametrize("change, words", [
    ({"tools": []}, "at least 1"),
    ({"id": "Bad-Id"}, "pattern"),
    ({"surprise": 1}, "extra"),
    ({"entry": {"command": ["node", "server.js"]}}, "python"),
    ({"tools": [tool("a.one", "x", ECHO, {"type": "object"}), tool("a.one", "y", ECHO, {"type": "object"})]}, "unique"),
    ({"tools": [tool("a.one", "x", ECHO, {"type": "object"}), tool("a.two", "x", ECHO, {"type": "object"})]}, "declared once"),
    ({"tools": [tool("a.one", "x", ECHO, {"type": "object"}, execution_mode="subprocess")]}, "execution_mode 'mcp'"),
    ({"tools": [tool("a.one", "x", ECHO, {"type": "object"}, permissions=["fs.read:sandbox"])]}, "file-system"),
    ({"tools": [tool("a.one", "x", {"type": "object", "properties": {}}, {"type": "object"})]}, "additionalProperties"),
    ({"tools": [tool("a.one", "bad name!", ECHO, {"type": "object"})]}, "pattern"),
    ({"tools": [tool("a.one", "x", ECHO, {"type": "object"}, side_effects="external")]}, "must require approval"),
    ({"secrets": ["lower"]}, "UPPER_SNAKE_CASE"),
])
def test_invalid_server_manifests_are_rejected_with_a_reason(change, words):
    reasons, m = validate_mcp_manifest({**manifest(), **change})
    assert m is None and any(words.lower() in r.lower() for r in reasons), reasons


def test_a_plugin_manifest_cannot_claim_mcp():
    data = {**tool("a.one", "x", ECHO, {"type": "object"}), "entry": {"command": ["python", "main.py"]}}
    data.pop("mcp_tool")
    reasons, m = validate_plugin_manifest(data)
    assert m is None and any("mcp_server" in r for r in reasons)


def test_the_pin_comparison_ignores_only_what_xodus_tightens():
    server = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    assert comparable_schema(server) == comparable_schema({**server, "additionalProperties": False, "$schema": "x"})
    assert comparable_schema(server) != comparable_schema({**server, "properties": {"text": {"type": "string"}, "cmd": {}}})
    assert comparable_schema(server) != comparable_schema({**server, "required": []})


# ---------------------------------------------------------------------------------------------- the client, end to end


@pytest.fixture
def plugins(tmp_path):
    return tmp_path / "plugins"


@pytest.fixture
def client(make_settings, fake_ollama, plugins, monkeypatch):
    monkeypatch.setenv("XODUS_SUPERVISOR__REAPER_INTERVAL_S", "0.3")
    opened = []

    def make(data: dict | None = None, extra: str = "", provider=None) -> TestClient:
        if data is not None:
            install(plugins, data)
        c = api_client(create_app(make_settings(extra=extra, base_url=fake_ollama.url), provider=provider))
        c.__enter__()
        opened.append(c)
        return c

    yield make
    for c in opened:
        c.__exit__(None, None, None)


def invoke(c, cap: str, **args):
    return c.post(f"/api/capabilities/{cap}/invoke", json={"args": args})


def inventory(c) -> dict:
    return next(e for e in c.get("/api/extensions").json()["extensions"] if e["id"] == "fixture")


def calls_seen(c) -> list[dict]:
    log = c.app.state.db.path.parent / "plugins" / "mcp-fixture" / "calls.log"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def test_tools_are_registered_lazily_and_run_in_a_supervised_optional_process(client):
    c = client(manifest())
    caps = {x["id"]: x for x in c.get("/api/capabilities").json()}
    assert caps["fixture.echo"]["source"] == "mcp:fixture" and caps["fixture.echo"]["available"]
    assert caps["fixture.echo"]["description"] == "Reviewed description of echo."  # never the server's own text
    assert inventory(c)["process"] is None and c.app.state.supervisor.managed() == []  # not started at startup
    r = invoke(c, "fixture.echo", text="hello")
    assert r.status_code == 200 and r.json()["result"] == {"text": "hello", "non_text_items": 1}
    assert invoke(c, "fixture.add", a=2, b=3.5).json()["result"] == {"sum": 5.5}  # structuredContent
    inv = inventory(c)
    assert inv["process"]["alive"] and inv["process"]["tier"] == "optional" and inv["starts"] == 1
    assert inv["protocol_version"] == "2025-06-18" and inv["server_info"] == {"name": "fixture", "version": "0.1"}
    mp = c.app.state.supervisor.managed()[0]
    assert (mp.role, mp.owner, mp.tier) == ("mcp", "mcp:fixture", "optional") and mp.memory_limit_bytes == 512 * 2**20
    view = c.get("/api/resources").json()
    optional = next(line for line in view["lines"] if line["line"] == "optional")
    assert optional["processes"] >= 1 and optional["uss_mib"] > 0  # counted in the measurements
    assert any(p["owner"] == "mcp:fixture" and p["tier"] == "optional" for p in view["processes"])


def test_arguments_are_checked_against_the_manifest_before_the_server_sees_them(client):
    c = client(manifest())
    assert invoke(c, "fixture.echo", text="ok").status_code == 200
    for bad in ({"text": "x", "shell": "calc.exe"}, {"text": 5}, {}, {"text": "y" * 1001}):
        assert c.post("/api/capabilities/fixture.echo/invoke", json={"args": bad}).status_code == 422
    assert [call["arguments"] for call in calls_seen(c)] == [{"text": "ok"}]


def test_a_changed_tool_definition_disables_that_tool(client):
    c = client(manifest("drift"))
    r = invoke(c, "fixture.echo", text="hi")
    assert r.status_code == 502 and "differs from the reviewed manifest" in r.json()["error"]["message"]
    assert calls_seen(c) == []  # the drifted tool was never called
    caps = {x["id"]: x for x in c.get("/api/capabilities").json()}
    assert not caps["fixture.echo"]["available"] and "differs" in caps["fixture.echo"]["unavailable_reasons"][0]
    assert caps["fixture.add"]["available"] and invoke(c, "fixture.add", a=1, b=1).json()["result"] == {"sum": 2}
    assert inventory(c)["tools"][0]["disabled_reason"]


def test_a_tool_the_server_no_longer_offers_is_disabled(client):
    c = client(manifest("missing_tool"))
    invoke(c, "fixture.echo", text="x")  # the first connection learns what the server offers
    r = invoke(c, "fixture.add", a=1, b=2)
    assert r.status_code == 409 and r.json()["error"]["code"] == "unavailable"  # refused by the gate, before the server
    assert "no longer offers" in r.json()["error"]["message"] and [x["name"] for x in calls_seen(c)] == ["echo"]


def test_requests_from_the_server_are_refused(client):
    c = client(manifest("sampling"))
    r = invoke(c, "fixture.echo", text="x")
    assert r.status_code == 200 and '"code": -32601' in r.json()["result"]["text"]  # the server got "method not found"
    assert inventory(c)["refused_server_requests"] == ["sampling/createMessage"]
    assert c.app.state.models.provider is not None  # (and no model call was made for it)


def test_the_server_gets_a_scrubbed_environment(client, monkeypatch):
    monkeypatch.setenv("XODUS_SECRET_CANARY", "never-show-this")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "nope")
    c = client(manifest("env"))
    names = json.loads(invoke(c, "fixture.echo", text="x").json()["result"]["text"])
    assert "XODUS_SECRET_CANARY" not in names and "AWS_SECRET_ACCESS_KEY" not in names and "USERPROFILE" not in names
    assert {"PATH", "TEMP", "TMP", "SYSTEMROOT"} <= set(names)


@pytest.mark.parametrize("mode, status, code", [
    ("hang", 504, "timeout"),
    ("crash", 502, "plugin_crashed"),
    ("huge", 500, "output_too_large"),
    ("garbage", 500, "mcp_protocol_error"),
    ("wrongid", 500, "mcp_protocol_error"),
    ("flood", 500, "mcp_protocol_error"),
    ("iserror", 502, "plugin_error"),
])
def test_misbehaving_servers_fail_cleanly_and_are_stopped(client, mode, status, code):
    c = client(manifest(mode))
    started = time.monotonic()
    r = invoke(c, "fixture.echo", text="x")
    assert r.status_code == status and r.json()["error"]["code"] == code, r.text
    assert time.monotonic() - started < 15
    if code != "plugin_error":  # a tool error is an answer; everything else ends the process
        wait_for(lambda: not c.app.state.supervisor.managed(), 10, what="the server to be stopped")
    assert c.get("/api/health").json()["backend"]["status"] == "ok"


@pytest.mark.parametrize("mode, code, words", [
    ("badversion", "mcp_protocol_error", "protocol version"),
    ("notools", "mcp_protocol_error", "does not offer tools"),
    ("slowinit", "timeout", "did not finish starting"),
])
def test_servers_that_fail_to_start_are_reported(client, mode, code, words):
    c = client(manifest(mode, init_timeout_s=1))
    r = invoke(c, "fixture.echo", text="x")
    assert r.json()["error"]["code"] == code and words in r.json()["error"]["message"]
    wait_for(lambda: not c.app.state.supervisor.managed(), 10, what="the server to be stopped")


def test_a_call_cancelled_while_the_server_starts_leaves_no_process(client):
    """The tool's own timeout (1 s) fires during a 5 s handshake: the half-started server must be stopped at once."""
    data = manifest("slowinit", init_timeout_s=5)
    data["tools"][0]["timeout_s"] = 1
    c = client(data)
    r = invoke(c, "fixture.echo", text="x")
    assert r.json()["error"]["code"] == "timeout"
    wait_for(lambda: not c.app.state.supervisor.managed(), 5, what="the half-started server to be stopped")


def test_repeated_failures_pause_the_server(client):
    c = client(manifest("crash"))
    for _ in range(3):
        invoke(c, "fixture.echo", text="x")
    caps = {x["id"]: x for x in c.get("/api/capabilities").json()}
    assert not caps["fixture.echo"]["available"] and "paused" in caps["fixture.echo"]["unavailable_reasons"][0]
    assert inventory(c)["paused"] and invoke(c, "fixture.echo", text="x").status_code == 409


def test_an_idle_server_is_stopped_and_restarted_on_next_use(client):
    c = client(manifest(), extra="[supervisor]\noptional_idle_timeout_s = 0.5\nreaper_interval_s = 0.2\n")
    assert invoke(c, "fixture.echo", text="1").status_code == 200
    pid = inventory(c)["process"]["pid"]
    wait_for(lambda: not psutil.pid_exists(pid), 10, what="the idle stop")
    assert invoke(c, "fixture.echo", text="2").status_code == 200
    assert inventory(c)["starts"] == 2 and inventory(c)["process"]["pid"] != pid


def test_the_optional_process_limit_applies(client, plugins):
    install(plugins, {**manifest(), "id": "second", "tools": [
        tool("second.echo", "echo", ECHO, {"type": "object", "properties": {"text": {"type": "string"}}})]}, "second")
    install(plugins, {**manifest(), "id": "third", "tools": [
        tool("third.echo", "echo", ECHO, {"type": "object", "properties": {"text": {"type": "string"}}})]}, "third")
    c = client(manifest())
    assert invoke(c, "fixture.echo", text="a").status_code == 200
    assert invoke(c, "second.echo", text="b").status_code == 200
    r = invoke(c, "third.echo", text="c")  # max_optional_processes = 2
    assert r.status_code == 503 and r.json()["error"]["code"] == "process_limit" and "At most 2" in r.json()["error"]["message"]
    assert len(c.app.state.supervisor.managed()) == 2
    assert inventory(c)["paused"] is None  # hitting the envelope cap is not counted as a failure of the server


async def test_a_subprocess_plugin_at_the_process_limit_is_refused_clearly(tmp_path):
    from xodus.capabilities.manifest import PluginManifest
    from xodus.capabilities.plugin_runner import SubprocessCapability
    from xodus.capabilities.registry import InvocationContext, ProcessLimit
    from xodus.supervisor.supervisor import SupervisorLimit

    class FullSupervisor:
        def launch(self, *a, **kw):
            raise SupervisorLimit("At most 2 optional processes may run at once")

    folder = tmp_path / "p"
    folder.mkdir()
    (folder / "main.py").write_text("print('never')")
    data = {**tool("p.echo", "x", ECHO, {"type": "object"}), "execution_mode": "subprocess",
            "entry": {"command": ["python", "main.py"]}}
    data.pop("mcp_tool")
    cap = SubprocessCapability(PluginManifest.model_validate(data), folder, FullSupervisor(), tmp_path / "work")
    with pytest.raises(ProcessLimit):
        await cap.invoke({"text": "x"}, InvocationContext(None, 0))


def test_a_secret_must_be_approved_before_a_server_receives_it(client, plugins, monkeypatch):
    monkeypatch.setenv("XODUS_SECRET_TOKEN_X", "abc123456")
    c = client(manifest(secrets=["TOKEN_X"]))
    rejected = c.get("/api/extensions").json()["rejected"]
    assert rejected and "not approved" in rejected[0]["reasons"][0]
    assert "fixture.echo" not in {x["id"] for x in c.get("/api/capabilities").json()}


def test_a_task_can_use_an_mcp_tool_through_the_normal_gate(client):
    provider = ScriptedProvider(tool_turns=[tool_call("fixture__add", {"a": 20, "b": 22}), Completion("The sum is 42.")])
    c = client(manifest(), provider=provider)
    task_id = c.post("/api/tasks", json={"goal": "Add 20 and 22."}).json()["task_id"]
    deadline = time.time() + 20
    while c.get(f"/api/tasks/{task_id}").json()["status"] not in ("completed", "error", "failed_safe") and time.time() < deadline:
        time.sleep(0.05)
    task = c.get(f"/api/tasks/{task_id}").json()
    assert task["status"] == "completed" and task["steps"][0]["capability_id"] == "fixture.add" and task["steps"][0]["status"] == "ok"
    assert "fixture.add" in task["offered_capabilities"]
    tool_message = provider.calls[1]["messages"][-1]["content"]
    assert tool_message.startswith('<untrusted_data source="fixture.add"') and '"sum": 42' in tool_message
    assert task["tainted"]  # MCP output is untrusted, like any tool output
