"""Process supervisor (master prompt section 2).

Every process XODUS launches is registered with its PID, creation time (so a reused PID is never
mistaken for ours), tier, role, owner capability, start reason and idle timeout, and lives in its own
Job Object (see jobs.py). Stopping always goes through the job, never through a bare PID, so an
unrelated process that happens to reuse a PID cannot be killed. Optional processes are capped in
number, stopped after their idle timeout, and may carry a job memory limit.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import psutil

from xodus.obs import log_event
from xodus.security.audit import AuditLog
from xodus.supervisor.jobs import JobObject, spawn_in_job

_DEFAULT = object()


class SupervisorLimit(Exception):
    code = "process_limit"


@dataclass
class ManagedProcess:
    pid: int
    create_time: float
    tier: str
    role: str
    owner: str
    start_reason: str
    command: list[str]
    started_at: float
    idle_timeout_s: float | None
    memory_limit_bytes: int | None
    popen: subprocess.Popen[bytes]
    job: JobObject
    last_used: float = 0.0
    stderr_path: Path | None = None
    _stderr: IO[bytes] | None = field(default=None, repr=False)

    @property
    def key(self) -> tuple[int, float]:
        return self.pid, self.create_time


class Supervisor:
    def __init__(
        self,
        *,
        max_optional: int = 2,
        idle_timeout_s: float = 300.0,
        memory_limit_bytes: int | None = None,
        audit: AuditLog | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_optional = max_optional
        self.idle_timeout_s = idle_timeout_s
        self.memory_limit_bytes = memory_limit_bytes
        self._audit = audit
        self._clock = clock
        self._lock = threading.Lock()
        self._procs: dict[tuple[int, float], ManagedProcess] = {}

    def _record(self, event: str, **fields: Any) -> None:
        log_event(event, **fields)
        if self._audit is not None:
            self._audit.append(event, **fields)

    def launch(
        self,
        command: list[str],
        *,
        role: str,
        owner: str,
        start_reason: str,
        cwd: Path,
        env: dict[str, str] | None,
        tier: str = "optional",
        idle_timeout_s: float | None | object = _DEFAULT,
        memory_limit_bytes: int | None | object = _DEFAULT,
        pipes: bool = True,
        stderr_path: Path | None = None,
    ) -> ManagedProcess:
        if tier not in ("core", "optional"):
            raise ValueError(f"unknown tier {tier!r}")
        idle = (self.idle_timeout_s if tier == "optional" else None) if idle_timeout_s is _DEFAULT else idle_timeout_s
        limit = (self.memory_limit_bytes if tier == "optional" else None) if memory_limit_bytes is _DEFAULT else memory_limit_bytes
        with self._lock:
            running_optional = sum(1 for p in self._procs.values() if p.tier == "optional")
            if tier == "optional" and running_optional >= self.max_optional:
                raise SupervisorLimit(
                    f"At most {self.max_optional} optional processes may run at once (supported workload "
                    "envelope); try again when one has finished or gone idle."
                )
            job = JobObject(memory_limit_bytes=limit)  # type: ignore[arg-type]
            stderr_file = open(stderr_path, "ab") if stderr_path else None
            try:
                popen = spawn_in_job(
                    command, job, cwd=cwd, env=env,
                    stdin=subprocess.PIPE if pipes else subprocess.DEVNULL,
                    stdout=subprocess.PIPE if pipes else subprocess.DEVNULL,
                    stderr=stderr_file or subprocess.DEVNULL,
                )
                create_time = psutil.Process(popen.pid).create_time()
            except BaseException:
                job.close()
                if stderr_file:
                    stderr_file.close()
                raise
            mp = ManagedProcess(
                pid=popen.pid, create_time=create_time, tier=tier, role=role, owner=owner,
                start_reason=start_reason, command=list(command), started_at=time.time(),
                idle_timeout_s=idle, memory_limit_bytes=limit, popen=popen, job=job,  # type: ignore[arg-type]
                last_used=self._clock(), stderr_path=stderr_path, _stderr=stderr_file,
            )
            self._procs[mp.key] = mp
        self._record("process_started", pid=mp.pid, tier=tier, role=role, owner=owner, reason=start_reason,
                     memory_limit_bytes=limit, idle_timeout_s=idle)
        return mp

    def is_alive(self, mp: ManagedProcess) -> bool:
        try:
            proc = psutil.Process(mp.pid)
            return abs(proc.create_time() - mp.create_time) < 1e-3 and proc.is_running()
        except psutil.Error:
            return False

    def touch(self, mp: ManagedProcess) -> None:
        mp.last_used = self._clock()

    def managed(self) -> list[ManagedProcess]:
        with self._lock:
            return list(self._procs.values())

    def stop(self, mp: ManagedProcess, reason: str) -> None:
        """Terminate the whole job (root and every descendant), then release its handles."""
        with self._lock:
            known = self._procs.pop(mp.key, None) is not None
        mp.job.terminate()
        exit_code = None
        try:
            exit_code = mp.popen.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for stream in (mp.popen.stdin, mp.popen.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if mp._stderr is not None:
            mp._stderr.close()
        mp.job.close()
        if known:
            self._record("process_stopped", pid=mp.pid, role=mp.role, owner=mp.owner, reason=reason, exit_code=exit_code)

    def reap(self) -> list[ManagedProcess]:
        """Stop optional processes past their idle timeout, and clean up jobs whose root has exited."""
        stopped = []
        now = self._clock()
        for mp in self.managed():
            if not self.is_alive(mp):
                self.stop(mp, "exited")
                stopped.append(mp)
            elif mp.idle_timeout_s is not None and now - mp.last_used > mp.idle_timeout_s:
                self.stop(mp, "idle")
                stopped.append(mp)
        return stopped

    async def run_reaper(self, interval_s: float = 5.0) -> None:
        while True:
            await asyncio.sleep(interval_s)
            await asyncio.to_thread(self.reap)

    def stop_all(self, reason: str = "shutdown") -> None:
        for mp in self.managed():
            self.stop(mp, reason)

    def snapshot(self) -> list[dict[str, Any]]:
        now = self._clock()
        out = []
        for mp in self.managed():
            try:
                accounting = mp.job.accounting()
            except Exception:  # job already closing
                accounting = {}
            out.append({
                "pid": mp.pid,
                "tier": mp.tier,
                "role": mp.role,
                "owner": mp.owner,
                "start_reason": mp.start_reason,
                "alive": self.is_alive(mp),
                "idle_s": round(now - mp.last_used, 1),
                "idle_timeout_s": mp.idle_timeout_s,
                "memory_limit_bytes": mp.memory_limit_bytes,
                "job_pids": mp.job.pids(),
                "job_processes_started": accounting.get("total_processes"),
            })
        return out
