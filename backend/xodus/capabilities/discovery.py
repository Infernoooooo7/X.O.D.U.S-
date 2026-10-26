"""Capability discovery (master prompt 4.2): built-ins, allow-listed entry points, plugin manifests and MCP servers.

A folder in plugins/ holds either one subprocess plugin (manifest.json with an execution_mode) or one MCP server
(manifest.json with "kind": "mcp_server" listing its pinned tools). Each discovered extension is added to
registry.extensions so the UI can inventory it, with its process, whether or not it has been started.

Anything that fails validation is rejected with a reason that the API shows; nothing is half-loaded.
In-process code must be first-party: Python entry points are only loaded from distributions the
user lists in plugins.trusted_entry_point_distributions. Everything else runs out of process.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from xodus.capabilities.list_files import ListFiles
from xodus.capabilities.manifest import CapabilityManifest, McpServerManifest, PluginManifest
from xodus.capabilities.mcp_client import McpServer, McpToolCapability
from xodus.capabilities.plugin_runner import SubprocessCapability
from xodus.capabilities.registry import CapabilityRegistry
from xodus.config.settings import Settings
from xodus.security.paths import PathViolation, is_link
from xodus.security.policy import PermissionPolicy
from xodus.supervisor.supervisor import Supervisor

MAX_MANIFEST_BYTES = 64 * 1024
ENTRY_POINT_GROUP = "xodus.capabilities"
ManifestT = TypeVar("ManifestT", bound=BaseModel)


def read_manifest_json(path: Path) -> tuple[list[str], Any]:
    """Read a manifest file as JSON; never raises."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [f"cannot read manifest: {exc}"], None
    if len(raw) > MAX_MANIFEST_BYTES:
        return [f"manifest is larger than {MAX_MANIFEST_BYTES} bytes"], None
    try:
        return [], json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return [f"manifest is not valid UTF-8 JSON: {exc}"], None
    except RecursionError:
        return ["manifest is nested too deeply"], None


def load_plugin_manifest(path: Path) -> tuple[list[str], PluginManifest | None]:
    """Parse and validate a plugin manifest; never raises."""
    reasons, data = read_manifest_json(path)
    return (reasons, None) if reasons else validate_plugin_manifest(data)


def _validate(model: type[ManifestT], data: Any) -> tuple[list[str], ManifestT | None]:
    if not isinstance(data, dict):
        return ["manifest must be a JSON object"], None
    try:
        return [], model.model_validate(data)
    except ValidationError as exc:
        reasons = []
        for err in exc.errors()[:10]:
            loc = ".".join(str(part) for part in err["loc"]) or "manifest"
            reasons.append(f"{loc}: {err['msg']}")
        return reasons, None
    except RecursionError:
        return ["manifest is nested too deeply"], None
    except Exception as exc:  # defensive: a validator choked on something unexpected
        return [f"manifest could not be validated: {type(exc).__name__}"], None


def validate_plugin_manifest(data: Any) -> tuple[list[str], PluginManifest | None]:
    return _validate(PluginManifest, data)


def validate_mcp_manifest(data: Any) -> tuple[list[str], McpServerManifest | None]:
    return _validate(McpServerManifest, data)


def discover(
    registry: CapabilityRegistry,
    *,
    settings: Settings,
    policy: PermissionPolicy,
    supervisor: Supervisor,
    entry_points: Callable[..., Any] = importlib.metadata.entry_points,
) -> None:
    _builtins(registry, settings, policy)
    _entry_points(registry, settings, entry_points)
    _plugins(registry, settings, supervisor)


def _builtins(registry: CapabilityRegistry, settings: Settings, policy: PermissionPolicy) -> None:
    root_id = settings.capabilities.list_files_root
    root = policy.roots.get(root_id)
    if root is None:
        registry.reject("builtin", "fs.list_files", [f"root {root_id!r} is not configured in [[permissions.roots]]"])
        return
    registry.register(ListFiles(root_id, root.path, settings.capabilities.list_files_max_entries), "builtin")


def _entry_points(registry: CapabilityRegistry, settings: Settings, entry_points: Callable[..., Any]) -> None:
    trusted = set(settings.plugins.trusted_entry_point_distributions)
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        dist = ep.dist.name if getattr(ep, "dist", None) else "unknown"
        ref = f"{dist}:{ep.name}"
        if dist not in trusted:
            registry.reject("entry_point", ref, [
                "in-process capabilities must be first-party; list the distribution in "
                "plugins.trusted_entry_point_distributions to trust it"])
            continue
        try:
            cap = ep.load()()
            manifest = CapabilityManifest.model_validate(cap.manifest.model_dump())
        except ValidationError as exc:
            registry.reject("entry_point", ref, [f"invalid manifest: {e['msg']}" for e in exc.errors()[:10]])
            continue
        except Exception as exc:
            registry.reject("entry_point", ref, [f"failed to load: {type(exc).__name__}: {exc}"])
            continue
        if manifest.execution_mode != "in_process":
            registry.reject("entry_point", ref, ["entry points provide in-process capabilities only"])
            continue
        registry.register(cap, f"entry_point:{ref}")


