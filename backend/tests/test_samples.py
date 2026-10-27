"""The two sample extensions shipped in plugins/: text.stats (subprocess plugin) and calc (MCP server).

They were added as folders only: no orchestrator (or other backend) file changed to support them (see the Gate E
report for the before/after file hashes). These tests load them from the real plugins/ folder.
"""

from __future__ import annotations

import importlib.util
import json
import time

import psutil
import pytest
from fastapi.testclient import TestClient

from tests.conftest import _ollama_up
from tests.helpers import ScriptedProvider, tool_call
from xodus.api.app import create_app
from xodus.config.settings import CONFIG_DIR, REPO_ROOT, load_settings
from xodus.providers.base import Completion

PLUGINS = REPO_ROOT / "plugins"


def load_calc():
    spec = importlib.util.spec_from_file_location("calc_server", PLUGINS / "calc_mcp" / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


calc = load_calc()


@pytest.fixture
def client(make_settings, fake_ollama, monkeypatch):
    monkeypatch.setenv("XODUS_PLUGINS__DIR", str(PLUGINS))
    opened = []

    def make(provider=None) -> TestClient:
        c = TestClient(create_app(make_settings(base_url=fake_ollama.url), provider=provider), base_url="http://127.0.0.1")
        c.__enter__()
        opened.append(c)
        return c

    yield make
    for c in opened:
        c.__exit__(None, None, None)


def invoke(c, cap: str, **args):
    return c.post(f"/api/capabilities/{cap}/invoke", json={"args": args})


# ---------------------------------------------------------------------------------------------- the safe evaluator


@pytest.mark.parametrize("expression, value", [
    ("1 + 2 * 3", 7), ("(17.5 * 4) / 3", 70 / 3), ("2^10", 1024), ("2 ** -1", 0.5), ("-(3 - 5)", 2), ("7 // 2", 3),
    ("7 % 4", 3), ("sqrt(16) + abs(-2)", 6.0), ("round(pi, 2)", 3.14), ("max(1, 5, 3)", 5), ("log10(1000)", 3.0),
])
def test_the_calculator_computes(expression, value):
    assert calc.evaluate(expression)["value"] == pytest.approx(value)


@pytest.mark.parametrize("expression, words", [
    ("__import__('os').system('calc')", "only numbers"),
    ("().__class__.__bases__[0].__subclasses__()", "only numbers"),
    ("open('C:/Windows/win.ini').read()", "only numbers"),
    ("lambda: 1", "only numbers"),
    ("[1, 2, 3]", "only numbers"),
    ("'a' * 10", "only numbers"),
    ("True + 1", "only numbers"),
    ("x + 1", "only numbers"),
    ("sqrt(x=4)", "only numbers"),
    ("9 ** 9 ** 9", "exponents are limited"),
    ("10 ** 99 * 10 ** 99", "too large"),
    ("1 / 0", "division by zero"),
    ("sqrt(-1)", "sqrt: "),
    ("1 +", "not valid arithmetic"),
    ("-" * 40 + "1", "nested too deeply"),  # parentheses add no tree nodes; chained operators do
    ("1" * 201, "limited to 200"),
])
def test_the_calculator_refuses_everything_else(expression, words):
    with pytest.raises(calc.CalcError, match=words):
        calc.evaluate(expression)


def test_describe():
    out = calc.describe([2, 4, 4, 4, 5, 5, 7, 9])
    assert out["count"] == 8 and out["mean"] == 5 and out["median"] == 4.5 and out["min"] == 2 and out["max"] == 9
    assert calc.describe([3])["stdev"] is None


# ---------------------------------------------------------------------------------------------- loaded from plugins/


def test_both_samples_are_discovered_and_available_without_being_started(client):
    c = client()
    caps = {x["id"]: x for x in c.get("/api/capabilities").json()}
    assert caps["text.stats"]["source"] == "plugin:text_stats" and caps["text.stats"]["execution_mode"] == "subprocess"
    assert caps["calc.evaluate"]["source"] == "mcp:calc" and caps["calc.describe"]["execution_mode"] == "mcp"
    assert all(caps[i]["available"] and caps[i]["process_tier"] == "optional" for i in ("text.stats", "calc.evaluate", "calc.describe"))
    ext = c.get("/api/extensions").json()
    assert {e["id"] for e in ext["extensions"]} == {"text.stats", "calc"} and ext["rejected"] == []
    assert all(e["process"] is None for e in ext["extensions"]) and c.app.state.supervisor.managed() == []


def test_text_stats(client):
    c = client()
    text = "The cat sat. The cat ran away!\n\nA dog barked at the cat?"
    r = invoke(c, "text.stats", text=text, top=2)
    assert r.status_code == 200, r.text
    out = r.json()["result"]
    assert (out["words"], out["sentences"], out["paragraphs"]) == (13, 3, 2)
    assert out["top_words"] == [{"word": "cat", "count": 3}, {"word": "sat", "count": 1}]
    assert invoke(c, "text.stats", text="").status_code == 422 and invoke(c, "text.stats", text="x", top=99).status_code == 422


def test_calc_over_mcp_and_the_server_spawns_nothing(client):
    c = client()
    r = invoke(c, "calc.evaluate", expression="(17.5 * 4) / 3 + sqrt(2)")
    assert r.status_code == 200 and r.json()["result"]["value"] == pytest.approx(70 / 3 + 2 ** 0.5)
    assert invoke(c, "calc.describe", numbers=[1, 2, 3, 4]).json()["result"]["median"] == 2.5
    for hostile in ("__import__('os').system('calc')", "open('x').read()", "9**9**9"):
        r = invoke(c, "calc.evaluate", expression=hostile)
        assert r.status_code == 502 and r.json()["error"]["code"] == "plugin_error"
    assert invoke(c, "calc.evaluate", expression="1" * 201).status_code == 422  # refused by the manifest schema first
    server = next(e for e in c.get("/api/extensions").json()["extensions"] if e["id"] == "calc")
    assert server["protocol_version"] == "2025-06-18" and server["server_info"]["name"] == "xodus-calc"
    assert all(t["disabled_reason"] is None for t in server["tools"])  # the reviewed pins match what the server offers
    mp = next(m for m in c.app.state.supervisor.managed() if m.owner == "mcp:calc")
    names = sorted(psutil.Process(p).name().lower() for p in mp.job.pids())
    # venv launcher + interpreter + the console host Windows attaches to a console program with redirected pipes;
    # the hostile expressions started nothing else
    assert names == ["conhost.exe", "python.exe", "python.exe"]
    optional = next(line for line in c.get("/api/resources").json()["lines"] if line["line"] == "optional")
    assert optional["processes"] >= 1 and optional["uss_mib"] > 0


def test_a_task_uses_both_samples_through_the_unchanged_orchestrator(client):
    provider = ScriptedProvider(tool_turns=[
        tool_call("calc__evaluate", {"expression": "1234.5 * 6.789"}),
        tool_call("text__stats", {"text": "One two three. Four five."}),
        Completion("1234.5 * 6.789 is 8380.9205 and the text has 5 words."),
    ])
    c = client(provider=provider)
    task_id = c.post("/api/tasks", json={"goal": "Multiply and count."}).json()["task_id"]
    deadline = time.time() + 30
    while c.get(f"/api/tasks/{task_id}").json()["status"] not in ("completed", "error", "failed_safe") and time.time() < deadline:
        time.sleep(0.05)
    task = c.get(f"/api/tasks/{task_id}").json()
    assert task["status"] == "completed", task
    assert [(s["capability_id"], s["status"]) for s in task["steps"]] == [("calc.evaluate", "ok"), ("text.stats", "ok")]
    tools_offered = provider.calls[0]["tools"]
    assert "calc__evaluate" in tools_offered and "text__stats" in tools_offered
    audit = [json.loads(line) for line in c.app.state.audit.path.read_text(encoding="utf-8").splitlines()]
    assert [a["capability"] for a in audit if a["event"] == "capability_result"] == ["calc.evaluate", "text.stats"]


@pytest.mark.ollama
def test_live_model_uses_the_calculator(tmp_path, monkeypatch):
    """Records (does not assert) whether the real model chooses the MCP calculator; asserts only what XODUS enforces."""
    if not _ollama_up():
        pytest.skip("Ollama is not running on 127.0.0.1:11434")
    monkeypatch.setenv("XODUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XODUS_LOG_DIR", str(tmp_path / "logs"))
    with TestClient(create_app(load_settings([CONFIG_DIR / "default.toml"])), base_url="http://127.0.0.1") as c:
        task_id = c.post("/api/tasks", json={"goal": "What is 1234.5 multiplied by 6.789? Use the calculator tool."}).json()["task_id"]
        deadline = time.time() + 240
        while c.get(f"/api/tasks/{task_id}").json()["status"] not in ("completed", "error", "failed_safe", "step_limit",
                                                                     "budget_exceeded", "blocked") and time.time() < deadline:
            time.sleep(0.5)
        task = c.get(f"/api/tasks/{task_id}").json()
    steps = [(s["capability_id"], s["args"], s["status"]) for s in task["steps"]]
    print("\nLIVE MCP TASK:", json.dumps({"status": task["status"], "steps": steps, "answer": (task["answer"] or "")[:300]}))
    assert task["status"] != "running"
    assert all(s[2] in ("ok", "blocked", "failed") for s in steps)
