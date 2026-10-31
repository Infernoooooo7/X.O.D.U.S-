"""Gate F measurement scenarios (master prompt 3.6; experiments/gate-f/PROTOCOL.md sections 3-7).

Run from backend/ with Ollama and the backend STOPPED (or use scripts\\measure.ps1):
    uv run python ..\\scripts\\measure.py                 # two full runs, about 20 minutes each
    uv run python ..\\scripts\\measure.py --dry-run       # one short run to check the harness; never used for decisions

Each run: idle baseline (XODUS stopped) -> S1 startup -> S2 chat -> S3 idle -> S4 plugin -> S5 concurrency ->
S6 cancellation -> S7 ingestion -> S8 embedding -> S9 retrieval -> S10 model switching -> stop -> 60 s post-run baseline.
The backend runs on an isolated data and log folder inside the result folder (the user's data/ is never touched),
with one extra read-only root for the fixed corpus and one measurement-only profile for S10, both set through
environment variables; no configuration file is changed. Writes results.json, raw_samples.json and summary.md to
experiments/results/gate-f-<timestamp>/.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

from xodus.config.settings import REPO_ROOT, load_settings
from xodus.experiments.harness import (GB, Backend, Sampler, START_OLLAMA, STOP_OLLAMA, agreement, baseline, environment,
                                       keep_awake, mib, ollama_facts, other_activity, require_quiet_machine, port_busy, run_ps, sse, verify_manifest, vram_agrees, window,
                                       write_json)

CORPUS = REPO_ROOT / "experiments" / "corpus" / "gate-f-50"
PROMPTS = json.loads((REPO_ROOT / "experiments" / "smoke" / "prompts.json").read_text(encoding="utf-8"))
SEED = 42
SWITCH_PROFILE = {"id": "measure-switch", "label": "Measurement only: Qwen3 4B Instruct", "model": "qwen3:4b-instruct-2507-q4_K_M",
                  "expected_digest_prefix": "0edcdef34593", "capabilities": ["completion", "structured", "tools"],
                  "num_ctx": 8192, "note": "Gate F model-switching scenario only; set by the harness, not by the configuration files."}
LONG_PROMPT = "Write a detailed essay of about 800 words on the history of the printing press, with headings."
SEARCHES = [
    "parse a JSON document into Python objects", "compile a regular expression and match groups",
    "read rows from a CSV file with a dialect", "open an SQLite database connection and run a query",
    "start a thread and wait for it to finish", "format a date as an ISO string", "add a command line option with a default",
    "compute a SHA-256 hash of bytes", "shuffle a list randomly with a seed", "extract members from a zip archive",
    "copy a directory tree recursively", "create a named temporary file", "count items with Counter",
    "chain several iterables together", "cache a function's results with lru_cache", "configure a logger with a handler",
    "encode bytes as base64", "generate a random UUID", "split a path into its parts", "run a child process and capture output",
]
QUESTIONS = [
    "How do I parse a JSON string into a Python object?", "How do I copy a whole directory tree?",
    "How do I compute the mean and median of a list of numbers?", "How do I keep a list sorted while inserting into it?",
    "How do I generate a cryptographically secure random token?", "How do I compare two sequences and show their differences?",
    "Which warehouse has the item with SKU-0007, and how many are in stock?", "How do I wrap a long paragraph to a fixed width?",
    "How do I schedule a function to run after a delay with sched?", "How do I create a dataclass with default values?",
]
EXPECTED_SCENARIOS = ["S1_startup", "S2_chat", "S3_idle", "S4_plugin", "S5_concurrency", "S6_cancellation", "S7_ingestion",
                      "S8_embedding", "S9_retrieval", "S10_model_switch"]


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def chat(client: Any, prompt: str, num_ctx: int = 8192, **kw: Any) -> dict[str, Any]:
    r = sse(client, "/api/chat", {"message": prompt, "num_ctx": num_ctx, "seed": SEED}, **kw)
    done = r.pop("done") or {}
    r["timing"], r["usage"], r["num_ctx"] = done.get("timing"), done.get("usage"), num_ctx
    r["cold"] = bool(r["timing"] and r["timing"].get("ollama_load_ms", 0) > 500)
    return r


def concurrent(backend: Backend, prompts: list[str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any] | None] = [None] * len(prompts)
    barrier = threading.Barrier(len(prompts))

    def worker(i: int) -> None:
        with backend.client() as c:
            barrier.wait()
            results[i] = chat(c, prompts[i])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(prompts))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return [r for r in results if r is not None]


def lat(rs: list[dict[str, Any]], key: str = "client_total_ms") -> dict[str, Any]:
    vals = [r[key] for r in rs if r.get(key) is not None]
    return {"n": len(rs), "ok": sum(1 for r in rs if r.get("ok")), "median_ms": statistics.median(vals) if vals else None,
            "max_ms": max(vals) if vals else None}


class Run:
    def __init__(self, label: str, out: Path, settings: Any, args: argparse.Namespace) -> None:
        self.label, self.settings, self.args = label, settings, args
        self.dir = out / f"run-{label}"
        (self.dir / "data").mkdir(parents=True)
        roots = [{"id": r.id, "path": str(r.path), "grants": r.grants} for r in settings.permissions.roots]
        roots.append({"id": "corpus", "path": str(CORPUS), "grants": ["fs.read"]})
        env = {**os.environ, "XODUS_DATA_DIR": str(self.dir / "data"), "XODUS_LOG_DIR": str(self.dir / "logs"),
               "XODUS_PERMISSIONS__ROOTS": json.dumps(roots), "XODUS_MODELS__PROFILES": json.dumps([SWITCH_PROFILE])}
        self.backend = Backend(env, self.dir / "data", self.dir / "backend.log", settings.server.port)
        self.scenarios: dict[str, dict[str, Any]] = {}
        self.sampler: Sampler | None = None

    def scenario(self, name: str, fn: Any) -> None:
        log(f"[{self.label}] {name} ...")
        t0 = time.time()
        try:
            detail = fn() or {}
            error = None
        except Exception as exc:  # a failing scenario is recorded, never hidden; the run continues
            detail, error = {}, repr(exc)
            log(f"[{self.label}] {name} FAILED: {error}")
        t1 = time.time()
        self.scenarios[name] = {"t0": t0, "t1": t1, "detail": detail, "error": error, "other_activity": other_activity()}
        log(f"[{self.label}] {name} done in {t1 - t0:.0f} s")

    def governor_level(self, client: Any) -> str | None:
        try:
            return client.get("/api/health").json()["resources"]["level"]
        except Exception:
            return None

    def execute(self) -> dict[str, Any]:
        if port_busy(11434) or port_busy(self.settings.server.port):
            raise SystemExit("Ports 11434 and 8765 must be free: stop Ollama and the backend first.")
        log(f"[{self.label}] idle baseline {self.args.baseline_seconds:.0f} s (XODUS stopped) ...")
        base = baseline(self.args.baseline_seconds)
        self.sampler = s = Sampler().start()
        cpu0, wall0 = s.cpu_seconds(), time.time()
        b = self.backend
        facts: dict[str, Any] = {}

        def s1() -> dict[str, Any]:
            started = run_ps(START_OLLAMA, self.dir)
            t = time.time()
            health = b.start()
            s.backend = b.psutil
            facts.update(ollama_facts())
            return {"ollama": started, "backend_ready_s": round(time.time() - t, 1), "health": {
                k: health[k] for k in ("ollama", "ollama_environment", "measurement", "resources", "models")}}

        self.scenario("S1_startup", s1)
        c = b.client()
        self.scenario("S2_chat", lambda: self.s2(c))
        self.scenario("S3_idle", lambda: (time.sleep(self.args.idle_seconds), {"seconds": self.args.idle_seconds,
                                                                               "governor_level": self.governor_level(c)})[1])
        self.scenario("S4_plugin", lambda: self.s4(c))
        self.scenario("S5_concurrency", lambda: self.s5(b))
        self.scenario("S6_cancellation", lambda: self.s6(c))
        self.scenario("S7_ingestion", lambda: self.s7(c))
        self.scenario("S8_embedding", lambda: self.s8(c))
        self.scenario("S9_retrieval", lambda: self.s9(c))
        self.scenario("S10_model_switch", lambda: self.s10(c))
        audit = c.get("/api/audit/verify").json()
        resources_events = [r for r in c.get("/api/audit", params={"limit": 200}).json()["records"]
                            if r["event"].startswith("resource_")]
        c.close()
        t_end = time.time()
        cpu1 = s.cpu_seconds()
        log(f"[{self.label}] stopping the backend and Ollama ...")
        b.stop()
        run_ps(STOP_OLLAMA, self.dir)
        time.sleep(5)
        s.stop()
        log(f"[{self.label}] post-run baseline 60 s ...")
        post = baseline(60, attempts=1)
        embed = self.settings.ollama.embed_model
        for name, sc in self.scenarios.items():
            sc["window"] = window(s, sc["t0"], sc["t1"], base, embed)
        whole = window(s, self.scenarios["S1_startup"]["t0"], t_end, base, embed)
        return {"label": self.label, "baseline": base, "post_baseline": post, "ollama": facts, "scenarios": self.scenarios,
                "whole_run": whole, "audit_verify": audit, "resource_events": resources_events,
                "sampler_overhead": {"cpu_seconds": round(cpu1 - cpu0, 2), "wall_seconds": round(t_end - wall0, 1),
                                     "pct_of_total_cpu": round(100 * (cpu1 - cpu0) / ((t_end - wall0) * (psutil.cpu_count() or 1)), 3)},
                "raw": {"fast": s.fast, "slow": s.slow}}

    # ------------------------------------------------------------------ scenarios

    def s2(self, c: Any) -> dict[str, Any]:
        at8k = [chat(c, p) for p in PROMPTS]
        at16k = [chat(c, p, 16384) for p in PROMPTS[:3]]
        restore = chat(c, PROMPTS[0])  # back to the default context for the scenarios that follow
        warm = [r for r in at8k if r["ok"] and not r["cold"]]
        return {"ctx_8192": lat(at8k), "ctx_8192_warm_ttft": lat(warm, "client_ttft_ms"),
                "ctx_8192_tokens_per_s_median": statistics.median(r["usage"]["tokens_per_s"] for r in warm) if warm else None,
                "ctx_16384": lat(at16k), "ctx_16384_cold": [r["cold"] for r in at16k], "restore_8192_ok": restore["ok"],
                "errors": [r["error"] for r in at8k + at16k + [restore] if not r["ok"]], "requests": at8k + at16k + [restore]}

    def s4(self, c: Any) -> dict[str, Any]:
        calls = {"text.stats": {"text": " ".join(PROMPTS)}, "calc.evaluate": {"expression": "(17.5 * 4) / 3 + sqrt(2)"}}
        out = {}
        for cap, args in calls.items():
            timings = []
            for _ in range(2):
                t0 = time.perf_counter()
                r = c.post(f"/api/capabilities/{cap}/invoke", json={"args": args})
                timings.append({"status": r.status_code, "ok": r.json().get("ok"), "ms": round((time.perf_counter() - t0) * 1000, 1)})
            out[cap] = {"first": timings[0], "warm": timings[1]}
        optional = [p for p in c.get("/api/resources").json()["processes"] if p["tier"] == "optional"]
        out["optional_processes"] = [{k: p[k] for k in ("pid", "owner", "role", "uss_mib")} for p in optional]
        time.sleep(3)  # the calls take under 0.5 s; without a hold the window can hold no 500 ms sample (run B, 29 Oct)
        return out

    def s5(self, b: Backend) -> dict[str, Any]:
        three = concurrent(b, PROMPTS[1:4])
        burst = concurrent(b, PROMPTS[4:10])
        refused = [r for r in burst if not r["ok"] and (r["error"] or {}).get("code") == "queue_full"]
        return {"three": lat(three), "three_queue_wait_ms": [r["timing"]["queue_wait_ms"] for r in three if r["timing"]],
                "burst_of_6": lat(burst), "burst_refused_queue_full": len(refused),
                "refusal_message": refused[0]["error"]["message"] if refused else None,
                "other_errors": [r["error"] for r in burst + three if not r["ok"] and r not in refused]}

    def s6(self, c: Any) -> dict[str, Any]:
        cut = chat(c, LONG_PROMPT, stop_after_tokens=20)
        t = time.time()
        time.sleep(1.0)
        after = chat(c, PROMPTS[0])
        return {"tokens_before_disconnect": cut["tokens"], "disconnected": cut["disconnected"],
                "next_chat_ok": after["ok"], "next_chat_queue_wait_ms": (after["timing"] or {}).get("queue_wait_ms"),
                "next_chat_total_ms": after["client_total_ms"], "seconds_between": round(time.time() - t, 1)}

    def s7(self, c: Any) -> dict[str, Any]:
        bad = verify_manifest(CORPUS)
        if bad:
            raise RuntimeError(f"corpus differs from MANIFEST.json: {bad}")
        c.post("/api/knowledge/collections", json={"name": "gatef"})
        t0 = time.perf_counter()
        r = c.post("/api/knowledge/ingest", json={"collection": "gatef", "root": "corpus", "path": ".", "recursive": False})
        queued = r.json()
        levels = []
        deadline = time.time() + self.args.ingest_timeout
        while time.time() < deadline:
            jobs = c.get("/api/knowledge/jobs", params={"limit": 200}).json()
            levels.append(self.governor_level(c))
            if all(j["status"] in ("completed", "failed", "cancelled") for j in jobs):
                break
            time.sleep(2)
        stats = c.get("/api/knowledge/stats").json()
        return {"queued": len(queued.get("jobs", [])), "rejected": queued.get("rejected"),
                "seconds": round(time.perf_counter() - t0, 1),
                "statuses": {st: sum(1 for j in jobs if j["status"] == st) for st in {j["status"] for j in jobs}},
                "failed": [{"path": j.get("path"), "error": j.get("error")} for j in jobs if j["status"] == "failed"],
                "chunks": stats.get("chunks"), "documents": stats.get("documents"),
                "chunks_per_s": round((stats.get("chunks") or 0) / max(1e-6, time.perf_counter() - t0), 2),
                "governor_levels_seen": sorted({lv for lv in levels if lv})}

    def s8(self, c: Any) -> dict[str, Any]:
        rs = []
        for q in SEARCHES:
            t0 = time.perf_counter()
            r = c.post("/api/knowledge/search", json={"query": q, "collections": ["gatef"]})
            body = r.json()
            rs.append({"status": r.status_code, "ms": round((time.perf_counter() - t0) * 1000, 1), "mode": body.get("mode"),
                       "results": len(body.get("results", [])), "degraded": body.get("degraded")})
        ms = [r["ms"] for r in rs]
        return {"n": len(rs), "ok": sum(r["status"] == 200 for r in rs), "first_ms": ms[0],
                "median_ms": statistics.median(ms), "max_ms": max(ms), "modes": sorted({str(r["mode"]) for r in rs}),
                "degraded": sum(bool(r["degraded"]) for r in rs)}

    def s9(self, c: Any) -> dict[str, Any]:
        rs = [sse(c, "/api/knowledge/ask", {"question": q, "collections": ["gatef"]}) for q in QUESTIONS]
        dones = [r["done"] or {} for r in rs]
        return {"answers": lat(rs), "ttft": lat(rs, "client_ttft_ms"),
                "valid_citations": [len(d.get("citations", {}).get("valid", []) if isinstance(d.get("citations"), dict)
                                        else d.get("citations") or []) for d in dones],
                "insufficient": sum(bool(d.get("insufficient")) for d in dones),
                "errors": [r["error"] for r in rs if not r["ok"]]}

    def s10(self, c: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for step, profile in (("to_challenger", "measure-switch"), ("back_to_default", "default")):
            t0 = time.perf_counter()
            r = c.post("/api/models/switch", json={"profile": profile})
            body = r.json()
            after = chat(c, PROMPTS[0])
            out[step] = {"status": r.status_code, "ms": round((time.perf_counter() - t0) * 1000, 1),
                         "switch": body.get("switch") or body.get("error"), "chat_ok": after["ok"],
                         "chat_total_ms": after["client_total_ms"]}
        return out


# ---------------------------------------------------------------------------------------------- analysis


def checks_for(run: dict[str, Any]) -> dict[str, Any]:
    w = run["whole_run"]
    quiet = run["baseline"]["quiet"]
    ram = agreement(w["uss_total"]["peak_mib"], w["system_delta"]["peak_mib"])
    per_scenario_complete = {n: sc["window"]["samples"]["complete_ratio"] for n, sc in run["scenarios"].items()}
    return {
        "baseline_quiet": quiet,
        "ram_tools_agree_within_20pct": (ram is not None and ram <= 0.20) if quiet else "inconclusive",
        "ram_agreement": ram,
        "vram_tools_agree": vram_agrees(w["device_vram_delta"]["peak_mib"], w["gpu_dedicated_xodus"]["peak_mib"]),
        "uss_complete_98pct_every_scenario": all((v or 0) >= 0.98 for v in per_scenario_complete.values()),
        "complete_ratio_by_scenario": per_scenario_complete,
        "sampler_overhead_below_2pct": run["sampler_overhead"]["pct_of_total_cpu"] < 2.0,
        "all_scenarios_ran": [n for n in EXPECTED_SCENARIOS if n not in run["scenarios"] or run["scenarios"][n]["error"]] == [],
        "audit_chain_ok": run["audit_verify"].get("ok"),
        "no_other_activity": not any(sc.get("other_activity") for sc in run["scenarios"].values()),
    }


def budget(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Protocol section 7: each target met, missed or not measured, from measurements only (worst run counts)."""
    def peak(path: Any) -> float | None:
        vals = [v for v in (path(r) for r in runs) if v is not None]
        return max(vals) if vals else None

    def role_peak(prefix: str, scenarios: list[str] | None = None) -> float | None:
        vals = []
        for r in runs:
            for name, sc in r["scenarios"].items():
                if scenarios and name not in scenarios:
                    continue
                vals += [v for role, v in sc["window"]["role_uss_peak_mib"].items() if role.startswith(prefix) and v is not None]
        return max(vals) if vals else None

    embed = runs[0]["ollama"] and next((n for n in runs[0]["ollama"].get("models", {}) if "embedding" in n), None)
    gb = lambda m: None if m is None else round(m * 2**20 / GB, 3)  # noqa: E731
    rows = []

    def row(target: str, measured: str, value: float | None, limit: float, unit: str = "GB") -> None:
        rows.append({"target": target, "measured_as": measured, "value": value, "limit": limit, "unit": unit,
                     "verdict": "not measured" if value is None else ("met" if value <= limit else "missed")})

    row("XODUS RAM goal about 4 GB", "peak summed USS, all scenarios", gb(peak(lambda r: r["whole_run"]["uss_total"]["peak_mib"])), 4.0)
    row("XODUS RAM ceiling 6 GB", "peak summed USS + shared GPU memory", gb(peak(lambda r: r["whole_run"]["uss_plus_shared_gpu_peak_mib"])), 6.0)
    row("XODUS VRAM about 4.0-4.5 GB", "peak device VRAM increase over baseline", gb(peak(lambda r: r["whole_run"]["device_vram_delta"]["peak_mib"])), 4.5)
    row("Ollama server + generation runner RAM <= 1.0 GB", "Ollama line USS peak (all its processes)",
        gb(peak(lambda r: r["whole_run"]["lines"]["ollama"]["uss"]["peak_mib"])), 1.0)
    row("Generation runner VRAM <= 4.0 GB", "per-process dedicated GPU memory of XODUS processes, peak",
        gb(peak(lambda r: r["whole_run"]["gpu_dedicated_xodus"]["peak_mib"])), 4.0)
    row("Embedding runner <= 1.0 GB RAM on CPU", f"USS of the runner serving {embed} (S7, S8)",
        gb(role_peak(f"runner:{embed}", ["S7_ingestion", "S8_embedding", "S9_retrieval"])) if embed else None, 1.0)
    row("Backend <= 0.6 GB", "backend process USS", gb(role_peak("backend")), 0.6)
    row("Ingestion worker <= 0.4 GB peak", "worker USS during S7", gb(role_peak("ingestion", ["S7_ingestion"])), 0.4)
    rows.append({"target": "PostgreSQL <= 0.4 GB", "measured_as": "n/a", "value": None, "limit": 0.4, "unit": "GB",
                 "verdict": "not measured (PostgreSQL not installed; SQLite runs inside the backend)"})
    s5 = [r["scenarios"]["S5_concurrency"]["detail"] for r in runs if r["scenarios"].get("S5_concurrency", {}).get("detail")]
    rows.append({"target": "Envelope: 1 running, <= 4 queued, beyond that a clear refusal", "measured_as": "S5",
                 "value": [{"three_ok": d["three"]["ok"], "burst_ok": d["burst_of_6"]["ok"], "refused": d["burst_refused_queue_full"]} for d in s5],
                 "verdict": "not measured" if not s5 else ("met" if all(d["three"]["ok"] == 3 and d["burst_refused_queue_full"] >= 1
                                                                       and d["burst_of_6"]["ok"] == 5 for d in s5) else "missed")})
    gens = peak(lambda r: r["whole_run"]["api_ps_max_generation_models"])
    rows.append({"target": "Envelope: 1 generation model + 1 embedder resident", "measured_as": "/api/ps in every sample (S10 included)",
                 "value": gens, "verdict": "not measured" if gens is None else ("met" if gens <= 1 else "missed")})
    s2 = [r["scenarios"]["S2_chat"] for r in runs if "S2_chat" in r["scenarios"]]
    fit = []
    for sc in s2:
        ps = sc["window"]["api_ps_at_end"] or []
        fit.append(all(m.get("size_vram") == m.get("size") for m in ps) and sc["detail"].get("ctx_16384", {}).get("ok") == 3)
    rows.append({"target": "Envelope: context 16384 where measured to fit", "measured_as": "S2 at 16384: all ok, full GPU placement",
                 "value": fit, "verdict": "not measured" if not fit else ("met" if all(fit) else "missed")})
    return rows


