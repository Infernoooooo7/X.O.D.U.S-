"""Smoke benchmark (master prompt v2, section 6) - validates the measurement pipeline.

Default profile only, 10 fixed prompts, num_ctx 4096 then 8192, two identical cold-start runs.
Each run: idle baseline with XODUS stopped -> start Ollama + backend -> 20 requests through the
backend's /api/chat -> stop everything.

Run from backend/ with Ollama and the backend STOPPED:
    uv run python ..\\scripts\\smoke_benchmark.py

Pass criteria (experiments/gate-f/PROTOCOL.md section 6, fixed before the Gate F runs):
  1. Idle baseline quiet (system in-use spread <= 256 MiB, VRAM spread <= 128 MiB), else the RAM comparison of that
     run is "inconclusive", never a pass.
  2. RAM: peak summed USS of XODUS processes vs peak system-wide increase over the idle baseline agree within 20%.
  3. VRAM: peak device increase over baseline vs peak per-process dedicated GPU memory (Windows counters) agree within
     max(15%, 300 MiB). The brief gives no number; this tolerance is the harness author's and awaits the user's review.
  4. At least 98% of samples have a USS reading for every process.
  5. Two runs agree within 5% on peak USS and on the peak device VRAM increase over their own baselines.
  6. Sampler threads use < 2% of total CPU capacity.
Changes since the Gate B version: the sampler is the shared harness (xodus/experiments/harness.py); USS reads are
retried once; the backend needs its API token; criteria 1 and 4 added; criterion 5 compares VRAM increases.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime
from typing import Any

import psutil

from xodus.config.settings import REPO_ROOT, load_settings
from xodus.experiments.harness import (Backend, Sampler, agreement, baseline, environment, keep_awake, mib, require_quiet_machine, ollama_facts, port_busy,
                                       run_ps, START_OLLAMA, STOP_OLLAMA, sse, vram_agrees, window, write_json)

RAM_TOL, RUN_TOL, OVERHEAD_MAX_PCT, COMPLETE_MIN = 0.20, 0.05, 2.0, 0.98
SEED = 42


def one_run(index: int, settings: Any, prompts: list[str], ctxs: list[int], baseline_s: float, out: Any) -> dict[str, Any]:
    if port_busy(11434) or port_busy(settings.server.port):
        raise SystemExit("Ports 11434/8765 must be free (stop Ollama and the backend first).")
    print(f"[run {index}] idle baseline {baseline_s:.0f} s ...", flush=True)
    base = baseline(baseline_s)
    run_dir = out / f"run{index}"
    (run_dir / "data").mkdir(parents=True)
    env = {**os.environ, "XODUS_DATA_DIR": str(run_dir / "data"), "XODUS_LOG_DIR": str(run_dir / "logs")}
    backend = Backend(env, run_dir / "data", run_dir / "backend.log", settings.server.port)

    sampler = Sampler().start()
    cpu0, wall0 = sampler.cpu_seconds(), time.time()
    t_services = time.time()
    print(f"[run {index}] {run_ps(START_OLLAMA, run_dir)}", flush=True)
    health = backend.start()
    sampler.backend = backend.psutil
    facts = ollama_facts()
    requests: list[dict[str, Any]] = []
    with backend.client() as client:
        for ctx in ctxs:
            for i, prompt in enumerate(prompts):
                r = sse(client, "/api/chat", {"message": prompt, "num_ctx": ctx, "seed": SEED})
                timing = (r["done"] or {}).get("timing") or {}
                r.update(prompt_index=i, num_ctx=ctx, usage=(r["done"] or {}).get("usage"), server_timing=timing,
                         cold=timing.get("ollama_load_ms", 0) > 500)
                r.pop("done")
                requests.append(r)
                print(f"[run {index}] ctx={ctx} prompt={i} ok={r['ok']} ttft={r['client_ttft_ms']} total={r['client_total_ms']}",
                      flush=True)
    time.sleep(3)  # tail of the steady window
    t_done = time.time()
    cpu1, wall1 = sampler.cpu_seconds(), time.time()
    backend.stop()
    run_ps(STOP_OLLAMA, run_dir)
    time.sleep(3)
    sampler.stop()
    w = window(sampler, t_services, t_done, base, settings.ollama.embed_model)
    by_ctx = {}
    for ctx in ctxs:
        rs = [r for r in requests if r["num_ctx"] == ctx]
        warm = [r for r in rs if r["ok"] and not r["cold"]]
        by_ctx[str(ctx)] = {"requests": len(rs), "errors": sum(not r["ok"] for r in rs), "cold_requests": sum(r["cold"] for r in rs),
                            "warm_ttft_ms_median": statistics.median(r["client_ttft_ms"] for r in warm) if warm else None,
                            "warm_total_ms_median": statistics.median(r["client_total_ms"] for r in warm) if warm else None,
                            "warm_tokens_per_s_median": statistics.median(r["usage"]["tokens_per_s"] for r in warm) if warm else None}
    return {"run": index, "baseline": base, "health_at_start": health, "ollama": facts, "window": w, "latency_by_ctx": by_ctx,
            "requests": requests,
            "sampler_overhead": {"cpu_seconds": round(cpu1 - cpu0, 3), "wall_seconds": round(wall1 - wall0, 1),
                                 "pct_of_total_cpu": round(100 * (cpu1 - cpu0) / ((wall1 - wall0) * (psutil.cpu_count() or 1)), 3)},
            "raw": {"fast": sampler.fast, "slow": sampler.slow}}


def pct_diff(a: float, b: float) -> float:
    return abs(a - b) / ((a + b) / 2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline-seconds", type=float, default=120)
    ap.add_argument("--runs", type=int, default=2)
    args = ap.parse_args()
    require_quiet_machine()
    keep_awake()

    settings = load_settings()
    prompts = json.loads((REPO_ROOT / "experiments" / "smoke" / "prompts.json").read_text(encoding="utf-8"))
    out = REPO_ROOT / "experiments" / "results" / f"smoke-{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True)
    env = environment(settings, {"sampling": {**settings.ollama.sampling.model_dump(), "seed": SEED,
                                              "max_output_tokens": settings.ollama.max_output_tokens}})
    runs = [one_run(i + 1, settings, prompts, [4096, 8192], args.baseline_seconds, out) for i in range(args.runs)]

    checks: dict[str, Any] = {}
    summary = []
    for r in runs:
        w, i = r["window"], r["run"]
        uss, sysd = w["uss_total"]["peak_mib"], w["system_delta"]["peak_mib"]
        ram = agreement(uss, sysd)
        quiet = r["baseline"]["quiet"]
        checks[f"run{i}_baseline_quiet"] = quiet
        checks[f"run{i}_ram_tools_agree_within_20pct"] = (ram is not None and ram <= RAM_TOL) if quiet else "inconclusive"
        checks[f"run{i}_vram_tools_agree"] = vram_agrees(w["device_vram_delta"]["peak_mib"], w["gpu_dedicated_xodus"]["peak_mib"])
        checks[f"run{i}_uss_complete_98pct"] = (w["samples"]["complete_ratio"] or 0) >= COMPLETE_MIN
        checks[f"run{i}_sampler_overhead_below_2pct"] = r["sampler_overhead"]["pct_of_total_cpu"] < OVERHEAD_MAX_PCT
        summary.append({"run": i, "baseline": {k: r["baseline"][k] for k in ("system_spread_mib", "vram_spread_mib", "quiet")}
                        | {"vram_used_median_mib": mib(r["baseline"]["vram_used_median"])},
                        "peak_uss_mib": uss, "peak_system_delta_mib": sysd, "ram_agreement": ram,
                        "uss_plus_shared_gpu_peak_mib": w["uss_plus_shared_gpu_peak_mib"],
                        "gpu_shared_xodus_peak_mib": w["gpu_shared_xodus"]["peak_mib"],
                        "device_vram_delta_peak_mib": w["device_vram_delta"]["peak_mib"],
                        "device_vram_peak_mib": w["device_vram_peak_mib"],
                        "gpu_dedicated_xodus_peak_mib": w["gpu_dedicated_xodus"]["peak_mib"],
                        "lines_uss_peak_mib": {k: v["uss"]["peak_mib"] for k, v in w["lines"].items()},
                        "role_uss_peak_mib": w["role_uss_peak_mib"], "samples": w["samples"],
                        "latency_by_ctx": r["latency_by_ctx"], "sampler_overhead": r["sampler_overhead"],
                        "harness_uss_peak_mib": w["harness_uss_peak_mib"], "api_ps_at_end": w["api_ps_at_end"]})
    if len(runs) >= 2:
        a, b = runs[0]["window"], runs[1]["window"]
        checks["runs_agree_peak_uss_5pct"] = pct_diff(a["uss_total"]["peak_mib"], b["uss_total"]["peak_mib"]) <= RUN_TOL
        checks["runs_agree_vram_delta_5pct"] = pct_diff(a["device_vram_delta"]["peak_mib"],
                                                        b["device_vram_delta"]["peak_mib"]) <= RUN_TOL
    passed = all(v is True for v in checks.values())
    env["ollama"] = runs[0]["ollama"]

    write_json(out / "results.json", {"environment": env, "criteria": {"ram_tol": RAM_TOL, "run_tol": RUN_TOL,
               "overhead_max_pct": OVERHEAD_MAX_PCT, "complete_min": COMPLETE_MIN, "vram_tol": "max(15%, 300 MiB)"},
               "checks": checks, "passed": passed, "summary": summary,
               "runs": [{k: v for k, v in r.items() if k != "raw"} for r in runs]})
    write_json(out / "raw_samples.json", [r["raw"] for r in runs])
    lines = [f"# Smoke benchmark {env['timestamp']}", "", f"**Result: {'PASS' if passed else 'FAIL'}**", "",
             "| Check | Result |", "|---|---|"]
    lines += [f"| {k} | {'pass' if v is True else ('FAIL' if v is False else v)} |" for k, v in checks.items()]
    lines += ["", "## Per run", "", "```json", json.dumps(summary, indent=1), "```", "", "## Environment", "", "```json",
              json.dumps(env, indent=1, default=str), "```"]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n{'PASS' if passed else 'FAIL'}: {json.dumps(checks, indent=1)}\nResults: {out}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
