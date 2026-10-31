"""Resource measurement harness (master prompt 3.3-3.6; protocol in experiments/gate-f/PROTOCOL.md).

Used by scripts/smoke_benchmark.py and scripts/measure.py. It runs OUTSIDE the backend, as its own process, so it can
take an idle baseline with XODUS fully stopped and then watch XODUS start.

Sampler: one thread reads device VRAM (NVML) every 200 ms; another reads, every 500 ms, the memory of every
XODUS-related process (the backend's process tree and Ollama's, found by the process listening on its port) and the
system's physical memory in use; every second tick it also reads the Windows per-process GPU counters and Ollama's
/api/ps. Each process is put on one of the protocol's lines: backend and ingestion worker ("xodus_app"), Ollama server
and runners ("ollama"), plugins and MCP servers ("optional"). The harness's own memory is reported apart.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import psutil

from xodus.config.settings import REPO_ROOT, default_config_files
from xodus.resources.sampler import (GpuCounters, Nvml, listener_pid, process_tree, read_process, system_memory,
                                     vanished)
from xodus.security.token import HEADER, read_token

MIB = 2**20
GB = 10**9
OLLAMA_PORT = 11434
START_OLLAMA = REPO_ROOT / "scripts" / "start-ollama.ps1"
STOP_OLLAMA = REPO_ROOT / "scripts" / "stop-ollama.ps1"
VENV_PY = REPO_ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
QUIET_SYSTEM_MIB = 256
QUIET_VRAM_MIB = 128
_BLOB = re.compile(r"sha256-([0-9a-f]{64})")


def mib(value: float | None) -> float | None:
    return None if value is None else round(value / MIB, 1)


def other_activity() -> list[dict[str, Any]]:
    """Other XODUS-related work on this machine: test runs and backends this script did not start. On 31 Oct 2026 a pytest
    run started by another agent used the same Ollama during failure injection and changed its results."""
    me = {psutil.Process().pid, *(p.pid for p in psutil.Process().children(recursive=True))}
    for parent in psutil.Process().parents():
        me.add(parent.pid)
    found = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            line = " ".join(proc.info["cmdline"] or [])
        except (psutil.Error, TypeError):
            continue
        if proc.info["pid"] in me or "python" not in (proc.info["name"] or "").lower():
            continue
        if any(mark in line for mark in ("-m pytest", "pytest.exe", "-m xodus")):
            found.append({"pid": proc.info["pid"], "command": line[:160]})
    return found


def require_quiet_machine(force: bool = False) -> None:
    """Refuse to measure next to other XODUS work (it shares Ollama, the GPU and port 8765). --force records it instead."""
    found = other_activity()
    if found and not force:
        lines = "\n".join(f"  pid {f['pid']}: {f['command']}" for f in found)
        raise SystemExit(f"Other XODUS work is running; a measurement next to it would be invalid:\n{lines}\n"
                         "Wait for it to finish, or stop it, and start again.")


def keep_awake() -> None:
    """Ask Windows not to sleep through a long measurement while this process runs (no setting is changed; it ends with
    the process, and closing the lid can still suspend the machine)."""
    if sys.platform == "win32":
        import ctypes

        es_continuous, es_system_required = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)


# ---------------------------------------------------------------------------------------------- Ollama model blobs


def model_blobs() -> dict[str, str]:
    """sha256 of every installed model's weights blob -> model name, read from Ollama's manifests. Lets the harness say
    which runner process serves which model (a runner's command line names the blob it loaded)."""
    root = Path(os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama" / "models") / "manifests"
    out: dict[str, str] = {}
    for manifest in root.glob("*/*/*/*"):
        try:
            layers = json.loads(manifest.read_text(encoding="utf-8")).get("layers", [])
        except (OSError, ValueError):
            continue
        name = f"{manifest.parent.name}:{manifest.name}"
        for layer in layers:
            if layer.get("mediaType") == "application/vnd.ollama.image.model":
                out[layer["digest"].split(":", 1)[1]] = name
    return out


# ---------------------------------------------------------------------------------------------- sampler


class Sampler:
    def __init__(self, ollama_port: int = OLLAMA_PORT) -> None:
        self.ollama_port = ollama_port
        self.backend: psutil.Process | None = None
        self.fast: list[dict[str, Any]] = []
        self.slow: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread_ids: list[int] = []
        self._threads = [threading.Thread(target=self._fast_loop, daemon=True, name="sampler-vram"),
                         threading.Thread(target=self._slow_loop, daemon=True, name="sampler-procs")]
        self._ollama_root: psutil.Process | None = None
        self._cmdlines: dict[tuple[int, float], str] = {}
        self._blobs = model_blobs()

    def start(self) -> Sampler:
        for t in self._threads:
            t.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=10)

    def cpu_seconds(self) -> float:
        return sum(t.user_time + t.system_time for t in psutil.Process().threads() if t.id in self._thread_ids)

    def _fast_loop(self) -> None:
        self._thread_ids.append(threading.get_native_id())
        nvml = Nvml()
        while not self._stop.is_set():
            t0 = time.perf_counter()
            mem = nvml.memory()
            self.fast.append({"t": time.time(), "used": mem["used"] if mem else None, "free": mem["free"] if mem else None})
            self._stop.wait(max(0.0, 0.2 - (time.perf_counter() - t0)))
        nvml.close()

    def _ollama(self) -> psutil.Process | None:
        if self._ollama_root is not None and self._ollama_root.is_running():
            return self._ollama_root
        pid = listener_pid(self.ollama_port)
        try:
            self._ollama_root = psutil.Process(pid) if pid else None
        except psutil.Error:
            self._ollama_root = None
        return self._ollama_root

    def _cmdline(self, proc: psutil.Process) -> str:
        try:
            key = (proc.pid, proc.create_time())
            if key not in self._cmdlines:
                self._cmdlines[key] = " ".join(proc.cmdline())
            return self._cmdlines[key]
        except psutil.Error:
            return ""

    def _backend_roles(self, tree: list[psutil.Process]) -> dict[int, tuple[str, str]]:
        """pid -> (group, role). The backend root is the venv launcher, its child the real interpreter; every other
        process descends from one of the backend's children (a launcher plus interpreter per worker or plugin)."""
        if not tree:
            return {}
        top = {tree[0].pid}
        for p in tree[1:]:
            try:
                if "-m xodus" in self._cmdline(p) and "xodus." not in self._cmdline(p) and p.ppid() in top:
                    top.add(p.pid)
            except psutil.Error:
                pass
        roles: dict[int, tuple[str, str]] = {pid: ("xodus_app", "backend") for pid in top}
        parent = {}
        for p in tree:
            try:
                parent[p.pid] = p.ppid()
            except psutil.Error:
                pass
        for p in tree:
            if p.pid in roles:
                continue
            node = p.pid
            while parent.get(node) not in top and parent.get(node) in parent:
                node = parent[node]
            head = next((q for q in tree if q.pid == node), p)
            line = self._cmdline(head)
            if "xodus.ingestion.worker" in line:
                roles[p.pid] = ("xodus_app", "ingestion")
            else:
                roles[p.pid] = ("optional", "plugin:" + Path(self._cwd(head) or "?").name)
        return roles

    @staticmethod
    def _cwd(proc: psutil.Process) -> str | None:
        try:
            return proc.cwd()
        except psutil.Error:
            return None

    def _ollama_role(self, proc: psutil.Process, root: psutil.Process) -> str:
        if proc.pid == root.pid:
            return "ollama_server"
        try:
            if proc.name().lower() == "conhost.exe":
                return "console_host"  # Windows console host of a hidden-window Ollama process
        except psutil.Error:
            pass
        match = _BLOB.search(self._cmdline(proc))
        if not match:
            return "runner:no-model"  # short-lived runner Ollama starts without a model (device and memory probes)
        return "runner:" + self._blobs.get(match.group(1), "unknown-model")

    def _read(self, proc: psutil.Process, group: str, role: str) -> dict[str, Any] | None:
        sample = read_process(proc, group, "optional" if group == "optional" else "core")
        if vanished(sample):
            return None  # exited between listing and reading: it holds no memory, and it is not an unreadable value
        out = vars(sample)
        out["role"] = role
        return out

    def _slow_loop(self) -> None:
        self._thread_ids.append(threading.get_native_id())
        gpu = GpuCounters()
        client = httpx.Client(trust_env=False, timeout=1.0)
        tick = 0
        while not self._stop.is_set():
            t0 = time.perf_counter()
            procs: list[dict[str, Any]] = []
            seen: set[int] = set()
            if self.backend is not None and self.backend.is_running():
                tree = process_tree(self.backend)
                roles = self._backend_roles(tree)
                for p in tree:
                    group, role = roles.get(p.pid, ("optional", "unknown"))
                    seen.add(p.pid)
                    if (sample := self._read(p, group, role)) is not None:
                        procs.append(sample)
            root = self._ollama()
            if root is not None:
                for p in process_tree(root):
                    if p.pid not in seen and (sample := self._read(p, "ollama", self._ollama_role(p, root))) is not None:
                        procs.append(sample)
            rec: dict[str, Any] = {"t": time.time(), "procs": procs, "system": system_memory(),
                                   "harness": vars(read_process(psutil.Process(), "harness", "external"))}
            if tick % 2 == 0:
                per_pid, luid = gpu.read()
                for p in procs:
                    p["gpu_dedicated"], p["gpu_shared"] = per_pid.get(p["pid"], (0, 0)) if gpu.error is None else (None, None)
                rec["gpu_counters"] = {"ok": gpu.error is None, "discrete_luid": luid, "error": gpu.error,
                                       "all_pids_dedicated": sum(d for d, _ in per_pid.values())}
                if root is not None:
                    try:
                        rec["api_ps"] = client.get(f"http://127.0.0.1:{self.ollama_port}/api/ps").json().get("models", [])
                    except (httpx.HTTPError, ValueError):
                        rec["api_ps"] = None
            rec["read_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            self.slow.append(rec)
            tick += 1
            self._stop.wait(max(0.0, 0.5 - (time.perf_counter() - t0)))
        client.close()


# ---------------------------------------------------------------------------------------------- baseline


def baseline(seconds: float, ollama_port: int = OLLAMA_PORT, attempts: int = 3,
             log: Callable[[str], None] = print) -> dict[str, Any]:
    """Idle baseline with XODUS stopped (protocol section 4): repeated while noisy, at most `attempts` times."""
    tries = []
    for attempt in range(1, attempts + 1):
        s = Sampler(ollama_port).start()
        time.sleep(seconds)
        s.stop()
        sys_vals = [r["system"]["in_use"] for r in s.slow]
        vram_vals = [r["used"] for r in s.fast if r["used"] is not None]
        result = {
            "attempt": attempt, "seconds": seconds,
            "system_in_use_median": statistics.median(sys_vals),
            "system_spread_mib": mib(max(sys_vals) - min(sys_vals)),
            "available_median": statistics.median(r["system"]["available"] for r in s.slow),
            "vram_used_median": statistics.median(vram_vals) if vram_vals else None,
            "vram_spread_mib": mib(max(vram_vals) - min(vram_vals)) if vram_vals else None,
            "gpu_dedicated_all_processes_median": statistics.median(
                r["gpu_counters"]["all_pids_dedicated"] for r in s.slow if "gpu_counters" in r) if s.slow else None,
            "samples": {"slow": len(s.slow), "fast": len(s.fast)},
        }
        result["quiet"] = (result["system_spread_mib"] <= QUIET_SYSTEM_MIB
                           and (result["vram_spread_mib"] is None or result["vram_spread_mib"] <= QUIET_VRAM_MIB))
        tries.append(result)
        log(f"  baseline attempt {attempt}: system spread {result['system_spread_mib']} MiB, VRAM spread "
            f"{result['vram_spread_mib']} MiB -> {'quiet' if result['quiet'] else 'NOISY'}")
        if result["quiet"]:
            break
    best = min(tries, key=lambda r: r["system_spread_mib"])
    return {**best, "attempts": tries}


# ---------------------------------------------------------------------------------------------- services


def port_busy(port: int) -> bool:
    return listener_pid(port) is not None


def run_ps(script: Path, log_dir: Path, *args: str, env: dict[str, str] | None = None) -> str:
    # Output goes to a file, not a pipe: start-ollama.ps1 leaves ollama.exe running, and a child that inherits a pipe
    # keeps it open, so reading a pipe to EOF would block forever (this hung the first Gate B benchmark attempt).
    log = log_dir / f"{script.stem}.log"
    with open(log, "a", encoding="utf-8") as fh:
        code = subprocess.run(["powershell", "-NoProfile", "-File", str(script), *args], stdin=subprocess.DEVNULL,
                              stdout=fh, stderr=subprocess.STDOUT, timeout=120,
                              env=None if env is None else {**os.environ, **env}).returncode
    text = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    if code != 0:
        raise RuntimeError(f"{script.name} failed ({code}): {text[-3:]}")
    return text[-1] if text else ""


class Backend:
    """A `python -m xodus` subprocess with its own environment (data and log folders, extra roots or profiles)."""

    def __init__(self, env: dict[str, str], data_dir: Path, log_path: Path, port: int = 8765) -> None:
        self.env, self.data_dir, self.log_path, self.port = env, data_dir, log_path, port
        self.proc: subprocess.Popen[bytes] | None = None
        self.token: str | None = None
        self.base = f"http://127.0.0.1:{port}"

    def start(self, timeout_s: float = 90) -> dict[str, Any]:
        self._log = open(self.log_path, "ab")
        self.proc = subprocess.Popen([str(VENV_PY), "-m", "xodus"], cwd=REPO_ROOT / "backend", env=self.env,
                                     stdout=self._log, stderr=subprocess.STDOUT)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"backend exited with code {self.proc.returncode}; see {self.log_path}")
            try:
                self.token = self.token or read_token(self.data_dir)
                body = httpx.get(f"{self.base}/api/health", headers={HEADER: self.token}, trust_env=False, timeout=2).json()
                if body["ollama"]["status"] == "ok":
                    return body
            except (OSError, httpx.HTTPError, ValueError, KeyError):
                pass
            time.sleep(0.3)
        raise RuntimeError("backend did not become healthy")

    @property
    def psutil(self) -> psutil.Process:
        assert self.proc is not None
        return psutil.Process(self.proc.pid)

    def client(self, timeout: float = 300) -> httpx.Client:
        return httpx.Client(base_url=self.base, headers={HEADER: self.token or ""}, trust_env=False, timeout=timeout)

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        for p in reversed(process_tree(self.psutil)):
            try:
                p.terminate()
            except psutil.Error:
                pass
        try:
            self.proc.wait(timeout=20)
        finally:
            self._log.close()


# ---------------------------------------------------------------------------------------------- HTTP helpers


def sse(client: httpx.Client, path: str, body: dict[str, Any], stop_after_tokens: int | None = None) -> dict[str, Any]:
    """POST an SSE endpoint; returns events, timing and the final 'done' or 'error' data. Optionally disconnects after
    `stop_after_tokens` tokens (cancellation mid-stream)."""
    t0 = time.perf_counter()
    first = None
    tokens = 0
    done = error = None
    event = None
    status = None
    disconnected = False
    try:
        with client.stream("POST", path, json=body) as resp:
            status = resp.status_code
            if status != 200:
                resp.read()
                try:
                    error = resp.json()
                except ValueError:
                    error = {"text": resp.text[:300]}
            else:
                for line in resp.iter_lines():
                    if line.startswith("event: "):
                        event = line[7:]
                    elif line.startswith("data: "):
                        data = json.loads(line[6:])
                        if event == "token":
                            tokens += 1
                            first = first or time.perf_counter()
                            if stop_after_tokens is not None and tokens >= stop_after_tokens:
                                disconnected = True
                                break
                        elif event == "done":
                            done = data
                        elif event == "error":
                            error = data
    except httpx.HTTPError as exc:
        error = {"code": "client_error", "message": repr(exc)}
    t_end = time.perf_counter()
    return {"status": status, "ok": done is not None and error is None, "error": error, "tokens": tokens,
            "disconnected": disconnected, "client_ttft_ms": round((first - t0) * 1000, 1) if first else None,
            "client_total_ms": round((t_end - t0) * 1000, 1), "done": done, "t_end": time.time()}


# ---------------------------------------------------------------------------------------------- analysis


def _sum(sample: dict[str, Any], key: str, pred: Callable[[dict[str, Any]], bool] = lambda p: True) -> int:
    return sum(p.get(key) or 0 for p in sample["procs"] if pred(p))


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"median_mib": None, "peak_mib": None}
    return {"median_mib": mib(statistics.median(values)), "peak_mib": mib(max(values))}