def write_summary(out: Path, env: dict[str, Any], runs: list[dict[str, Any]], dry: bool) -> None:
    checks = {r["label"]: checks_for(r) for r in runs}
    if len(runs) >= 2:
        a, b = runs[0]["whole_run"], runs[1]["whole_run"]
        diff = lambda x, y: None if not x or not y else round(abs(x - y) / ((x + y) / 2), 3)  # noqa: E731
        checks["repeatability"] = {
            "peak_uss_diff": diff(a["uss_total"]["peak_mib"], b["uss_total"]["peak_mib"]),
            "vram_delta_diff": diff(a["device_vram_delta"]["peak_mib"], b["device_vram_delta"]["peak_mib"])}
        checks["repeatability"]["within_5pct"] = all(v is not None and v <= 0.05 for v in checks["repeatability"].values())
    table = budget(runs)
    scen = {r["label"]: {n: {"duration_s": sc["window"]["duration_s"], "uss_total": sc["window"]["uss_total"],
                             "lines_uss_peak_mib": {k: v["uss"]["peak_mib"] for k, v in sc["window"]["lines"].items()},
                             "role_uss_peak_mib": sc["window"]["role_uss_peak_mib"],
                             "system_delta": sc["window"]["system_delta"], "gpu_shared_xodus": sc["window"]["gpu_shared_xodus"],
                             "device_vram_delta": sc["window"]["device_vram_delta"],
                             "gpu_dedicated_xodus": sc["window"]["gpu_dedicated_xodus"],
                             "device_vram_free_min_mib": sc["window"]["device_vram_free_min_mib"],
                             "system_available_min_mib": sc["window"]["system_available_min_mib"],
                             "error": sc["error"], "detail": {k: v for k, v in sc["detail"].items() if k != "requests"}}
                         for n, sc in r["scenarios"].items()} for r in runs}
    write_json(out / "results.json", {"dry_run": dry, "environment": env, "checks": checks, "budget": table,
                                      "scenarios": scen, "runs": [{k: v for k, v in r.items() if k != "raw"} for r in runs]})
    write_json(out / "raw_samples.json", {r["label"]: r["raw"] for r in runs})
    lines = [f"# Gate F measurement {env['timestamp']}{' (DRY RUN: not used for decisions)' if dry else ''}", "",
             "Protocol: experiments/gate-f/PROTOCOL.md. GB = 10^9 bytes; MiB = 2^20 bytes. Browser tab: not run, not measured "
             "(excluded from every aggregate).", "", "## Budget (worst run)", "", "| Target | Measured as | Value | Limit | Verdict |",
             "|---|---|---|---|---|"]
    lines += [f"| {r['target']} | {r['measured_as']} | {r['value']} | {r.get('limit', '')} | {r['verdict']} |" for r in table]
    lines += ["", "## Pipeline checks", "", "```json", json.dumps(checks, indent=1), "```", "", "## Scenarios", ""]
    for label, sc in scen.items():
        lines += [f"### Run {label}", "", "| Scenario | s | USS median / peak MiB | backend / ollama / optional peak MiB | sys delta peak MiB "
                  "| shared GPU peak MiB | VRAM delta peak MiB | XODUS dedicated peak MiB | error |", "|---|---|---|---|---|---|---|---|---|"]
        for n, v in sc.items():
            lp = v["lines_uss_peak_mib"]
            lines.append(f"| {n} | {v['duration_s']} | {v['uss_total']['median_mib']} / {v['uss_total']['peak_mib']} | "
                         f"{lp['xodus_app']} / {lp['ollama']} / {lp['optional']} | {v['system_delta']['peak_mib']} | "
                         f"{v['gpu_shared_xodus']['peak_mib']} | {v['device_vram_delta']['peak_mib']} | {v['gpu_dedicated_xodus']['peak_mib']} | "
                         f"{v['error'] or ''} |")
        lines.append("")
    lines += ["## Scenario details", "", "```json", json.dumps({l: {n: v["detail"] for n, v in s.items()} for l, s in scen.items()},
                                                               indent=1, default=str)[:60000], "```", "", "## Environment", "",
              "```json", json.dumps(env, indent=1, default=str), "```"]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--baseline-seconds", type=float, default=120)
    ap.add_argument("--idle-seconds", type=float, default=300)
    ap.add_argument("--ingest-timeout", type=float, default=1800)
    ap.add_argument("--dry-run", action="store_true", help="one short run (30 s baseline, 10 s idle) to check the harness")
    args = ap.parse_args()
    require_quiet_machine()
    keep_awake()
    if args.dry_run:
        args.runs, args.baseline_seconds, args.idle_seconds = 1, 30, 10
    settings = load_settings()
    out = REPO_ROOT / "experiments" / "results" / f"gate-f-{'dry-' if args.dry_run else ''}{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True)
    env = environment(settings, {"protocol": "experiments/gate-f/PROTOCOL.md", "dry_run": args.dry_run,
                                 "args": vars(args), "switch_profile": SWITCH_PROFILE})
    runs = []
    for label in "ABCDEFG"[:args.runs]:
        result = Run(label, out, settings, args).execute()
        runs.append(result)
        env.setdefault("ollama", result["ollama"])
        write_summary(out, env, runs, args.dry_run)  # after every run, so a later failure loses nothing
    log(f"Results: {out}")
    print((out / "summary.md").read_text(encoding="utf-8")[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
