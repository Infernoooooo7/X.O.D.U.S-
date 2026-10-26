"""Manifest validation, discovery and fuzzing (security table: malformed manifests)."""

from __future__ import annotations

import copy
import json
import random
from types import SimpleNamespace

import pytest

from tests.conftest import FIXTURES
from xodus.capabilities.discovery import discover, load_plugin_manifest, validate_plugin_manifest
from xodus.capabilities.registry import CapabilityRegistry
from xodus.security.policy import PermissionPolicy, Root
from xodus.supervisor.supervisor import Supervisor

VALID = {
    "id": "test.echo",
    "name": "Echo",
    "description": "Echo the arguments back.",
    "version": "1.0.0",
    "input_schema": {"type": "object", "additionalProperties": False, "properties": {"text": {"type": "string"}}},
    "output_schema": {"type": "object"},
    "permissions": ["net.none", "exec.none"],
    "execution_mode": "subprocess",
    "process_tier": "optional",
    "timeout_s": 5,
    "max_output_bytes": 10000,
    "requires_approval": "never",
    "side_effects": "none",
    "entry": {"command": ["python", "plugin_main.py", "echo"]},
}


def mutate(**changes):
    data = copy.deepcopy(VALID)
    for key, value in changes.items():
        if value is ...:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def test_valid_manifest_is_accepted():
    reasons, manifest = validate_plugin_manifest(VALID)
    assert reasons == [] and manifest is not None and manifest.id == "test.echo"


BAD = [
    (mutate(id="Echo"), "id"),
    (mutate(id="echo"), "id"),
    (mutate(id="test.echo__x"), "id"),  # would collide with test.echo.x as the tool name test__echo__x
    (mutate(id="test.echo_"), "id"),
    (mutate(id="test." + "a" * 70), "id"),
    (mutate(version="1"), "version"),
    (mutate(name=...), "name"),
    (mutate(unexpected=True), "Extra inputs"),
    (mutate(permissions=["fs.read:sandbox", "net.anything"]), "unknown permission"),
    (mutate(permissions=["fs.read:../etc"]), "unknown permission"),
    (mutate(permissions=["net.none", "net.outbound"]), "contradict"),
    (mutate(permissions=["exec.none", "exec.none"]), "duplicate"),
    (mutate(input_schema={"type": "object", "properties": {}}), "additionalProperties"),
    (mutate(input_schema={"type": "string"}), "type 'object'"),
    (mutate(input_schema={"type": "object", "additionalProperties": False, "properties": {"p": {"type": 5}}}), "valid JSON Schema"),
    (mutate(input_schema={"type": "object", "additionalProperties": False,
                          "properties": {"p": {"$ref": "https://evil.example/schema.json"}}}), "remote"),
    (mutate(input_schema={"type": "object", "additionalProperties": False,
                          "properties": {"p": {"type": "string", "x-xodus-path": "fs.read:sandbox"}}}), "does not declare"),
    (mutate(permissions=["fs.read:sandbox"], input_schema={"type": "object", "additionalProperties": False, "properties": {
        "p": {"type": "string", "x-xodus-path": "fs.read:sandbox", "x-xodus-path-kind": "new_file"}}}), "fs.write"),
    (mutate(permissions=["fs.read:sandbox"], input_schema={"type": "object", "additionalProperties": False, "properties": {
        "o": {"type": "object", "properties": {"p": {"type": "string", "x-xodus-path": "fs.read:sandbox"}}}}}), "top-level"),
    (mutate(permissions=["fs.write:sandbox"]), "side_effects"),
    (mutate(side_effects="external"), "require approval"),
    (mutate(execution_mode="in_process", process_tier="none"), "out of process"),
    (mutate(execution_mode="mcp"), "MCP"),
    (mutate(process_tier="none"), "process_tier"),
    (mutate(timeout_s=0), "timeout_s"),
    (mutate(timeout_s=10_000), "timeout_s"),
    (mutate(max_output_bytes=50_000_000), "max_output_bytes"),
    (mutate(requires_approval="sometimes"), "requires_approval"),
    (mutate(entry={"command": ["cmd.exe", "/c", "evil.bat"]}), "Python"),
    (mutate(entry={"command": ["python", "main.exe"]}), ".py"),
    (mutate(entry={"command": ["python"]}), "command"),
    (mutate(secrets=["lowercase"]), "UPPER_SNAKE"),
    (mutate(dependencies=["anything goes"]), "dependency"),
    ([1, 2, 3], "JSON object"),
    ("manifest", "JSON object"),
]


@pytest.mark.parametrize("data, reason", BAD, ids=[str(i) for i in range(len(BAD))])
def test_invalid_manifests_are_rejected_with_a_reason(data, reason):
    reasons, manifest = validate_plugin_manifest(data)
    assert manifest is None
    assert any(reason in r for r in reasons), reasons


def test_oversized_and_non_json_manifest_files(tmp_path):
    big = tmp_path / "big.json"
    big.write_text(json.dumps({**VALID, "description": "x" * 70_000}))
    assert "larger than" in load_plugin_manifest(big)[0][0]
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\xff\xfe not json")
    assert "not valid UTF-8 JSON" in load_plugin_manifest(bad)[0][0]
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 100_000 + "]" * 100_000)
    reasons, manifest = load_plugin_manifest(deep)
    assert manifest is None and reasons


