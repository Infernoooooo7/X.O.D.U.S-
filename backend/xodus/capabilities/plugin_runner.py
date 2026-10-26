"""Out-of-process capabilities (master prompt 4.3): a supervised subprocess speaking JSON-RPC 2.0.

Contract: one JSON object per line on stdin/stdout.
  request : {"jsonrpc": "2.0", "id": N, "method": "invoke",
             "params": {"capability": "<id>", "args": {...}, "roots": {"<root id>": "<absolute path>"}}}
  response: {"jsonrpc": "2.0", "id": N, "result": {...}}  or  {"jsonrpc": "2.0", "id": N, "error": {"message": "..."}}

The process is started on first use (optional tier), lives in its own Job Object, gets a scrubbed
environment (no secrets unless approved in config), its own working folder, a timeout and an output
byte cap. On timeout, cancellation, oversized output or a protocol violation the whole process tree
is killed; it restarts on the next call. Three failures within a minute pause the capability.
Process isolation is not a sandbox: plugin code runs with the user's rights.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from xodus.capabilities.manifest import PluginManifest
from xodus.capabilities.registry import (
    CapabilityTimeout,
    InvalidOutput,
    InvocationContext,
    OutputTooLarge,
    PluginCrashed,
    PluginError,
)
from xodus.security.paths import resolve_existing
from xodus.supervisor.supervisor import ManagedProcess, Supervisor


class _Exited(Exception):
    pass


class _Oversized(Exception):
    pass


def scrubbed_env(work_dir: Path, secrets: dict[str, str]) -> dict[str, str]:
    """Only what a Python process needs to start on Windows, plus explicitly approved secrets."""
    env = {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS",
                                      "PROCESSOR_ARCHITECTURE") if k in os.environ}
    system32 = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), str(system32)])
    tmp = work_dir / "tmp"
    env.update(TEMP=str(tmp), TMP=str(tmp), PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    env.update(secrets)
    return env


def _send(proc: ManagedProcess, message: dict[str, Any]) -> None:
    assert proc.popen.stdin is not None
    proc.popen.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
    proc.popen.stdin.flush()


def _read_line(proc: ManagedProcess, cap: int) -> bytes:
    assert proc.popen.stdout is not None
    line = proc.popen.stdout.readline(cap + 1)
    if not line:
        raise _Exited()
    if len(line) > cap:
        raise _Oversized()
    return line


class SubprocessCapability:
    def __init__(
        self,
        manifest: PluginManifest,
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
        self.work_dir = work_root / manifest.id
        self.secrets = dict(secrets or {})
        self._max_failures = max_failures
        self._window = failure_window_s
        self._clock = clock
        self._failures: deque[float] = deque(maxlen=max_failures)
        self._proc: ManagedProcess | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0

    def health(self) -> list[str]:
        now = self._clock()
        recent = [t for t in self._failures if now - t < self._window]
        if len(recent) >= self._max_failures:
            return [f"plugin failed {len(recent)} times in the last {self._window:.0f} s; paused"]
        return []

    @property
    def process(self) -> ManagedProcess | None:
        return self._proc

    def _command(self) -> list[str]:
        return [sys.executable, "-E", "-s", "-B", str(self.script), *self.manifest.entry.command[2:]]

    async def _ensure_started(self) -> ManagedProcess:
        if self._proc is not None and self.supervisor.is_alive(self._proc):
            return self._proc
        if self._proc is not None:
            await asyncio.to_thread(self.supervisor.stop, self._proc, "exited")
        (self.work_dir / "tmp").mkdir(parents=True, exist_ok=True)
        self._proc = await asyncio.to_thread(
            self.supervisor.launch,
            self._command(),
            role="plugin",
            owner=self.manifest.id,
            start_reason="first use" if self._next_id == 0 else "restart after stop",
            cwd=self.work_dir,
            env=scrubbed_env(self.work_dir, self.secrets),
            tier="optional",
            stderr_path=self.work_dir / "stderr.log",
        )
        return self._proc

    async def _kill(self, reason: str) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            await asyncio.to_thread(self.supervisor.stop, proc, reason)

    def _fail(self) -> None:
        self._failures.append(self._clock())

    async def invoke(self, args: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        async with self._lock:
            proc = await self._ensure_started()
            self._next_id += 1
            request_id = self._next_id
            request = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "invoke",
                "params": {"capability": self.manifest.id, "args": args,
                           "roots": {k: str(v) for k, v in ctx.roots.items()}},
            }
            try:
                await asyncio.to_thread(_send, proc, request)
                raw = await asyncio.wait_for(
                    asyncio.to_thread(_read_line, proc, self.manifest.max_output_bytes), timeout=self.manifest.timeout_s
                )
            except TimeoutError:
                self._fail()
                await self._kill("timeout")
                raise CapabilityTimeout(f"{self.manifest.id} did not answer within {self.manifest.timeout_s:.0f} s; it was stopped.") from None
            except asyncio.CancelledError:
                ctx.cancel.set()
                await asyncio.shield(self._kill("cancelled"))
                raise
            except _Oversized:
                self._fail()
                await self._kill("output too large")
                raise OutputTooLarge(
                    f"{self.manifest.id} produced more than {self.manifest.max_output_bytes} bytes; the output was "
                    "discarded and the plugin stopped."
                ) from None
            except (_Exited, OSError):
                try:  # EOF arrives slightly before the process (and a venv launcher parent) finishes exiting
                    code = await asyncio.to_thread(proc.popen.wait, 2)
                except Exception:
                    code = None
                self._fail()
                await self._kill("crashed")
                raise PluginCrashed(f"{self.manifest.id} exited unexpectedly (exit code {code}).") from None
            self.supervisor.touch(proc)
            try:
                return self._parse(raw, request_id)
            except InvalidOutput:
                await self._kill("protocol violation")  # out of sync: start clean on the next call
                raise

    def _parse(self, raw: bytes, request_id: int) -> dict[str, Any]:
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._fail()
            raise InvalidOutput(f"{self.manifest.id} sent a reply that is not JSON.") from None
        if not isinstance(response, dict) or response.get("id") != request_id:
            self._fail()
            raise InvalidOutput(f"{self.manifest.id} sent a reply that does not match the request.")
        if "error" in response:
            error = response["error"]
            message = error.get("message") if isinstance(error, dict) else error
            raise PluginError(f"{self.manifest.id} reported an error: {str(message)[:300]}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise InvalidOutput(f"{self.manifest.id} returned a result that is not a JSON object.")
        return result

    async def shutdown(self) -> None:
        await self._kill("shutdown")
