"""Resource sampling for XODUS-related processes (master prompt 3.3, 3.4).

Per process: working set (psutil rss), private working set / USS (psutil memory_full_info().uss,
psutil 7.x API), private commit (psutil vms == pagefile on Windows), and dedicated/shared GPU
memory from the Windows "GPU Process Memory" performance counters.
Device-wide: used VRAM from NVML and system physical memory in use (total - available).

Every reader degrades to None plus an error string instead of raising, so a sample never
breaks a request.
"""

from __future__ import annotations

import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psutil

try:  # Windows-only performance counters
    import win32pdh
except ImportError:  # pragma: no cover - non-Windows
    win32pdh = None

try:
    import pynvml
except ImportError:  # pragma: no cover
    pynvml = None


@dataclass
class ProcessSample:
    pid: int
    name: str
    group: str  # xodus_app | ollama | optional
    tier: str  # core | optional
    rss: int | None = None
    uss: int | None = None
    private_commit: int | None = None
    gpu_dedicated: int | None = None
    gpu_shared: int | None = None
    errors: list[str] = field(default_factory=list)


def process_tree(root: psutil.Process) -> list[psutil.Process]:
    try:
        return [root, *root.children(recursive=True)]
    except psutil.Error:
        return [root]


def listener_pid(port: int) -> int | None:
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:
                return conn.pid
    except psutil.Error:
        return None
    return None


def backend_root() -> psutil.Process:
    """The backend process, or its venv launcher parent (Windows venvs start python via a launcher)."""
    me = psutil.Process()
    try:
        parent = me.parent()
        launcher = Path(sys.prefix) / "Scripts" / "python.exe"
        if parent is not None and Path(parent.exe()).resolve() == launcher.resolve():
            return parent
    except (psutil.Error, OSError):
        pass
    return me


def read_process(proc: psutil.Process, group: str, tier: str) -> ProcessSample:
    try:
        sample = ProcessSample(pid=proc.pid, name=proc.name(), group=group, tier=tier)
    except psutil.Error as exc:
        return ProcessSample(pid=proc.pid, name="?", group=group, tier=tier, errors=[type(exc).__name__])
    try:
        try:
            full = proc.memory_full_info()
        except psutil.AccessDenied:
            # Measured in the Gate B smoke benchmark: a single read of ollama.exe was refused once in 146 samples while
            # a runner started. One retry in the same tick; a second refusal is reported, never guessed.
            full = proc.memory_full_info()
        sample.rss, sample.uss, sample.private_commit = full.rss, full.uss, full.vms
    except psutil.AccessDenied:
        sample.errors.append("uss: AccessDenied (needs elevation)")
        try:
            basic = proc.memory_info()
            sample.rss, sample.private_commit = basic.rss, basic.vms
        except psutil.Error as exc:
            sample.errors.append(f"memory_info: {type(exc).__name__}")
    except psutil.Error as exc:
        sample.errors.append(f"memory_full_info: {type(exc).__name__}")
    return sample


def vanished(sample: ProcessSample) -> bool:
    """True if the process exited while it was being read (Ollama starts short-lived probe runners, for example)."""
    return any("NoSuchProcess" in e for e in sample.errors)


class GpuCounters:
    """Windows 'GPU Process Memory' and 'GPU Adapter Memory' counters (what Task Manager shows)."""

    def __init__(self) -> None:
        self.error: str | None = None if win32pdh else "pywin32 not installed"

    def read(self) -> tuple[dict[int, tuple[int, int]], str | None]:
        """Return ({pid: (dedicated, shared)} on the discrete adapter, discrete adapter LUID)."""
        if win32pdh is None:
            return {}, None
        query = None
        try:
            query = win32pdh.OpenQuery()
            counters = {
                name: win32pdh.AddEnglishCounter(query, path)
                for name, path in (
                    ("ded", r"\GPU Process Memory(*)\Dedicated Usage"),
                    ("shr", r"\GPU Process Memory(*)\Shared Usage"),
                    ("adapter_ded", r"\GPU Adapter Memory(*)\Dedicated Usage"),
                )
            }
            win32pdh.CollectQueryData(query)
            values = {
                name: win32pdh.GetFormattedCounterArray(handle, win32pdh.PDH_FMT_LARGE)
                for name, handle in counters.items()
            }
        except Exception as exc:  # pywintypes.error; invalid samples when a process exits mid-read
            self.error = f"{type(exc).__name__}: {exc}"
            return {}, None
        finally:
            if query is not None:
                win32pdh.CloseQuery(query)
        self.error = None

        # The discrete GPU is the adapter with the most dedicated memory in use; integrated
        # adapters have (almost) none. Shared usage is only "spill" on the discrete adapter.
        adapters = values["adapter_ded"]
        discrete = max(adapters, key=adapters.get).split("_phys")[0].lower() if adapters else None
        per_pid: dict[int, tuple[int, int]] = {}
        for key in ("ded", "shr"):
            for instance, value in values[key].items():
                parts = instance.lower().split("_")
                if len(parts) < 3 or parts[0] != "pid":
                    continue
                luid = "_".join(parts[2:]).split("_phys")[0]
                if discrete and luid != discrete:
                    continue
                pid = int(parts[1])
                ded, shr = per_pid.get(pid, (0, 0))
                per_pid[pid] = (ded + value, shr) if key == "ded" else (ded, shr + value)
        return per_pid, discrete