def window(sampler: Sampler, t0: float, t1: float, base: dict[str, Any], embed_model: str | None = None) -> dict[str, Any]:
    """Every protocol section-5 figure for one scenario window [t0, t1]."""
    slow = [s for s in sampler.slow if t0 <= s["t"] <= t1]
    fast = [s for s in sampler.fast if t0 <= s["t"] <= t1 and s["used"] is not None]
    complete = [s for s in slow if all(p["uss"] is not None for p in s["procs"])]  # empty = nothing running yet: complete
    complete_ids = {id(s) for s in complete}
    gpu = [s for s in slow if "gpu_counters" in s and s["gpu_counters"]["ok"]]
    lines = {}
    for line in ("xodus_app", "ollama", "optional"):
        def member(p: dict[str, Any], line: str = line) -> bool:
            return p["group"] == line
        lines[line] = {"uss": _stats([_sum(s, "uss", member) for s in complete]),
                       "working_set": _stats([_sum(s, "rss", member) for s in slow]),
                       "private_commit": _stats([_sum(s, "private_commit", member) for s in slow]),
                       "processes_max": max((sum(member(p) for p in s["procs"]) for s in slow), default=0)}
    roles: dict[str, float] = {}
    for s in complete:
        per_role: dict[str, int] = {}
        for p in s["procs"]:
            per_role[p["role"]] = per_role.get(p["role"], 0) + (p["uss"] or 0)
        for role, value in per_role.items():
            roles[role] = max(roles.get(role, 0), value)
    uss_tot = [_sum(s, "uss") for s in complete]
    sys_delta = [s["system"]["in_use"] - base["system_in_use_median"] for s in slow]
    shared = [_sum(s, "gpu_shared") for s in gpu]
    dedicated = [_sum(s, "gpu_dedicated") for s in gpu]
    vram_delta = [s["used"] - base["vram_used_median"] for s in fast] if base.get("vram_used_median") is not None else []
    ps = [s.get("api_ps") for s in slow if s.get("api_ps") is not None]
    generators = [sum(1 for m in models if m.get("name") != embed_model) for models in ps]
    peak_i = max(range(len(complete)), key=lambda i: uss_tot[i]) if complete else None
    return {
        "start": t0, "end": t1, "duration_s": round(t1 - t0, 1),
        "samples": {"slow": len(slow), "fast": len(fast), "complete": len(complete), "with_gpu_counters": len(gpu),
                    "complete_ratio": round(len(complete) / len(slow), 3) if slow else None},
        "uss_total": _stats(uss_tot),
        "uss_plus_shared_gpu_peak_mib": mib(max((_sum(s, "uss") + _sum(s, "gpu_shared") for s in gpu
                                                 if id(s) in complete_ids), default=0)) if gpu else None,
        "lines": lines,
        "role_uss_peak_mib": {r: mib(v) for r, v in sorted(roles.items())},
        "system_delta": _stats(sys_delta),
        "system_available_min_mib": mib(min((s["system"]["available"] for s in slow), default=0)) if slow else None,
        "gpu_shared_xodus": _stats(shared),
        "gpu_dedicated_xodus": _stats(dedicated),
        "device_vram_delta": _stats(vram_delta),
        "device_vram_peak_mib": mib(max((s["used"] for s in fast), default=0)) if fast else None,
        "device_vram_free_min_mib": mib(min((s["free"] for s in fast), default=0)) if fast else None,
        "api_ps_max_generation_models": max(generators, default=0),
        "api_ps_at_end": ps[-1] if ps else None,
        "processes_at_uss_peak": None if peak_i is None else [
            {k: p.get(k) for k in ("pid", "name", "group", "role", "tier", "errors")} | {
                "uss_mib": mib(p["uss"]), "working_set_mib": mib(p["rss"]), "private_commit_mib": mib(p["private_commit"])}
            for p in complete[peak_i]["procs"]],
        "harness_uss_peak_mib": mib(max((s["harness"]["uss"] or 0 for s in slow), default=0)),
        "sampler_read_ms": {"median": statistics.median(s["read_ms"] for s in slow) if slow else None,
                            "max": max((s["read_ms"] for s in slow), default=None)},
    }