def test_fuzzed_manifests_never_crash_and_always_explain():
    """1,500 random mutations of a valid manifest: each is either a valid manifest or rejected with reasons."""
    rng = random.Random(20261027)
    junk = [None, True, -1, 0, 1e308, "", "x" * 5000, [], {}, [None], {"$ref": "http://x"}, {"type": "object"},
            "../../etc", "\x00", {"x-xodus-path": "fs.write:sandbox"}, [["deep"]], "fs.read:sandbox"]

    def mutate_any(node, depth=0):
        if isinstance(node, dict) and node and rng.random() < 0.7:
            key = rng.choice(list(node))
            action = rng.random()
            if action < 0.3:
                node.pop(key)
            elif action < 0.6:
                node[key] = rng.choice(junk)
            elif action < 0.8:
                node[rng.choice(["x-xodus-path", "$ref", "type", "additionalProperties", key + "_"])] = rng.choice(junk)
            else:
                mutate_any(node[key], depth + 1)
        elif isinstance(node, list) and node:
            i = rng.randrange(len(node))
            node[i] = rng.choice(junk) if rng.random() < 0.6 else node[i]
        return node

    accepted = rejected = 0
    for _ in range(1500):
        data = copy.deepcopy(VALID)
        for _ in range(rng.randint(1, 4)):
            mutate_any(data)
        reasons, manifest = validate_plugin_manifest(data)
        if manifest is None:
            assert reasons and all(isinstance(r, str) and r for r in reasons)
            rejected += 1
        else:
            assert manifest.input_schema.get("additionalProperties") is False
            accepted += 1
    assert rejected > 1000  # nearly every random mutation is invalid


def _write_plugin(root, name, manifest):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "plugin_main.py").write_text((FIXTURES / "plugin_main.py").read_text(), encoding="utf-8")
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


def test_discovery_registers_valid_plugins_and_lists_rejections(make_settings, tmp_path, sandbox):
    plugins = tmp_path / "plugins"
    _write_plugin(plugins, "echo", VALID)
    _write_plugin(plugins, "broken", mutate(input_schema={"type": "object"}))
    _write_plugin(plugins, "dupe", mutate(id="fs.list_files", entry={"command": ["python", "plugin_main.py", "echo"]}))
    _write_plugin(plugins, "secret", mutate(id="test.secretive", secrets=["API_TOKEN"]))
    _write_plugin(plugins, "escape", mutate(id="test.escape", entry={"command": ["python", "../escape.py"]}))
    (plugins / "nomanifest").mkdir()
    settings = make_settings()
    policy = PermissionPolicy([Root("sandbox", sandbox.resolve(), frozenset({"fs.read"}))])
    registry = CapabilityRegistry(policy)
    discover(registry, settings=settings, policy=policy, supervisor=Supervisor(), entry_points=lambda group: [])
    assert registry.ids() == ["fs.list_files", "test.echo"]
    rejected = {r["ref"]: " ".join(r["reasons"]) for r in registry.describe_rejected()}
    assert "additionalProperties" in rejected["broken"]
    assert "duplicate id" in rejected["fs.list_files"]
    assert "not approved" in rejected["secret"]
    assert "entry script" in rejected["escape"] or "command" in rejected["escape"]
    assert "manifest.json not found" in rejected["nomanifest"]


def test_entry_points_need_an_allow_listed_distribution(make_settings, sandbox):
    loaded = []

    def fake_entry_points(group):
        loaded.append(group)
        return [SimpleNamespace(name="evil", dist=SimpleNamespace(name="random-package"),
                                load=lambda: (_ for _ in ()).throw(AssertionError("must not be loaded")))]

    settings = make_settings()
    policy = PermissionPolicy([Root("sandbox", sandbox.resolve(), frozenset({"fs.read"}))])
    registry = CapabilityRegistry(policy)
    discover(registry, settings=settings, policy=policy, supervisor=Supervisor(), entry_points=fake_entry_points)
    assert loaded == ["xodus.capabilities"]
    assert registry.ids() == ["fs.list_files"]
    assert "first-party" in registry.describe_rejected()[0]["reasons"][0]


def test_unavailable_reasons(make_settings, sandbox):
    from tests.helpers import WriteNote

    policy = PermissionPolicy([Root("sandbox", sandbox.resolve(), frozenset({"fs.read"}))])  # read-only
    registry = CapabilityRegistry(policy, disabled=["fs.list_files"])
    from xodus.capabilities.list_files import ListFiles

    registry.register(ListFiles("sandbox", sandbox), "builtin")
    registry.register(WriteNote("sandbox", sandbox), "test")
    reasons = {c["id"]: c["unavailable_reasons"] for c in registry.describe()}
    assert reasons["fs.list_files"] == ["disabled in config"]
    assert "not granted fs.write" in reasons["test.write_note"][0]
    assert registry.available_ids() == []
