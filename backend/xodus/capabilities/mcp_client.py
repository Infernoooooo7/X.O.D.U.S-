"""MCP servers as capability providers (master prompt 4.2 and 4.3): a minimal Model Context Protocol client over stdio.

Transport: newline-delimited JSON-RPC 2.0 on the server's stdin/stdout. XODUS implements the client side of
initialize, notifications/initialized, tools/list and tools/call, and nothing else:

- Requests the server sends to XODUS (sampling/createMessage, roots/list, elicitation/create, ...) are refused with
  "method not found". A server can never use XODUS's model, see its folders or ask the user anything.
- Notifications (logging, progress, list_changed) are read and dropped.

Each tool is declared in the server's manifest.json (McpToolManifest), reviewed like any plugin. What the user
reviewed is pinned: after every (re)connection the input schema each tool advertises is compared with the manifest,
and a tool whose definition changed is disabled ("rug pull" protection). The model only ever sees the manifest's
description, never the server's. Arguments are validated against the manifest before the server sees them.

The server is an optional-tier process: started on first use, in its own Job Object with a scrubbed environment,
work folder, timeout, output cap and idle stop, and counted by the resource sampler. Process isolation is not a
sandbox: server code runs with the user's rights.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from xodus import __version__
from xodus.capabilities.manifest import McpServerManifest, McpToolManifest, strip_annotations
from xodus.capabilities.plugin_runner import LineTooLong, ProcessExited, read_line, scrubbed_env, send_line
from xodus.capabilities.registry import (
    CapabilityTimeout,
    InvalidOutput,
    InvocationContext,
    OutputTooLarge,
    PluginCrashed,
    PluginError,
    ProcessLimit,
)
from xodus.obs import log_event
from xodus.security.paths import resolve_existing
from xodus.supervisor.supervisor import ManagedProcess, Supervisor, SupervisorLimit

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
_IGNORED_SCHEMA_KEYS = {"$schema", "additionalProperties"}  # XODUS's pinned schema may be stricter than the server's
MAX_NOTIFICATIONS_PER_CALL = 1000
MAX_LIST_PAGES = 10


class McpProtocolError(InvalidOutput):
    code = "mcp_protocol_error"


class _Deadline(Exception):
    pass


def comparable_schema(schema: Any) -> str:
    """Canonical JSON of an input schema for the pin check: XODUS annotations and the top-level keys XODUS may
    tighten are left out, so a manifest can add "additionalProperties": false to a server's schema."""
    if not isinstance(schema, dict):
        return json.dumps(schema, sort_keys=True)
    clean = {k: v for k, v in strip_annotations(schema).items() if k not in _IGNORED_SCHEMA_KEYS}
    return json.dumps(clean, sort_keys=True, separators=(",", ":"))