def _plugins(registry: CapabilityRegistry, settings: Settings, supervisor: Supervisor) -> None:
    plugin_root = settings.plugins.dir
    if not plugin_root.is_dir():
        return
    for folder in sorted(plugin_root.iterdir()):
        if not folder.is_dir():
            continue
        ref = folder.name
        if is_link(folder):
            registry.reject("plugin", ref, ["plugin folder is a link; links are not followed"])
            continue
        manifest_path = folder / "manifest.json"
        if not manifest_path.is_file():
            registry.reject("plugin", ref, ["manifest.json not found"])
            continue
        reasons, data = read_manifest_json(manifest_path)
        if reasons:
            registry.reject("plugin", ref, reasons)
            continue
        if isinstance(data, dict) and data.get("kind") == "mcp_server":
            _mcp_server(registry, settings, supervisor, folder, data)
            continue
        reasons, manifest = validate_plugin_manifest(data)
        if manifest is None:
            registry.reject("plugin", ref, reasons)
            continue
        secrets = _secrets(registry, settings, "plugin", ref, manifest.id, manifest.secrets)
        if secrets is None:
            continue
        try:
            cap = SubprocessCapability(manifest, folder, supervisor, settings.data_dir / "plugins", secrets)
        except PathViolation as exc:
            registry.reject("plugin", ref, [f"entry script: {exc.message}"])
            continue
        registry.register(cap, f"plugin:{ref}")
        registry.extensions.append(PluginInventory(cap, folder))


def _secrets(registry: CapabilityRegistry, settings: Settings, source: str, ref: str, owner_id: str,
             names: tuple[str, ...]) -> dict[str, str] | None:
    """The approved secrets' values, or None (and a visible rejection) if any is unapproved or unset."""
    approved = set(settings.plugins.approved_secrets.get(owner_id, []))
    unapproved = [s for s in names if s not in approved]
    if unapproved:
        registry.reject(source, ref, [f"requests secret {s!r}, which is not approved in plugins.approved_secrets" for s in unapproved])
        return None
    secrets, missing = {}, []
    for name in names:
        value = os.environ.get(f"XODUS_SECRET_{name}")
        if value is None:
            missing.append(f"approved secret {name!r} has no value (set XODUS_SECRET_{name})")
        else:
            secrets[name] = value
    if missing:
        registry.reject(source, ref, missing)
        return None
    return secrets


def _mcp_server(registry: CapabilityRegistry, settings: Settings, supervisor: Supervisor, folder: Path, data: Any) -> None:
    ref = folder.name
    reasons, manifest = validate_mcp_manifest(data)
    if manifest is None:
        registry.reject("mcp_server", ref, reasons)
        return
    secrets = _secrets(registry, settings, "mcp_server", ref, f"mcp:{manifest.id}", manifest.secrets)
    if secrets is None:
        return
    try:
        server = McpServer(manifest, folder, supervisor, settings.data_dir / "plugins", secrets)
    except PathViolation as exc:
        registry.reject("mcp_server", ref, [f"entry script: {exc.message}"])
        return
    registry.extensions.append(server)
    for tool in manifest.tools:
        registry.register(McpToolCapability(tool, server), f"mcp:{manifest.id}")


class PluginInventory:
    """Inventory row for a subprocess plugin (the MCP server keeps its own)."""

    def __init__(self, cap: SubprocessCapability, folder: Path) -> None:
        self.cap = cap
        self.folder = folder

    def inventory(self) -> dict[str, Any]:
        m, proc = self.cap.manifest, self.cap.process
        return {
            "kind": "plugin", "id": m.id, "name": m.name, "version": m.version, "description": m.description,
            "folder": str(self.folder), "command": list(m.entry.command),
            "tools": [{"capability": m.id, "mcp_tool": None, "disabled_reason": None}],
            "process": {"pid": proc.pid, "alive": self.cap.supervisor.is_alive(proc), "tier": proc.tier} if proc else None,
            "paused": (self.cap.health() or [None])[0], "secrets": list(m.secrets),
        }
