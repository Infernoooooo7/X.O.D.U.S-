"""Security table, "Secrets leaking": a canary secret never appears in logs, prompts, the database or the audit log.

A plugin is approved to receive the canary (plugins.approved_secrets, value from XODUS_SECRET_<NAME>) and, like a buggy
or malicious plugin, echoes it in its output. A task calls the plugin through the real app; then every prompt sent to
the model, the JSON logs, the task log, the audit log and the raw database files are scanned for the value.
"""

from __future__ import annotations

import json
import secrets
import shutil
import time
from pathlib import Path

from tests.conftest import FIXTURES
from tests.helpers import ScriptedProvider, api_client, tool_call
from xodus.api.app import create_app
from xodus.obs import configure_logging, logger

PLUGIN_ID = "test.envcheck"


def install_echo_plugin(plugins: Path) -> None:
    folder = plugins / "envcheck"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "plugin_main.py", folder / "main.py")
    (folder / "manifest.json").write_text(json.dumps({
        "id": PLUGIN_ID, "name": "Environment check (test)", "version": "1.0.0",
        "description": "Reports its environment, including the secret it was given. Test fixture only.",
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {}},
        "output_schema": {"type": "object"},
        "permissions": ["net.none"], "execution_mode": "subprocess", "process_tier": "optional",
        "timeout_s": 10, "max_output_bytes": 20000, "requires_approval": "never", "side_effects": "none",
        "secrets": ["APPROVED_TOKEN"], "entry": {"command": ["python", "main.py", "env"]},
    }), encoding="utf-8")


def test_a_canary_secret_never_reaches_logs_prompts_database_or_audit(make_settings, tmp_path, monkeypatch):
    canary = "xodus-canary-" + secrets.token_hex(8)
    monkeypatch.setenv("XODUS_SECRET_APPROVED_TOKEN", canary)
    install_echo_plugin(tmp_path / "plugins")
    settings = make_settings(extra=f'\n[plugins.approved_secrets]\n"{PLUGIN_ID}" = ["APPROVED_TOKEN"]\n')
    provider = ScriptedProvider(tool_turns=[tool_call(PLUGIN_ID, {})], answer="Done.")
    configure_logging(settings.log_dir, to_stdout=False)
    try:
        app = create_app(settings, provider=provider)
        with api_client(app) as client:
            assert PLUGIN_ID in [c["id"] for c in client.get("/api/capabilities").json() if c["available"]]
            direct = client.post(f"/api/capabilities/{PLUGIN_ID}/invoke", json={"args": {}}).json()
            assert direct["ok"] and direct["result"]["approved"] == canary  # the plugin really had the secret
            task = client.post("/api/tasks", json={"goal": "Check the plugin environment."}).json()["task_id"]
            deadline = time.time() + 30
            while client.get(f"/api/tasks/{task}").json()["status"] not in ("completed", "failed", "cancelled"):
                assert time.time() < deadline
                time.sleep(0.1)
            assert client.get(f"/api/tasks/{task}").json()["status"] == "completed"
    finally:
        for handler in list(logger.handlers):
            handler.close()
        logger.handlers.clear()

    prompts = json.dumps(provider.calls, default=str)
    assert '\\"approved\\": \\"[redacted]\\"' in prompts  # the tool result reached the model (JSON in JSON), without the value
    assert canary not in prompts
    scanned = [p for folder in (settings.log_dir, settings.data_dir) for p in folder.rglob("*") if p.is_file()]
    assert {"xodus.jsonl", "tasks.jsonl", "audit.jsonl", "xodus.db"} <= {p.name for p in scanned}
    leaks = [str(p) for p in scanned if canary.encode() in p.read_bytes() and "plugins" not in p.parts]
    assert leaks == []