class McpServer:
    def __init__(
        self,
        manifest: McpServerManifest,
        plugin_dir: Path,
        supervisor: Supervisor,
        work_root: Path,
        secrets: dict[str, str] | None = None,
        *,
        max_failures: int = 3,
        failure_window_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.manifest = manifest
        self.plugin_dir = plugin_dir.resolve()
        self.script = resolve_existing(self.plugin_dir, manifest.entry.command[1], "file")
        self.supervisor = supervisor
        self.work_dir = work_root / f"mcp-{manifest.id}"
        self.secrets = dict(secrets or {})
        self._max_failures = max_failures
        self._window = failure_window_s
        self._clock = clock
        self._failures: deque[float] = deque(maxlen=max_failures)
        self._proc: ManagedProcess | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0
        self.protocol_version: str | None = None
        self.server_info: dict[str, Any] | None = None
        self.drift: dict[str, str] = {}  # mcp tool name -> why it is disabled
        self.starts = 0
        self.refused_requests: list[str] = []

    # ------------------------------------------------------------------ state

    def paused(self) -> str | None:
        now = self._clock()
        recent = [t for t in self._failures if now - t < self._window]
        if len(recent) >= self._max_failures:
            return f"MCP server {self.manifest.id} failed {len(recent)} times in the last {self._window:.0f} s; paused"
        return None

    def health(self, tool: str) -> list[str]:
        reasons = []
        if paused := self.paused():
            reasons.append(paused)
        if tool in self.drift:
            reasons.append(self.drift[tool])
        return reasons

    @property
    def process(self) -> ManagedProcess | None:
        return self._proc

    def inventory(self) -> dict[str, Any]:
        proc = self._proc
        alive = proc is not None and self.supervisor.is_alive(proc)
        return {
            "kind": "mcp_server", "id": self.manifest.id, "name": self.manifest.name, "version": self.manifest.version,
            "description": self.manifest.description, "folder": str(self.plugin_dir), "command": list(self.manifest.entry.command),
            "tools": [{"capability": t.id, "mcp_tool": t.mcp_tool, "disabled_reason": self.drift.get(t.mcp_tool)}
                      for t in self.manifest.tools],
            "process": {"pid": proc.pid, "alive": alive, "tier": proc.tier} if proc is not None else None,
            "starts": self.starts, "protocol_version": self.protocol_version, "server_info": self.server_info,
            "paused": self.paused(), "refused_server_requests": self.refused_requests[-10:],
            "secrets": list(self.manifest.secrets),
        }

    # ------------------------------------------------------------------ process and protocol

    def _fail(self) -> None:
        self._failures.append(self._clock())

    async def _kill(self, reason: str) -> None:
        proc, self._proc = self._proc, None
        self.protocol_version = None
        if proc is not None:
            await asyncio.to_thread(self.supervisor.stop, proc, reason)

    def _send(self, proc: ManagedProcess, message: dict[str, Any]) -> None:
        send_line(proc, {"jsonrpc": "2.0", **message})

    async def _receive(self, proc: ManagedProcess, request_id: int, deadline: float, cap: int) -> dict[str, Any]:
        """Read until the response to request_id arrives. Refuses server requests, drops notifications."""
        notifications = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Deadline()
            try:
                raw = await asyncio.wait_for(asyncio.to_thread(read_line, proc, cap), timeout=remaining)
            except TimeoutError:
                raise _Deadline() from None
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise McpProtocolError(f"MCP server {self.manifest.id} sent a line that is not JSON.") from None
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise McpProtocolError(f"MCP server {self.manifest.id} sent something that is not a JSON-RPC 2.0 message.")
            if "method" in message:
                if "id" in message:  # a request from the server: refuse it
                    method = str(message.get("method"))[:64]
                    self.refused_requests.append(method)
                    log_event("mcp_server_request_refused", server=self.manifest.id, method=method)
                    await asyncio.to_thread(self._send, proc, {"id": message["id"], "error": {
                        "code": -32601, "message": f"XODUS does not support {method}"}})
                notifications += 1
                if notifications > MAX_NOTIFICATIONS_PER_CALL:
                    raise McpProtocolError(f"MCP server {self.manifest.id} sent too many messages without answering.")
                continue
            if message.get("id") != request_id:
                raise McpProtocolError(f"MCP server {self.manifest.id} answered a request that was not made.")
            return message

    async def _request(self, proc: ManagedProcess, method: str, params: dict[str, Any], deadline: float, cap: int) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        await asyncio.to_thread(self._send, proc, {"id": request_id, "method": method, "params": params})
        message = await self._receive(proc, request_id, deadline, cap)
        if "error" in message:
            error = message["error"]
            text = error.get("message") if isinstance(error, dict) else error
            raise PluginError(f"MCP server {self.manifest.id} refused {method}: {str(text)[:300]}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpProtocolError(f"MCP server {self.manifest.id} returned a {method} result that is not an object.")
        return result

    async def _start(self) -> ManagedProcess:
        (self.work_dir / "tmp").mkdir(parents=True, exist_ok=True)
        proc = await asyncio.to_thread(
            self.supervisor.launch,
            [sys.executable, "-E", "-s", "-B", str(self.script), *self.manifest.entry.command[2:]],
            role="mcp", owner=f"mcp:{self.manifest.id}", start_reason="first use" if self.starts == 0 else "restart after stop",
            cwd=self.work_dir, env=scrubbed_env(self.work_dir, self.secrets), tier="optional",
            stderr_path=self.work_dir / "stderr.log")
        self.starts += 1
        self._proc = proc
        deadline = time.monotonic() + self.manifest.init_timeout_s
        cap = 1_000_000
        init = await self._request(proc, "initialize", {
            "protocolVersion": PROTOCOL_VERSIONS[0],
            "capabilities": {},  # XODUS offers no client capabilities: no sampling, no roots, no elicitation
            "clientInfo": {"name": "xodus", "version": __version__}}, deadline, cap)
        version = init.get("protocolVersion")
        if version not in PROTOCOL_VERSIONS:
            raise McpProtocolError(f"MCP server {self.manifest.id} speaks protocol version {str(version)[:32]!r}; XODUS "
                                   f"supports {', '.join(PROTOCOL_VERSIONS)}.")
        if not isinstance(init.get("capabilities"), dict) or "tools" not in init["capabilities"]:
            raise McpProtocolError(f"MCP server {self.manifest.id} does not offer tools.")
        info = init.get("serverInfo")
        self.server_info = {k: str(v)[:100] for k, v in info.items() if k in ("name", "version", "title")} if isinstance(info, dict) else None
        self.protocol_version = version
        await asyncio.to_thread(self._send, proc, {"method": "notifications/initialized"})
        await self._verify_pins(proc, deadline, cap)
        return proc

    async def _verify_pins(self, proc: ManagedProcess, deadline: float, cap: int) -> None:
        advertised: dict[str, Any] = {}
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            page = await self._request(proc, "tools/list", {"cursor": cursor} if cursor else {}, deadline, cap)
            for tool in page.get("tools") or []:
                if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                    advertised[tool["name"]] = tool.get("inputSchema")
            cursor = page.get("nextCursor")
            if not cursor:
                break
        drift = {}
        for tool in self.manifest.tools:
            if tool.mcp_tool not in advertised:
                drift[tool.mcp_tool] = f"the MCP server no longer offers the tool {tool.mcp_tool!r}"
            elif comparable_schema(advertised[tool.mcp_tool]) != comparable_schema(tool.input_schema):
                drift[tool.mcp_tool] = (f"the MCP server's definition of {tool.mcp_tool!r} differs from the reviewed manifest; "
                                        "review the change and update manifest.json to enable it again")
        if drift != self.drift:
            log_event("mcp_tool_drift", server=self.manifest.id, disabled=sorted(drift))
        self.drift = drift

    async def _ensure_ready(self) -> ManagedProcess:
        if self._proc is not None and self.supervisor.is_alive(self._proc) and self.protocol_version:
            return self._proc
        if self._proc is not None:
            await self._kill("exited")
        try:
            return await self._start()
        except SupervisorLimit as exc:  # nothing was started; not a failure of this server
            raise ProcessLimit(str(exc)) from None
        except asyncio.CancelledError:  # the caller's timeout or a cancelled task: never leave a half-started server
            await asyncio.shield(self._kill("cancelled while starting"))
            raise
        except _Deadline:
            self._fail()
            await self._kill("initialize timeout")
            raise CapabilityTimeout(f"MCP server {self.manifest.id} did not finish starting within "
                                    f"{self.manifest.init_timeout_s:.0f} s; it was stopped.") from None
        except (ProcessExited, OSError):
            self._fail()
            await self._kill("crashed")
            raise PluginCrashed(f"MCP server {self.manifest.id} exited while starting; see its stderr.log.") from None
        except (LineTooLong, McpProtocolError, PluginError):
            self._fail()
            await self._kill("protocol violation")
            raise

    # ------------------------------------------------------------------ calls

    async def call(self, tool: McpToolManifest, args: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        async with self._lock:
            proc = await self._ensure_ready()
            with self.supervisor.in_use(proc):  # never stopped as idle by the resource governor mid-call
                return await self._call(proc, tool, args, ctx)

    async def _call(self, proc: ManagedProcess, tool: McpToolManifest, args: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        if tool.mcp_tool in self.drift:
            raise PluginError(f"{tool.id} is disabled: {self.drift[tool.mcp_tool]}.")
        try:
            result = await self._request(proc, "tools/call", {"name": tool.mcp_tool, "arguments": args},
                                         time.monotonic() + tool.timeout_s, tool.max_output_bytes)
        except _Deadline:
            self._fail()
            await self._kill("timeout")
            raise CapabilityTimeout(f"{tool.id} did not answer within {tool.timeout_s:.0f} s; the MCP server was stopped.") from None
        except asyncio.CancelledError:
            ctx.cancel.set()
            await asyncio.shield(self._kill("cancelled"))
            raise
        except LineTooLong:
            self._fail()
            await self._kill("output too large")
            raise OutputTooLarge(f"{tool.id} produced more than {tool.max_output_bytes} bytes; the output was discarded "
                                 "and the MCP server stopped.") from None
        except (ProcessExited, OSError):
            self._fail()
            await self._kill("crashed")
            raise PluginCrashed(f"MCP server {self.manifest.id} exited unexpectedly during {tool.id}.") from None
        except McpProtocolError:
            self._fail()
            await self._kill("protocol violation")
            raise
        self.supervisor.touch(proc)
        return self._unwrap(tool, result)

    def _unwrap(self, tool: McpToolManifest, result: dict[str, Any]) -> dict[str, Any]:
        content = result.get("content") if isinstance(result.get("content"), list) else []
        texts = [c["text"] for c in content if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)]
        if result.get("isError"):
            raise PluginError(f"{tool.id} reported an error: {' '.join(texts)[:300] or 'no details'}")
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        out: dict[str, Any] = {"text": "\n".join(texts)}
        others = len(content) - len(texts)
        if others:
            out["non_text_items"] = others  # images, audio and resources are not passed on
        return out

    async def shutdown(self) -> None:
        await self._kill("shutdown")


class McpToolCapability:
    """One pinned tool of an MCP server, registered like any other capability."""

    def __init__(self, manifest: McpToolManifest, server: McpServer) -> None:
        self.manifest = manifest
        self.server = server

    def health(self) -> list[str]:
        return self.server.health(self.manifest.mcp_tool)

    async def invoke(self, args: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        return await self.server.call(self.manifest, args, ctx)