class Nvml:
    def __init__(self) -> None:
        self.handle = None
        self.error: str | None = None
        if pynvml is None:
            self.error = "nvidia-ml-py not installed"
            return
        try:
            pynvml.nvmlInit()
            self.handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self.name = pynvml.nvmlDeviceGetName(self.handle)
            self.driver = pynvml.nvmlSystemGetDriverVersion()
        except Exception as exc:  # pynvml.NVMLError
            self.error = f"{type(exc).__name__}: {exc}"

    def memory(self) -> dict[str, int] | None:
        if self.handle is None:
            return None
        try:
            info = pynvml.nvmlDeviceGetMemoryInfo(self.handle)
            return {"total": info.total, "used": info.used, "free": info.free}
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return None

    def close(self) -> None:
        if self.handle is not None:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass
            self.handle = None


def system_memory() -> dict[str, int]:
    vm = psutil.virtual_memory()
    return {"total": vm.total, "available": vm.available, "in_use": vm.total - vm.available}


class ResourceSampler:
    """Snapshots of XODUS-related processes. Browser and other apps are external to the budget.

    Groups: xodus_app (backend), ollama (server and runners, found by port), and every process the
    supervisor launched (its whole job), reported under its own tier.
    """

    def __init__(self, ollama_port: int, supervisor: Any = None) -> None:
        self.ollama_port = ollama_port
        self.supervisor = supervisor
        self.nvml = Nvml()
        self.gpu = GpuCounters()

    def processes(self) -> list[tuple[psutil.Process, str, str]]:
        supervised: dict[int, str] = {}
        if self.supervisor is not None:
            for mp in self.supervisor.managed():
                for pid in mp.job.pids() or [mp.pid]:
                    supervised[pid] = mp.tier
        # Each process is counted once. Supervised processes are classified by their own tier first, then the backend's
        # tree, then Ollama's (found by its port). The trees can overlap, e.g. if Ollama ever ran inside or under the
        # backend process (as the test fake does); a process seen once is not added again.
        found: list[tuple[psutil.Process, str, str]] = []
        seen: set[int] = set()

        def add(proc: psutil.Process, group: str, tier: str) -> None:
            if proc.pid not in seen:
                seen.add(proc.pid)
                found.append((proc, group, tier))

        for pid, tier in supervised.items():
            try:
                add(psutil.Process(pid), "optional" if tier == "optional" else "xodus_app", tier)
            except psutil.Error:
                pass
        for p in process_tree(backend_root()):
            add(p, "xodus_app", "core")
        pid = listener_pid(self.ollama_port)
        if pid:
            try:
                for p in process_tree(psutil.Process(pid)):
                    add(p, "ollama", "core")
            except psutil.Error:
                pass
        return found

    def snapshot(self) -> dict[str, Any]:
        started = time.perf_counter()
        gpu_by_pid, discrete_luid = self.gpu.read()
        samples = []
        for proc, group, tier in self.processes():
            sample = read_process(proc, group, tier)
            if vanished(sample):
                continue  # exited between listing and reading: it holds no memory, and it is not an unreadable value
            if proc.pid in gpu_by_pid:
                sample.gpu_dedicated, sample.gpu_shared = gpu_by_pid[proc.pid]
            samples.append(sample)

        def total(attr: str, group: str | None = None) -> int:
            return sum(getattr(s, attr) or 0 for s in samples if group in (None, s.group))

        return {
            "timestamp": time.time(),
            "processes": [asdict(s) for s in samples],
            "totals": {
                "uss": total("uss"),
                "rss": total("rss"),
                "private_commit": total("private_commit"),
                "uss_xodus_app": total("uss", "xodus_app"),
                "uss_ollama": total("uss", "ollama"),
                "uss_optional": total("uss", "optional"),
                "gpu_dedicated_ollama": total("gpu_dedicated", "ollama"),
                "gpu_shared_ollama": total("gpu_shared", "ollama"),
                # Every XODUS-related process, from the per-process Windows counters: XODUS's own VRAM, and the system RAM
                # used on the GPU's behalf (shared GPU memory: spill or pinned staging memory).
                "gpu_dedicated_xodus": total("gpu_dedicated"),
                "gpu_shared_xodus": total("gpu_shared"),
            },
            "uss_complete": all(s.uss is not None for s in samples),
            "device_vram": self.nvml.memory(),
            "system_memory": system_memory(),
            "excluded": "browser and other user applications (external to budget)",
            "errors": {"nvml": self.nvml.error, "gpu_counters": self.gpu.error, "discrete_luid": discrete_luid},
            "sample_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    def close(self) -> None:
        self.nvml.close()


def mib(value: int | None) -> float | None:
    return None if value is None else round(value / 2**20, 1)


__all__ = [
    "GpuCounters",
    "Nvml",
    "ProcessSample",
    "ResourceSampler",
    "backend_root",
    "listener_pid",
    "mib",
    "process_tree",
    "read_process",
    "system_memory",
    "vanished",
]
