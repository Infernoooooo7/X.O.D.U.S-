"""Gate F mitigation experiment: does capping llama-server's prompt cache bound the generation runner's RAM?

Finding it tests (Gate F run A, S9): Ollama 0.40.11 runs qwen3.5:4b in llama.cpp's llama-server, whose host-RAM prompt
cache (default limit 8192 MiB) kept every earlier prompt: 18 prompts, 2,718 MiB, and the runner's USS rose by about
200 MiB per long-prompt request. llama-server reads LLAMA_ARG_CACHE_RAM (0 disables the cache) and LLAMA_ARG_CTX_CHECKPOINTS
from its environment; start-ollama.ps1 can set them if Ollama passes its environment on to the runner. Measured here.

Run from backend/ with Ollama and the backend STOPPED:
    uv run python ..\\scripts\\cache_experiment.py <folder of a measured run, e.g. ...\\gate-f-<ts>\\run-A>
Copies that run's database (the 50-document corpus already ingested) into a fresh folder per condition, so no condition
re-ingests. Per condition: start Ollama with the condition's variables, start the backend, 20 retrieval questions
(about 2,500-token prompts) and a 6-turn conversation; sample the generation runner's USS; stop everything.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from xodus.config.settings import REPO_ROOT, load_settings
from xodus.experiments.harness import (Backend, Sampler, START_OLLAMA, STOP_OLLAMA, environment, keep_awake, port_busy, require_quiet_machine, run_ps, sse,
                                       write_json)

CORPUS = REPO_ROOT / "experiments" / "corpus" / "gate-f-50"
CONDITIONS = {
    "default": {"LLAMA_ARG_CACHE_RAM": "8192", "LLAMA_ARG_CTX_CHECKPOINTS": "32"},  # llama-server's own defaults
    "cache_ram_0": {"LLAMA_ARG_CACHE_RAM": "0", "LLAMA_ARG_CTX_CHECKPOINTS": "32"},
    "cache_ram_0_checkpoints_2": {"LLAMA_ARG_CACHE_RAM": "0", "LLAMA_ARG_CTX_CHECKPOINTS": "2"},
}
QUESTIONS = [
    "How do I parse a JSON string into a Python object?", "How do I copy a whole directory tree?",
    "How do I compute the mean and median of a list of numbers?", "How do I keep a list sorted while inserting into it?",
    "How do I generate a cryptographically secure random token?", "How do I compare two sequences and show their differences?",
    "Which warehouse has the item with SKU-0007, and how many are in stock?", "How do I wrap a long paragraph to a fixed width?",
    "How do I schedule a function to run after a delay with sched?", "How do I create a dataclass with default values?",
    "How do I read rows from a CSV file?", "How do I start a thread and wait for it?", "How do I compute a SHA-256 hash?",
    "How do I create a temporary file?", "How do I count items with a Counter?", "How do I configure logging to a file?",
    "How do I encode bytes as base64?", "How do I generate a UUID?", "How do I glob for files matching a pattern?",
    "How do I format the current time as a string?",
]
TURNS = ["My name is Ada and I am planning a three-day trip to Lisbon.", "What should I see on the first day?",
         "And on the second day, something outside the city?", "Suggest a typical dish for dinner on day two.",
         "What was my name again, and which city am I visiting?", "Summarise the whole plan in three lines."]


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def gen_runner_uss(sampler: Sampler, model: str, t0: float, t1: float) -> list[tuple[float, float]]:
    out = []
    for s in sampler.slow:
        if t0 <= s["t"] <= t1:
            vals = [p["uss"] for p in s["procs"] if p["role"] == f"runner:{model}" and p["uss"]]
            if vals:
                out.append((round(s["t"] - t0, 1), round(vals[0] / 2**20, 1)))
    return out


def condition(name: str, extra: dict[str, str], source: Path, out: Path, settings: Any) -> dict[str, Any]:
    run = out / name
    (run / "data").mkdir(parents=True)
    for f in ("xodus.db", "xodus.db-wal", "xodus.db-shm"):
        if (source / "data" / f).exists():
            shutil.copy2(source / "data" / f, run / "data" / f)
    roots = [{"id": r.id, "path": str(r.path), "grants": r.grants} for r in settings.permissions.roots]
    roots.append({"id": "corpus", "path": str(CORPUS), "grants": ["fs.read"]})
    env = {**os.environ, "XODUS_DATA_DIR": str(run / "data"), "XODUS_LOG_DIR": str(run / "logs"),
           "XODUS_PERMISSIONS__ROOTS": json.dumps(roots)}
    backend = Backend(env, run / "data", run / "backend.log", settings.server.port)
    sampler = Sampler().start()
    log(f"{name}: {run_ps(START_OLLAMA, run, env=extra)}")
    try:
        backend.start()
        sampler.backend = backend.psutil
        with backend.client() as c:
            sse(c, "/api/chat", {"message": "Say hello."})  # load the model
            time.sleep(3)
            t0 = time.time()
            asks = [sse(c, "/api/knowledge/ask", {"question": q, "collections": ["gatef"]}) for q in QUESTIONS]
            t_asks = time.time()
            conv = c.post("/api/conversations", json={"title": "cache experiment"}).json()["id"]
            turns = [sse(c, "/api/chat", {"message": m, "conversation_id": conv}) for m in TURNS]
            t1 = time.time()
            stats = c.get("/api/knowledge/stats").json()
    finally:
        backend.stop()
        run_ps(STOP_OLLAMA, run)
        time.sleep(2)
        sampler.stop()
    model = settings.ollama.model
    series = gen_runner_uss(sampler, model, t0 - 2, t1)
    ttft = [a["client_ttft_ms"] for a in asks if a["client_ttft_ms"]]
    turn_ttft = [t["client_ttft_ms"] for t in turns if t["client_ttft_ms"]]
    return {"env": extra, "chunks_in_db": stats.get("chunks"),
            "asks_ok": sum(a["ok"] for a in asks), "turns_ok": sum(t["ok"] for t in turns),
            "runner_uss_start_mib": series[0][1] if series else None,
            "runner_uss_after_asks_mib": max((u for t, u in series if t <= t_asks - t0), default=None),
            "runner_uss_end_mib": series[-1][1] if series else None,
            "runner_uss_peak_mib": max((u for _, u in series), default=None),
            "ask_ttft_ms_median": statistics.median(ttft) if ttft else None,
            "ask_total_ms_median": statistics.median(a["client_total_ms"] for a in asks),
            "turn_ttft_ms": turn_ttft, "turn_ttft_ms_median": statistics.median(turn_ttft) if turn_ttft else None,
            "uss_series": series}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    source = Path(sys.argv[1])
    if port_busy(11434) or port_busy(8765):
        raise SystemExit("Ports 11434 and 8765 must be free: stop Ollama and the backend first.")
    require_quiet_machine()
    keep_awake()
    settings = load_settings()
    out = REPO_ROOT / "experiments" / "results" / f"gate-f-cache-{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True)
    results = {name: condition(name, extra, source, out, settings) for name, extra in CONDITIONS.items()}
    write_json(out / "results.json", {"environment": environment(settings), "source_run": str(source), "results": results})
    for name, r in results.items():
        log(f"{name}: " + json.dumps({k: v for k, v in r.items() if k != "uss_series"}))
    log(f"Results: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
