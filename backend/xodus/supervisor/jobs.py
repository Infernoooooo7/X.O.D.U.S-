"""Windows Job Objects for processes XODUS launches (master prompt section 2).

Each launched process gets its own job configured with KILL_ON_JOB_CLOSE: when the job handle
closes - backend shutdown, crash or a hard kill - Windows terminates every process still in the job,
grandchildren included. Processes are created suspended and assigned to the job before their first
instruction runs, so no child can be spawned outside the job in between. DIE_ON_UNHANDLED_EXCEPTION
stops a crashing process from hanging on an error-reporting dialog.

A job is a lifetime and resource boundary. It is NOT a security sandbox: processes in it run with
the user's full rights (file system, network, registry).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import IO, Any

import psutil

try:
    import pywintypes
    import win32job
except ImportError:  # pragma: no cover - non-Windows
    win32job = None
    pywintypes = None

CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400


class JobError(Exception):
    pass


class JobObject:
    def __init__(self, memory_limit_bytes: int | None = None) -> None:
        if win32job is None:
            raise JobError("Windows Job Objects are unavailable (pywin32 missing or not Windows).")
        self.memory_limit_bytes = memory_limit_bytes
        self.closed = False
        self.handle = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation)
        flags = info["BasicLimitInformation"]["LimitFlags"]
        flags |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        flags |= getattr(win32job, "JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION", _DIE_ON_UNHANDLED_EXCEPTION)
        if memory_limit_bytes:
            flags |= win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
            info["JobMemoryLimit"] = memory_limit_bytes
        info["BasicLimitInformation"]["LimitFlags"] = flags
        win32job.SetInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation, info)

    def assign(self, process_handle: int) -> None:
        win32job.AssignProcessToJobObject(self.handle, process_handle)

    def pids(self) -> list[int]:
        """Every process currently in the job (descendants included, even if their parent exited)."""
        if self.closed:
            return []
        try:
            return [int(p) for p in win32job.QueryInformationJobObject(self.handle, win32job.JobObjectBasicProcessIdList)]
        except pywintypes.error:
            return []

    def accounting(self) -> dict[str, Any]:
        """Process counts. PeakJobMemoryUsed is deliberately not exposed: measured on this machine it
        includes commit attempts the limit refused (about 545 MB reported for a 128 MB job whose 512 MB
        allocation failed), so it is not a measure of memory actually used."""
        if self.closed:
            return {}
        info = win32job.QueryInformationJobObject(self.handle, win32job.JobObjectBasicAccountingInformation)
        return {"active_processes": info["ActiveProcesses"], "total_processes": info["TotalProcesses"]}

    def terminate(self, exit_code: int = 1) -> None:
        if self.closed:
            return
        try:
            win32job.TerminateJobObject(self.handle, exit_code)
        except pywintypes.error:
            pass

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.handle.Close()


def spawn_in_job(
    command: list[str],
    job: JobObject,
    *,
    cwd: Path,
    env: dict[str, str] | None,
    stdin: int | IO[bytes] | None,
    stdout: int | IO[bytes] | None,
    stderr: int | IO[bytes] | None,
) -> subprocess.Popen[bytes]:
    """Start a process suspended, put it in the job, then let it run."""
    popen = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        creationflags=CREATE_SUSPENDED | CREATE_NO_WINDOW,
    )
    try:
        job.assign(int(popen._handle))  # type: ignore[attr-defined]  # the CreateProcess handle
        psutil.Process(popen.pid).resume()  # NtResumeProcess
    except BaseException:
        popen.kill()
        popen.wait()
        raise
    return popen
