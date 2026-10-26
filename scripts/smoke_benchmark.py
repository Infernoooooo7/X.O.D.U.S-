"""Smoke benchmark (master prompt v2, section 6) - validates the measurement pipeline.

Default profile only, 10 fixed prompts, num_ctx 4096 then 8192, two identical cold-start runs.
Each run: idle baseline with XODUS stopped -> start Ollama + backend -> 20 requests through the
backend's /api/chat -> stop everything.

Run from backend/ with Ollama and the backend STOPPED:
    uv run python ..\\scripts\\smoke_benchmark.py

Pass criteria (fixed here before the first run):
  1. RAM: peak summed USS of XODUS processes vs peak system-wide increase over the idle
     baseline agree within 20% (section 3.3).
  2. VRAM: peak NVML device increase over baseline vs peak per-process dedicated GPU memory
     (Windows counters) agree within max(15%, 300 MiB). Section 3.4 gives no number; this
     tolerance is the harness author's choice and must be confirmed by the user.
  3. Two runs agree within 5% on peak USS and peak device VRAM.
  4. Sampler threads use < 2% of total CPU capacity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import psutil

from xodus.config.settings import REPO_ROOT, default_config_files, load_settings
from xodus.resources.sampler import GpuCounters, Nvml, listener_pid, process_tree, read_process, system_memory

MIB = 2**20
RAM_TOL = 0.20
VRAM_TOL_FRAC, VRAM_TOL_ABS = 0.15, 300 * MIB
RUN_TOL = 0.05
OVERHEAD_MAX_PCT = 2.0
SEED = 42
START_OLLAMA = REPO_ROOT / "scripts" / "start-ollama.ps1"
STOP_OLLAMA = REPO_ROOT / "scripts" / "stop-ollama.ps1"
VENV_PY = REPO_ROOT / "backend" / ".venv" / "Scripts" / "python.exe"


class Sampler:
    """Fast thread: NVML device VRAM every 200 ms. Slow thread: per-process memory and system memory
    every 500 ms; GPU process counters and Ollama /api/ps every second."""

    def __init__(self, ollama_port: int, backend: psutil.Process | None = None) -> None:
        self.ollama_port = ollama_port
        self.backend = backend
        self.fast: list[dict[str, Any]] = []
        self.slow: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread_ids: list[int] = []
        self._threads = [threading.Thread(target=self._fast_loop, daemon=True),
                         threading.Thread(target=self._slow_loop, daemon=True)]
        self._ollama_root: psutil.Process | None = None

    def start(self) -> None:
        self.t_start = time.time()
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=10)
        self.t_stop = time.time()

    def cpu_seconds(self) -> float:
        return sum(t.user_time + t.system_time for t in psutil.Process().threads() if t.id in self._thread_ids)

    def _fast_loop(self) -> None:
        self._thread_ids.append(threading.get_native_id())
        nvml = Nvml()
        while not self._stop.is_set():
            t0 = time.perf_counter()
            mem = nvml.memory()
            self.fast.append({"t": time.time(), "used": mem["used"] if mem else None})
            self._stop.wait(max(0.0, 0.2 - (time.perf_counter() - t0)))
        nvml.close()

    def _ollama(self) -> psutil.Process | None:
        if self._ollama_root is not None and self._ollama_root.is_running():
            return self._ollama_root
        pid = listener_pid(self.ollama_port)
        self._ollama_root = psutil.Process(pid) if pid else None
        return self._ollama_root

    def _slow_loop(self) -> None:
        self._thread_ids.append(threading.get_native_id())
        gpu = GpuCounters()
        client = httpx.Client(trust_env=False, timeout=1.0)
        tick = 0
        while not self._stop.is_set():
            t0 = time.perf_counter()
            procs: list[dict[str, Any]] = []
            if self.backend is not None and self.backend.is_running():
                procs += [vars(read_process(p, "xodus_app", "core")) for p in process_tree(self.backend)]
            root = self._ollama()
            if root is not None:
                procs += [vars(read_process(p, "ollama", "core")) for p in process_tree(root)]
            harness = vars(read_process(psutil.Process(), "harness", "optional"))
            rec: dict[str, Any] = {"t": time.time(), "procs": procs, "harness": harness,
                                   "system": system_memory()}
            if tick % 2 == 0:
                per_pid, luid = gpu.read()
                for p in procs:
                    p["gpu_dedicated"], p["gpu_shared"] = per_pid.get(p["pid"], (None, None))
                rec["gpu_counters"] = {"ok": gpu.error is None, "discrete_luid": luid, "error": gpu.error}
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


def port_busy(port: int) -> bool:
    return listener_pid(port) is not None


def run_ps(script: Path) -> str:
    out = subprocess.run(["powershell", "-NoProfile", "-File", str(script)], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"{script.name} failed: {out.stdout} {out.stderr}")
    return out.stdout.strip()


def wait_health(port: int, timeout: float = 60) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            body = httpx.get(f"http://127.0.0.1:{port}/api/health", trust_env=False, timeout=2).json()
            if body["ollama"]["status"] == "ok":
                return body
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        time.sleep(0.3)
    raise RuntimeError("backend did not become healthy")


def chat(client: httpx.Client, port: int, prompt: str, num_ctx: int) -> dict[str, Any]:
    t0 = time.perf_counter()
    first = None
    done = error = None
    event = None
    try:
        with client.stream("POST", f"http://127.0.0.1:{port}/api/chat",
                           json={"message": prompt, "num_ctx": num_ctx, "seed": SEED}, timeout=300) as resp:
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                    if event == "token" and first is None:
                        first = time.perf_counter()
                    elif event == "done":
                        done = data
                    elif event == "error":
                        error = data
    except httpx.HTTPError as exc:
        error = {"code": "client_error", "message": repr(exc)}
    t_end = time.perf_counter()
    return {
        "num_ctx": num_ctx,
        "client_ttft_ms": round((first - t0) * 1000, 1) if first else None,
        "client_total_ms": round((t_end - t0) * 1000, 1),
        "ok": done is not None and error is None,
        "error": error,
        "server_timing": done["timing"] if done else None,
        "usage": done["usage"] if done else None,
    }


def baseline(ollama_port: int, seconds: float) -> dict[str, Any]:
    s = Sampler(ollama_port)
    s.start()
    time.sleep(seconds)
    s.stop()
    sys_vals = [r["system"]["in_use"] for r in s.slow]
    vram_vals = [r["used"] for r in s.fast if r["used"] is not None]
    gpu_users = s.slow[0].get("gpu_counters") if s.slow else None
    return {
        "seconds": seconds,
        "system_in_use_median": statistics.median(sys_vals),
        "system_in_use_p95": sorted(sys_vals)[int(0.95 * (len(sys_vals) - 1))],
        "system_in_use_spread_mib": round((max(sys_vals) - min(sys_vals)) / MIB, 1),
        "vram_used_median": statistics.median(vram_vals) if vram_vals else None,
        "vram_used_spread_mib": round((max(vram_vals) - min(vram_vals)) / MIB, 1) if vram_vals else None,
        "samples": {"slow": len(s.slow), "fast": len(s.fast)},
        "gpu_counters": gpu_users,
    }


def one_run(index: int, settings: Any, prompts: list[str], ctxs: list[int], baseline_s: float, log_dir: Path) -> dict[str, Any]:
    ollama_port = 11434
    port = settings.server.port
    if port_busy(ollama_port) or port_busy(port):
        raise SystemExit(f"Ports {ollama_port}/{port} must be free (stop Ollama and the backend first).")
    print(f"[run {index}] idle baseline {baseline_s:.0f} s ...", flush=True)
    base = baseline(ollama_port, baseline_s)

    sampler = Sampler(ollama_port)
    sampler.start()
    cpu0, wall0 = sampler.cpu_seconds(), time.time()
    t_services = time.time()
    print(f"[run {index}] starting Ollama: {run_ps(START_OLLAMA)}", flush=True)
    log = (log_dir / f"backend-run{index}.log").open("w", encoding="utf-8")
    backend = subprocess.Popen([str(VENV_PY), "-m", "xodus"], cwd=REPO_ROOT / "backend", stdout=log, stderr=subprocess.STDOUT)
    sampler.backend = psutil.Process(backend.pid)
    health = wait_health(port)
    t_ready = time.time()

    requests: list[dict[str, Any]] = []
    with httpx.Client(trust_env=False) as client:
        for ctx in ctxs:
            for i, prompt in enumerate(prompts):
                r = chat(client, port, prompt, ctx)
                r.update(prompt_index=i, run=index, t=time.time(),
                         cold=bool(r["server_timing"] and r["server_timing"]["ollama_load_ms"] > 500))
                requests.append(r)
                print(f"[run {index}] ctx={ctx} prompt={i} ok={r['ok']} ttft={r['client_ttft_ms']} total={r['client_total_ms']}", flush=True)
    time.sleep(3)  # tail of the steady window
    t_requests_done = time.time()
    cpu1, wall1 = sampler.cpu_seconds(), time.time()

    ps = psutil.Process(backend.pid)
    for p in reversed(process_tree(ps)):
        p.terminate()
    backend.wait(timeout=15)
    log.close()
    run_ps(STOP_OLLAMA)
    time.sleep(3)
    sampler.stop()

    return {
        "run": index,
        "baseline": base,
        "health_at_start": health,
        "times": {"services_started": t_services, "ready": t_ready, "requests_done": t_requests_done},
        "requests": requests,
        "sampler_overhead": {
            "cpu_seconds": round(cpu1 - cpu0, 3),
            "wall_seconds": round(wall1 - wall0, 1),
            "pct_of_total_cpu": round(100 * (cpu1 - cpu0) / ((wall1 - wall0) * psutil.cpu_count()), 3),
            "pct_of_one_core": round(100 * (cpu1 - cpu0) / (wall1 - wall0), 2),
            "slow_read_ms_median": statistics.median(r["read_ms"] for r in sampler.slow),
            "slow_read_ms_max": max(r["read_ms"] for r in sampler.slow),
        },
        "fast_samples": sampler.fast,
        "slow_samples": sampler.slow,
    }


def analyse_run(run: dict[str, Any]) -> dict[str, Any]:
    base = run["baseline"]
    t0, t1 = run["times"]["services_started"], run["times"]["requests_done"]
    slow = [s for s in run["slow_samples"] if t0 <= s["t"] <= t1]
    fast = [s for s in run["fast_samples"] if t0 <= s["t"] <= t1 and s["used"] is not None]

    def summed(s: dict[str, Any], key: str, group: str | None = None) -> int:
        return sum(p[key] or 0 for p in s["procs"] if group in (None, p["group"]))

    uss = [summed(s, "uss") for s in slow]
    i_peak = max(range(len(uss)), key=uss.__getitem__)
    sys_delta = [s["system"]["in_use"] - base["system_in_use_median"] for s in slow]
    gpu_slow = [s for s in slow if "gpu_counters" in s]
    ded = [summed(s, "gpu_dedicated", "ollama") for s in gpu_slow]
    shr = [summed(s, "gpu_shared", "ollama") for s in gpu_slow]
    size_vram = [max((m.get("size_vram", 0) for m in s.get("api_ps") or []), default=0) for s in gpu_slow]
    size_total = [max((m.get("size", 0) for m in s.get("api_ps") or []), default=0) for s in gpu_slow]
    vram_peak = max(s["used"] for s in fast)
    vram_delta = vram_peak - base["vram_used_median"]

    peak_procs: dict[str, Any] = {}
    for p in slow[i_peak]["procs"]:
        peak_procs[f'{p["name"]}#{p["pid"]}'] = {"group": p["group"], "uss_mib": round((p["uss"] or 0) / MIB, 1),
                                                  "rss_mib": round((p["rss"] or 0) / MIB, 1),
                                                  "private_commit_mib": round((p["private_commit"] or 0) / MIB, 1),
                                                  "errors": p["errors"]}

    reqs = run["requests"]
    by_ctx: dict[str, Any] = {}
    for ctx in sorted({r["num_ctx"] for r in reqs}):
        rs = [r for r in reqs if r["num_ctx"] == ctx]
        warm = [r for r in rs if r["ok"] and not r["cold"]]
        by_ctx[str(ctx)] = {
            "requests": len(rs),
            "errors": sum(not r["ok"] for r in rs),
            "cold_requests": sum(r["cold"] for r in rs),
            "cold_total_ms": [r["client_total_ms"] for r in rs if r["cold"]],
            "warm_ttft_ms_median": statistics.median(r["client_ttft_ms"] for r in warm) if warm else None,
            "warm_total_ms_median": statistics.median(r["client_total_ms"] for r in warm) if warm else None,
            "warm_tokens_per_s_median": statistics.median(r["usage"]["tokens_per_s"] for r in warm) if warm else None,
        }

    peak_uss, peak_sys = max(uss), max(sys_delta)
    vram_ref = max(ded) if ded else None
    return {
        "samples_in_window": {"slow": len(slow), "fast": len(fast)},
        "uss_complete": all(p["uss"] is not None for s in slow for p in s["procs"]),
        "peak_uss_mib": round(peak_uss / MIB, 1),
        "peak_uss_xodus_app_mib": round(max(summed(s, "uss", "xodus_app") for s in slow) / MIB, 1),
        "peak_uss_ollama_mib": round(max(summed(s, "uss", "ollama") for s in slow) / MIB, 1),
        "peak_rss_sum_mib": round(max(summed(s, "rss") for s in slow) / MIB, 1),
        "peak_private_commit_sum_mib": round(max(summed(s, "private_commit") for s in slow) / MIB, 1),
        "harness_uss_peak_mib": round(max(s["harness"]["uss"] or 0 for s in slow) / MIB, 1),
        "peak_system_delta_mib": round(peak_sys / MIB, 1),
        "system_delta_at_uss_peak_mib": round(sys_delta[i_peak] / MIB, 1),
        "ram_agreement": round(abs(peak_uss - peak_sys) / max(peak_uss, peak_sys), 3),
        "processes_at_uss_peak": peak_procs,
        "vram_baseline_mib": round(base["vram_used_median"] / MIB, 1),
        "vram_device_peak_mib": round(vram_peak / MIB, 1),
        "vram_device_delta_mib": round(vram_delta / MIB, 1),
        "vram_counters_dedicated_ollama_peak_mib": round(vram_ref / MIB, 1) if vram_ref is not None else None,
        "vram_counters_shared_ollama_peak_mib": round(max(shr) / MIB, 1) if shr else None,
        "api_ps_size_vram_peak_mib": round(max(size_vram) / MIB, 1) if size_vram else None,
        "api_ps_size_peak_mib": round(max(size_total) / MIB, 1) if size_total else None,
        "vram_diff_device_vs_counters_mib": round(abs(vram_delta - vram_ref) / MIB, 1) if vram_ref else None,
        "vram_agreement_ok": bool(vram_ref) and abs(vram_delta - vram_ref) <= max(VRAM_TOL_ABS, VRAM_TOL_FRAC * vram_delta),
        "latency_by_ctx": by_ctx,
        "sampler_overhead": run["sampler_overhead"],
    }


def environment(settings: Any) -> dict[str, Any]:
    def cmd(args: list[str]) -> str:
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"unavailable: {exc}"

    cfg_hash = hashlib.sha256(b"".join(p.read_bytes() for p in default_config_files())).hexdigest()[:16]
    battery = psutil.sensors_battery()
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "windows": platform.platform(),
        "python": sys.version.split()[0],
        "psutil": psutil.__version__,
        "cpu_logical": psutil.cpu_count(),
        "ram_total_mib": round(psutil.virtual_memory().total / MIB),
        "gpu": cmd(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,driver_model.current", "--format=csv,noheader"]),
        "power_plan": cmd(["powercfg", "/getactivescheme"]),
        "on_ac_power": battery.power_plugged if battery else None,
        "model": settings.ollama.model,
        "config_hash": cfg_hash,
        "ollama_settings": "scripts/start-ollama.ps1 (NUM_PARALLEL=1, MAX_LOADED_MODELS=2, FLASH_ATTENTION=1, KV q8_0)",
        "sampling": {**settings.ollama.sampling.model_dump(), "seed": SEED,
                     "max_output_tokens": settings.ollama.max_output_tokens},
    }


def pct_diff(a: float, b: float) -> float:
    return abs(a - b) / ((a + b) / 2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline-seconds", type=float, default=120)
    ap.add_argument("--runs", type=int, default=2)
    args = ap.parse_args()

    settings = load_settings()
    prompts = json.loads((REPO_ROOT / "experiments" / "smoke" / "prompts.json").read_text(encoding="utf-8"))
    ctxs = [4096, 8192]
    out = REPO_ROOT / "experiments" / "results" / f"smoke-{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True)
    env = environment(settings)

    runs = [one_run(i + 1, settings, prompts, ctxs, args.baseline_seconds, out) for i in range(args.runs)]
    for r in runs:
        r["health_at_start"]["ollama"].pop("details", None)
    env["ollama"] = runs[0]["health_at_start"]["ollama"]
    analyses = [analyse_run(r) for r in runs]

    checks: dict[str, Any] = {}
    for i, a in enumerate(analyses, 1):
        checks[f"run{i}_ram_tools_agree_within_20pct"] = a["ram_agreement"] <= RAM_TOL
        checks[f"run{i}_vram_tools_agree"] = a["vram_agreement_ok"]
        checks[f"run{i}_sampler_overhead_below_2pct"] = a["sampler_overhead"]["pct_of_total_cpu"] < OVERHEAD_MAX_PCT
        checks[f"run{i}_uss_complete"] = a["uss_complete"]
    if len(analyses) >= 2:
        a1, a2 = analyses[0], analyses[1]
        checks["runs_agree_peak_uss_5pct"] = pct_diff(a1["peak_uss_mib"], a2["peak_uss_mib"]) <= RUN_TOL
        checks["runs_agree_peak_vram_5pct"] = pct_diff(a1["vram_device_peak_mib"], a2["vram_device_peak_mib"]) <= RUN_TOL
    passed = all(checks.values())

    (out / "results.json").write_text(json.dumps(
        {"environment": env, "criteria": {"ram_tol": RAM_TOL, "vram_tol_frac": VRAM_TOL_FRAC,
                                          "vram_tol_abs_mib": VRAM_TOL_ABS / MIB, "run_tol": RUN_TOL,
                                          "overhead_max_pct": OVERHEAD_MAX_PCT},
         "checks": checks, "passed": passed, "analyses": analyses, "runs": runs}, indent=1, default=str), encoding="utf-8")

    lines = [f"# Smoke benchmark {env['timestamp']}", "", f"**Result: {'PASS' if passed else 'FAIL'}**", "",
             "| Check | Result |", "|---|---|"]
    lines += [f"| {k} | {'pass' if v else 'FAIL'} |" for k, v in checks.items()]
    lines += ["", "## Per run", "", "```json", json.dumps(
        [{k: v for k, v in a.items() if k != "processes_at_uss_peak"} for a in analyses], indent=1), "```",
              "", "## Processes at USS peak (run 1)", "", "```json",
              json.dumps(analyses[0]["processes_at_uss_peak"], indent=1), "```", "", "## Environment", "",
              "```json", json.dumps(env, indent=1, default=str), "```"]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n{'PASS' if passed else 'FAIL'}: {json.dumps(checks, indent=1)}\nResults: {out}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
