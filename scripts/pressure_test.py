"""Gate F live tests of the resource governor (experiments/gate-f/PROTOCOL.md section 8).

Run from backend/ with Ollama and the backend STOPPED:
    uv run python ..\\scripts\\pressure_test.py lowered     # P1 warn -> recover, P2 stop -> recover (lowered thresholds)
    uv run python ..\\scripts\\pressure_test.py ballast     # P3 real low-memory run (bounded ballast)

P1/P2 lower the USS thresholds through environment variables (stated in the results), so the real growth of XODUS's
memory when the model loads crosses them, and unloading the model brings it back below. P3 changes no threshold: a
separate ballast process takes physical memory until system available memory is just under the 1.5 GB stop floor,
never below 1.2 GB (user's bound, 29 Oct 2026), holds at most 45 s, and is then killed. Each ballast page carries unique
random bytes so Windows memory compression and page combining cannot hide the pressure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any

import httpx
import psutil

from xodus.config.settings import REPO_ROOT, load_settings
from xodus.experiments.harness import (GB, Backend, START_OLLAMA, STOP_OLLAMA, environment, keep_awake, port_busy, require_quiet_machine, run_ps,
                                       sse,
                                       write_json)

CORPUS = REPO_ROOT / "experiments" / "corpus" / "gate-f-50"
FLOOR_GB = 1.2  # never push system available memory below this
TARGET_GB = 1.35  # just under the 1.5 GB stop floor
HOLD_S = 45
BALLAST_CODE = r"""
import os, sys, time, psutil
target, floor, step = float(sys.argv[1]) * 1e9, float(sys.argv[2]) * 1e9, 256 * 2**20
block = bytearray(os.urandom(2**20))
held, n = [], 0
while psutil.virtual_memory().available > target:
    if psutil.virtual_memory().available - step < floor:
        break
    chunk = bytearray(step)
    for off in range(0, step, 2**20):
        chunk[off:off + 2**20] = block
    for off in range(0, step, 4096):  # every page unique: no compression or page combining
        n += 1
        chunk[off:off + 8] = n.to_bytes(8, "little")
    held.append(chunk)
    print(f"held {len(held) * step / 1e9:.2f} GB, available {psutil.virtual_memory().available / 1e9:.2f} GB", flush=True)
print("READY", flush=True)
end = time.time() + 600
while time.time() < end:  # the parent kills this process after the hold; it also lets go by itself if others push lower
    if psutil.virtual_memory().available < floor - 0.2e9:
        print("EMERGENCY_RELEASE", flush=True)
        break
    time.sleep(0.5)