def agreement(a: float | None, b: float | None) -> float | None:
    """|a - b| / max(a, b), the protocol's comparison."""
    if not a or not b:
        return None
    return round(abs(a - b) / max(a, b), 3)


def vram_agrees(device_delta_mib: float | None, per_process_mib: float | None) -> bool | None:
    if device_delta_mib is None or per_process_mib is None:
        return None
    return abs(device_delta_mib - per_process_mib) <= max(300.0, 0.15 * device_delta_mib)


# ---------------------------------------------------------------------------------------------- environment


def environment(settings: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    def cmd(args: list[str]) -> str:
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"unavailable: {exc}"

    cfg_hash = hashlib.sha256(b"".join(p.read_bytes() for p in default_config_files())).hexdigest()[:16]
    battery = psutil.sensors_battery()
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "windows": platform.platform(), "windows_version": platform.version(),
        "cpu": platform.processor(), "cpu_logical": psutil.cpu_count(),
        "python": sys.version.split()[0], "psutil": psutil.__version__,
        "ram_total_mib": round(psutil.virtual_memory().total / MIB),
        "gpu": cmd(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,driver_model.current", "--format=csv,noheader"]),
        "power_plan": cmd(["powercfg", "/getactivescheme"]),
        "on_ac_power": battery.power_plugged if battery else None,
        "model": settings.ollama.model, "embed_model": settings.ollama.embed_model,
        "config_hash": cfg_hash,
        "ollama_settings": "scripts/start-ollama.ps1 (NUM_PARALLEL=1, MAX_LOADED_MODELS=2, MAX_QUEUE=8, FLASH_ATTENTION=1, "
                           "KV q8_0, KEEP_ALIVE=10m, NO_CLOUD=1; defaults LLAMA_ARG_CACHE_RAM=0, LLAMA_ARG_CTX_CHECKPOINTS=2)",
        "start_ollama_sha256": sha256_file(START_OLLAMA)[:16],
        **(extra or {}),
    }


def ollama_facts() -> dict[str, Any]:
    """Ollama version and every installed model's digest (Ollama must be running)."""
    with httpx.Client(base_url=f"http://127.0.0.1:{OLLAMA_PORT}", trust_env=False, timeout=5) as c:
        return {"version": c.get("/api/version").json().get("version"),
                "models": {m["name"]: {"digest": m.get("digest"), "size": m.get("size")}
                           for m in c.get("/api/tags").json().get("models", [])}}


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify_manifest(folder: Path) -> list[str]:
    """Files whose SHA-256 differs from MANIFEST.json (the fixed corpus must not drift between runs)."""
    manifest = json.loads((folder / "MANIFEST.json").read_text(encoding="utf-8"))
    return [name for name, digest in manifest["files"].items() if sha256_file(folder / name) != digest]