"""


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


class Timeline(threading.Thread):
    """Polls /api/health every second: governor level, its state, and system available memory."""

    def __init__(self, backend: Backend) -> None:
        super().__init__(daemon=True)
        self.backend, self.rows, self.stop_flag = backend, [], threading.Event()

    def run(self) -> None:
        with self.backend.client(timeout=5) as c:
            t0 = time.time()
            while not self.stop_flag.is_set():
                try:
                    h = c.get("/api/health").json()
                    self.rows.append({"t": round(time.time() - t0, 1), "level": h["resources"]["level"],
                                      "stepping_down_in_s": h["resources"]["stepping_down_in_s"],
                                      "held": h["knowledge"]["ingestion"]["held"],
                                      "available_gb": round(psutil.virtual_memory().available / GB, 2)})
                except (httpx.HTTPError, KeyError, ValueError):
                    pass
                self.stop_flag.wait(1.0)

    def first(self, level: str) -> float | None:
        return next((r["t"] for r in self.rows if r["level"] == level), None)


def start(out: Any, name: str, thresholds: dict[str, str]) -> Backend:
    settings = load_settings()
    roots = [{"id": r.id, "path": str(r.path), "grants": r.grants} for r in settings.permissions.roots]
    roots.append({"id": "corpus", "path": str(CORPUS), "grants": ["fs.read"]})
    run_dir = out / name
    (run_dir / "data").mkdir(parents=True)
    env = {**os.environ, "XODUS_DATA_DIR": str(run_dir / "data"), "XODUS_LOG_DIR": str(run_dir / "logs"),
           "XODUS_PERMISSIONS__ROOTS": json.dumps(roots), **thresholds}
    backend = Backend(env, run_dir / "data", run_dir / "backend.log", settings.server.port)
    backend.start()
    return backend


def unload_model() -> None:
    model = load_settings().ollama.model
    httpx.post("http://127.0.0.1:11434/api/generate", json={"model": model, "keep_alive": 0}, trust_env=False, timeout=60)


def wait_level(timeline: Timeline, level: str, timeout: float) -> float | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if timeline.rows and timeline.rows[-1]["level"] == level:
            return timeline.rows[-1]["t"]
        time.sleep(0.5)
    return None


def try_all(c: httpx.Client, ingest: bool = True) -> dict[str, Any]:
    """What is allowed right now: an optional-process plugin, a task, a model switch, an ingestion request, and chat
    last (chat loads the model, which moves the lowered-threshold tests' level again)."""
    out = {}
    r = c.post("/api/capabilities/text.stats/invoke", json={"args": {"text": "one two three"}})
    out["optional_plugin"] = {"status": r.status_code, "body": r.json()}
    r = c.post("/api/tasks", json={"goal": "List the files in the sandbox folder."})
    out["task"] = {"status": r.status_code, "body": r.json()}
    if r.status_code == 202:
        c.post(f"/api/tasks/{r.json()['task_id']}/cancel")
    r = c.post("/api/models/switch", json={"profile": "nonexistent-profile"})
    out["model_switch"] = {"status": r.status_code, "code": (r.json().get("error") or {}).get("code")}
    if ingest:
        r = c.post("/api/knowledge/ingest", json={"collection": "pressure", "root": "corpus", "path": "json.txt"})
        out["ingest"] = {"status": r.status_code, "body": r.json()}
    chat = sse(c, "/api/chat", {"message": "Say hello in five words."})
    out["chat"] = {"ok": chat["ok"], "error": chat["error"]}
    return out


def cancel_pending(c: httpx.Client) -> int:
    jobs = [j for j in c.get("/api/knowledge/jobs", params={"limit": 200}).json() if j["status"] in ("queued", "running")]
    for j in jobs:
        c.post(f"/api/knowledge/jobs/{j['id']}/cancel")
    return len(jobs)


def wait_jobs(c: httpx.Client, timeout: float) -> dict[str, int]:
    deadline = time.time() + timeout
    while True:
        jobs = c.get("/api/knowledge/jobs", params={"limit": 200}).json()
        counts: dict[str, int] = {}
        for j in jobs:
            counts[j["status"]] = counts.get(j["status"], 0) + 1
        if (not counts.get("queued") and not counts.get("running")) or time.time() > deadline:
            return counts
        time.sleep(2)


def integrity(data_dir: Any) -> list[str]:
    import sqlite3

    import sqlite_vec

    conn = sqlite3.connect(f"file:{data_dir / 'xodus.db'}?mode=ro", uri=True, timeout=30)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    try:
        return [r[0] for r in conn.execute("PRAGMA integrity_check")]
    finally:
        conn.close()


def lowered(out: Any) -> dict[str, Any]:
    results = {}
    for name, thresholds in (("P1_warn", {"XODUS_THRESHOLDS__WARN_USS_GB": "0.5", "XODUS_THRESHOLDS__STOP_USS_GB": "50"}),
                             ("P2_stop", {"XODUS_THRESHOLDS__WARN_USS_GB": "0.4", "XODUS_THRESHOLDS__STOP_USS_GB": "0.5"})):
        log(f"{name}: thresholds {thresholds}")
        log(run_ps(START_OLLAMA, out))
        unload_model()
        backend = start(out, name, thresholds)
        timeline = Timeline(backend)
        timeline.start()
        try:
            with backend.client() as c:
                c.post("/api/knowledge/collections", json={"name": "pressure"})
                time.sleep(3)
                before = timeline.rows[-1] if timeline.rows else None
                sse(c, "/api/chat", {"message": "Say hello."})  # loads the model: XODUS USS rises past the lowered level
                reached = wait_level(timeline, "warn" if name == "P1_warn" else "stop", 20)
                at_level = try_all(c)
                resources = c.get("/api/resources").json()["thresholds"]
                cancelled = cancel_pending(c)  # a released ingestion would load the embedder and cross the lowered level again
                unload_model()  # USS falls back below the warning level; recovery needs 30 s of that
                recovered = wait_level(timeline, "ok", 90)
                after = try_all(c, ingest=False)
                events = [r for r in c.get("/api/audit", params={"limit": 200}).json()["records"] if r["event"].startswith(
                    ("resource_", "ingestion_held", "ingestion_released"))]
        finally:
            timeline.stop_flag.set()
            backend.stop()
            run_ps(STOP_OLLAMA, out)
        results[name] = {"thresholds_env": thresholds, "before": before, "reached_level_at_s": reached,
                         "held_jobs_cancelled_before_recovery": cancelled,
                         "at_level": at_level, "checks_at_level": resources["checks"], "recovered_to_ok_at_s": recovered,
                         "after_recovery": after, "audit_events": list(reversed(events)), "timeline": timeline.rows}
        log(f"{name}: reached at {reached} s, recovered at {recovered} s")
    return results


def ballast(out: Any) -> dict[str, Any]:
    log(run_ps(START_OLLAMA, out))
    backend = start(out, "P3_ballast", {})
    timeline = Timeline(backend)
    timeline.start()
    proc = None
    try:
        with backend.client() as c:
            sse(c, "/api/chat", {"message": "Say hello."})
            c.post("/api/knowledge/collections", json={"name": "pressure"})
            c.post("/api/knowledge/ingest", json={"collection": "pressure", "root": "corpus", "path": ".", "recursive": False})
            deadline = time.time() + 300  # let ingestion get going, so the stop level meets a running background job
            while time.time() < deadline and c.get("/api/knowledge/stats").json().get("chunks", 0) < 15:
                time.sleep(2)
            chunks_before = c.get("/api/knowledge/stats").json().get("chunks")
            before = timeline.rows[-1]
            log(f"available before ballast: {before['available_gb']} GB; target {TARGET_GB} GB, floor {FLOOR_GB} GB")
            proc = subprocess.Popen([sys.executable, "-c", BALLAST_CODE, str(TARGET_GB), str(FLOOR_GB)],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            lines = []
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.append(line.strip())
                if line.startswith("READY"):
                    break
            t_ready = time.time()
            reached = wait_level(timeline, "stop", 15)
            at_stop = try_all(c) if reached is not None else None
            held_at_stop = c.get("/api/health").json()["knowledge"]["ingestion"]
            min_available = min(r["available_gb"] for r in timeline.rows[-20:])
            time.sleep(max(0.0, HOLD_S - (time.time() - t_ready)))
    finally:
        if proc is not None:
            proc.kill()
            proc.wait(timeout=30)
            log("ballast released")
    try:
        with backend.client() as c:
            recovered = wait_level(timeline, "ok", 120)
            after = try_all(c, ingest=False)
            jobs = wait_jobs(c, 1200)  # the held ingestion is released and must finish from its checkpoints
            stats = c.get("/api/knowledge/stats").json()
            events = [r for r in c.get("/api/audit", params={"limit": 300}).json()["records"]
                      if r["event"].startswith(("resource_", "ingestion_"))]
    finally:
        timeline.stop_flag.set()
        backend.stop()
        run_ps(STOP_OLLAMA, out)
    return {"P3_ballast": {"floor_gb": FLOOR_GB, "target_gb": TARGET_GB, "hold_s": HOLD_S, "before": before,
                           "ballast_log": lines[-4:], "reached_stop_at_s": reached, "min_available_gb": min_available,
                           "chunks_before_ballast": chunks_before, "ingestion_at_stop": held_at_stop,
                           "jobs_after": jobs, "chunks_after": stats.get("chunks"), "documents_after": stats.get("documents"),
                           "integrity_check": integrity(backend.data_dir),
                           "at_stop": at_stop, "recovered_to_ok_at_s": recovered, "after_recovery": after,
                           "audit_events": list(reversed(events)), "timeline": timeline.rows}}


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("lowered", "ballast"):
        print(__doc__)
        return 2
    if port_busy(11434) or port_busy(8765):
        raise SystemExit("Ports 11434 and 8765 must be free: stop Ollama and the backend first.")
    require_quiet_machine()
    keep_awake()
    out = REPO_ROOT / "experiments" / "results" / f"gate-f-pressure-{mode}-{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True)
    env = environment(load_settings())
    results = lowered(out) if mode == "lowered" else ballast(out)
    write_json(out / "results.json", {"environment": env, "mode": mode, "results": results})
    log(f"Results: {out}")
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk not in ("timeline", "audit_events", "checks_at_level")}
                      for k, v in results.items()}, indent=1, default=str)[:6000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
