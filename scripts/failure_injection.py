"""Gate F failure injection on the real stack (experiments/gate-f/PROTOCOL.md section 9, and section 10).

Run from backend/ with Ollama and the backend STOPPED:   uv run python ..\\scripts\\failure_injection.py
Uses an isolated data, log and plugins folder in experiments/results/gate-f-faults-<timestamp>/ (the user's data/ and
plugins/ are not touched). After every injected failure: the audit chain is verified, the database gets
PRAGMA integrity_check, and the backend must still answer. Results: results.json and summary.md.

F3 starts a SECOND Ollama server on 127.0.0.1:11435 (loopback only, same installed models, no download) holding
qwen3:4b-instruct with a 16K context, to take VRAM away from XODUS's Ollama; it is stopped by this script.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import psutil
import sqlite_vec

from xodus.config.settings import REPO_ROOT, load_settings
from xodus.experiments.harness import (Backend, START_OLLAMA, STOP_OLLAMA, environment, keep_awake, mib, other_activity, port_busy, require_quiet_machine, run_ps, sse,
                                       write_json)
from xodus.resources.sampler import process_tree

FIXTURE = REPO_ROOT / "backend" / "tests" / "fixtures" / "plugin_main.py"
OLLAMA_EXE = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Ollama" / "ollama.exe"
HOG_PORT = 11435
HOG_MODEL = "qwen3:4b-instruct-2507-q4_K_M"


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def fixture_plugin(plugins: Path, pid: str, mode: str, timeout_s: float = 10) -> None:
    folder = plugins / pid.replace(".", "_")
    folder.mkdir(parents=True)
    shutil.copy(FIXTURE, folder / "main.py")
    (folder / "manifest.json").write_text(json.dumps({
        "id": pid, "name": f"Fault fixture ({mode})", "version": "1.0.0", "description": f"Gate F fault injection: {mode}.",
        "input_schema": {"type": "object", "properties": {"seconds": {"type": "number"}}, "additionalProperties": False},
        "output_schema": {"type": "object"}, "permissions": ["net.none"], "execution_mode": "subprocess",
        "process_tier": "optional", "timeout_s": timeout_s, "max_output_bytes": 20000, "requires_approval": "never",
        "side_effects": "none", "entry": {"command": ["python", "main.py", mode]}}), encoding="utf-8")


class Faults:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.out = REPO_ROOT / "experiments" / "results" / f"gate-f-faults-{datetime.now():%Y%m%d-%H%M%S}"
        self.data = self.out / "data"
        self.inject = self.out / "inject"
        plugins = self.out / "plugins"
        for d in (self.data, self.inject, plugins):
            d.mkdir(parents=True)
        for sample in ("text_stats", "calc_mcp"):  # the shipped samples, copied
            shutil.copytree(REPO_ROOT / "plugins" / sample, plugins / sample)
        fixture_plugin(plugins, "fault.slow", "slow", timeout_s=2)
        fixture_plugin(plugins, "fault.crash", "crash")
        (plugins / "broken_json").mkdir()
        (plugins / "broken_json" / "manifest.json").write_text("{ this is not json", encoding="utf-8")
        (plugins / "missing_fields").mkdir()
        (plugins / "missing_fields" / "manifest.json").write_text(json.dumps({"id": "fault.incomplete", "name": "x"}), encoding="utf-8")
        roots = [{"id": r.id, "path": str(r.path), "grants": r.grants} for r in self.settings.permissions.roots]
        roots.append({"id": "inject", "path": str(self.inject), "grants": ["fs.read"]})
        env = {**os.environ, "XODUS_DATA_DIR": str(self.data), "XODUS_LOG_DIR": str(self.out / "logs"),
               "XODUS_PLUGINS__DIR": str(plugins), "XODUS_PERMISSIONS__ROOTS": json.dumps(roots)}
        self.backend = Backend(env, self.data, self.out / "backend.log", self.settings.server.port)
        self.results: dict[str, Any] = {}

    # ------------------------------------------------------------------ checks after every fault

    def integrity(self, c: httpx.Client) -> dict[str, Any]:
        audit = c.get("/api/audit/verify").json()
        conn = sqlite3.connect(f"file:{self.data / 'xodus.db'}?mode=ro", uri=True, timeout=30)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        try:
            check = conn.execute("PRAGMA integrity_check").fetchall()
        finally:
            conn.close()
        health = c.get("/api/health")
        return {"audit_ok": audit.get("ok"), "audit_records": audit.get("records"),
                "integrity_check": [r[0] for r in check], "backend_answers": health.status_code == 200}

    def fault(self, name: str, fn: Any, c: httpx.Client) -> None:
        deadline = time.time() + 60  # a level left by the previous fault must not decide this one's outcome
        while time.time() < deadline and c.get("/api/health").json()["resources"]["level"] != "ok":
            time.sleep(1)
        level = c.get("/api/health").json()["resources"]["level"]
        log(f"{name} (governor level {level}) ...")
        try:
            detail = fn(c)
            error = None
        except Exception as exc:
            detail, error = {}, repr(exc)
            log(f"{name} harness error: {error}")
        self.results[name] = {"level_before": level, "detail": detail, "harness_error": error, "after": self.integrity(c),
                              "other_activity": other_activity()}
        log(f"{name}: {json.dumps(self.results[name], default=str)[:400]}")

    # ------------------------------------------------------------------ faults

    def f1_ollama_down(self, c: httpx.Client) -> dict[str, Any]:
        run_ps(STOP_OLLAMA, self.out)
        time.sleep(1)
        during = sse(c, "/api/chat", {"message": "Say hello."})
        health = c.get("/api/health").json()
        run_ps(START_OLLAMA, self.out)
        time.sleep(1)
        after = sse(c, "/api/chat", {"message": "Say hello."})
        return {"chat_while_down": {"ok": during["ok"], "error": during["error"]},
                "health_while_down": {"backend": health["backend"]["status"], "ollama": health["ollama"]},
                "chat_after_restart_ok": after["ok"]}

    def f2_database_locked(self, c: httpx.Client) -> dict[str, Any]:
        holder = sqlite3.connect(self.data / "xodus.db", timeout=1, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")  # another process-like connection holds the write lock
        t0 = time.perf_counter()
        try:
            write = c.post("/api/conversations", json={"title": "written while locked"}, timeout=60)
            write_ms = round((time.perf_counter() - t0) * 1000)
            read = c.get("/api/conversations")
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        after = c.post("/api/conversations", json={"title": "written after release"})
        return {"write_while_locked": {"status": write.status_code, "body": write.json() if write.headers.get(
                    "content-type", "").startswith("application/json") else write.text[:300], "ms": write_ms},
                "read_while_locked_status": read.status_code, "write_after_release_status": after.status_code}

    def f3_out_of_vram(self, c: httpx.Client) -> dict[str, Any]:
        def ps() -> list[dict[str, Any]]:
            return httpx.get("http://127.0.0.1:11434/api/ps", trust_env=False, timeout=5).json().get("models", [])

        def vram() -> dict[str, Any]:
            out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.free", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True).stdout.strip().split(",")
            return {"used_mib": int(out[0]), "free_mib": int(out[1])}

        # XODUS's generation model out of VRAM first, so it has to load while another process holds the GPU.
        httpx.post("http://127.0.0.1:11434/api/generate", json={"model": self.settings.ollama.model, "keep_alive": 0},
                   trust_env=False, timeout=60)
        time.sleep(3)
        env = {**os.environ, "OLLAMA_HOST": f"127.0.0.1:{HOG_PORT}", "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1",
               "OLLAMA_NO_CLOUD": "1", "OLLAMA_KEEP_ALIVE": "-1", "OLLAMA_FLASH_ATTENTION": "1"}
        hog_log = open(self.out / "hog-ollama.log", "wb")
        hog = subprocess.Popen([str(OLLAMA_EXE), "serve"], env=env, stdout=hog_log, stderr=subprocess.STDOUT)
        try:
            deadline = time.time() + 30
            while not port_busy(HOG_PORT) and time.time() < deadline:
                time.sleep(0.3)
            hog_vram_before = vram()
            httpx.post(f"http://127.0.0.1:{HOG_PORT}/api/generate", json={"model": HOG_MODEL, "prompt": "hi", "stream": False,
                       "keep_alive": -1, "options": {"num_ctx": 16384, "num_predict": 1}}, trust_env=False, timeout=180)
            hog_vram = vram()
            chat = sse(c, "/api/chat", {"message": "Name three primary colours."})
            loaded = ps()
            resources = c.get("/api/resources").json()
            levels = {ch["metric"]: (ch["value_gb"], ch["level"]) for ch in resources["thresholds"]["checks"]}
            vram_idle_hog = vram()
            # The same again with the competitor busy: Windows can move an idle process's GPU memory out, not a busy one's.
            httpx.post("http://127.0.0.1:11434/api/generate", json={"model": self.settings.ollama.model, "keep_alive": 0},
                       trust_env=False, timeout=60)
            time.sleep(3)
            stop_hog = threading.Event()
            hog_requests = {"ok": 0, "failed": 0}

            def keep_busy() -> None:
                while not stop_hog.is_set():
                    try:
                        r = httpx.post(f"http://127.0.0.1:{HOG_PORT}/api/generate", json={
                            "model": HOG_MODEL, "prompt": "Write a long essay about rivers.", "stream": False, "keep_alive": -1,
                            "options": {"num_ctx": 16384, "num_predict": 200}}, trust_env=False, timeout=180)
                        hog_requests["ok" if r.status_code == 200 else "failed"] += 1
                    except httpx.HTTPError:
                        hog_requests["failed"] += 1

            busy = threading.Thread(target=keep_busy, daemon=True)
            busy.start()
            time.sleep(4)
            vram_busy_before = vram()
            active = sse(c, "/api/chat", {"message": "Name three primary colours."})
            active_loaded = ps()
            active_res = c.get("/api/resources").json()
            active_vram = vram()
            stop_hog.set()
            busy.join(timeout=60)
            return {"vram_before_hog": hog_vram_before, "vram_with_hog": hog_vram, "vram_with_xodus_idle_hog": vram_idle_hog,
                    "active_competitor": {
                        "vram_before_chat": vram_busy_before, "vram_after_chat": active_vram, "competitor_requests": hog_requests,
                        "xodus_chat": {"ok": active["ok"], "error": active["error"], "ttft_ms": active["client_ttft_ms"],
                                       "total_ms": active["client_total_ms"],
                                       "tokens_per_s": ((active["done"] or {}).get("usage") or {}).get("tokens_per_s")},
                        "xodus_model_placement": [{"name": m["name"], "size_mib": mib(m.get("size")),
                                                   "size_vram_mib": mib(m.get("size_vram"))} for m in active_loaded],
                        "thresholds": {ch["metric"]: (ch["value_gb"], ch["level"]) for ch in active_res["thresholds"]["checks"]},
                        "gpu_xodus": active_res.get("gpu_xodus")},
                    "xodus_chat": {"ok": chat["ok"], "error": chat["error"], "total_ms": chat["client_total_ms"],
                                   "tokens_per_s": ((chat["done"] or {}).get("usage") or {}).get("tokens_per_s")},
                    "xodus_model_placement": [{"name": m["name"], "size_mib": mib(m.get("size")),
                                               "size_vram_mib": mib(m.get("size_vram"))} for m in loaded],
                    "thresholds": levels, "governor": resources["thresholds"]["governor"],
                    "gpu_xodus": resources.get("gpu_xodus")}
        finally:
            for p in reversed(process_tree(psutil.Process(hog.pid))):
                try:
                    p.kill()
                except psutil.Error:
                    pass
            hog.wait(timeout=20)
            hog_log.close()
            time.sleep(2)
            # back to a clean state: reload XODUS's model fully on the GPU
            httpx.post("http://127.0.0.1:11434/api/generate", json={"model": self.settings.ollama.model, "keep_alive": 0},
                       trust_env=False, timeout=60)
            time.sleep(2)

    def f3_recovery(self, c: httpx.Client) -> dict[str, Any]:
        chat = sse(c, "/api/chat", {"message": "Name three primary colours."})
        loaded = httpx.get("http://127.0.0.1:11434/api/ps", trust_env=False, timeout=5).json().get("models", [])
        return {"chat_ok": chat["ok"], "placement": [{"name": m["name"], "size_mib": mib(m.get("size")),
                                                     "size_vram_mib": mib(m.get("size_vram"))} for m in loaded]}

    def f4_oversized(self, c: httpx.Client) -> dict[str, Any]:
        sys.path.insert(0, str(REPO_ROOT / "backend"))
        from tests.kb_helpers import write_pdf

        with open(self.inject / "big.txt", "wb") as fh:
            fh.write(b"a" * (51 * 2**20))
        write_pdf(self.inject / "pages.pdf", [f"page {i}" for i in range(501)])
        c.post("/api/knowledge/collections", json={"name": "faults"})
        big = c.post("/api/knowledge/ingest", json={"collection": "faults", "root": "inject", "path": "big.txt"})
        pdf = c.post("/api/knowledge/ingest", json={"collection": "faults", "root": "inject", "path": "pages.pdf"})
        deadline = time.time() + 300
        jobs: list[dict[str, Any]] = []
        while time.time() < deadline:
            jobs = c.get("/api/knowledge/jobs", params={"limit": 50}).json()
            if all(j["status"] in ("completed", "failed", "cancelled") for j in jobs):
                break
            time.sleep(1)
        docs = c.get("/api/knowledge/documents", params={"limit": 50}).json()
        return {"big_txt": {"status": big.status_code, "body": big.json()},
                "pages_pdf_request": {"status": pdf.status_code, "jobs": len(pdf.json().get("jobs", []))},
                "pages_pdf_jobs": [{"status": j["status"], "error": j.get("error")} for j in jobs],
                "documents_stored": len(docs.get("items", docs) if isinstance(docs, dict) else docs)}

    def f5_malformed_manifests(self, c: httpx.Client) -> dict[str, Any]:
        rejected = c.get("/api/capabilities/rejected").json()
        return {"rejected": [{"ref": r.get("ref"), "reasons": r.get("reasons")} for r in rejected]}

    def f6_timeout(self, c: httpx.Client) -> dict[str, Any]:
        t0 = time.perf_counter()
        r = c.post("/api/capabilities/fault.slow/invoke", json={"args": {"seconds": 30}})
        ms = round((time.perf_counter() - t0) * 1000)
        left = [p for p in c.get("/api/processes").json()["supervised"] if p["owner"] == "fault.slow"]
        return {"status": r.status_code, "body": r.json(), "ms": ms, "processes_left": left}

    def f7_cancel_mid_stream(self, c: httpx.Client) -> dict[str, Any]:
        cut = sse(c, "/api/chat", {"message": "Write a long story about a lighthouse keeper, at least 600 words."},
                  stop_after_tokens=15)
        time.sleep(1)
        follow = sse(c, "/api/chat", {"message": "Say hello."})
        task = c.post("/api/tasks", json={"goal": "List the files in the sandbox folder and describe each one in detail."}).json()
        time.sleep(2.5)
        cancel = c.post(f"/api/tasks/{task['task_id']}/cancel")
        deadline = time.time() + 30
        status = None
        while time.time() < deadline:
            status = c.get(f"/api/tasks/{task['task_id']}").json()["status"]
            if status in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.3)
        supervised = c.get("/api/processes").json()["supervised"]
        return {"chat_disconnected_after_tokens": cut["tokens"], "next_chat_ok": follow["ok"],
                "next_chat_queue_wait_ms": ((follow["done"] or {}).get("timing") or {}).get("queue_wait_ms"),
                "task_cancel_status": cancel.status_code, "task_final_status": status,
                "supervised_after": [{k: p[k] for k in ("pid", "owner", "tier", "alive")} for p in supervised]}

    def f8_plugin_crash(self, c: httpx.Client) -> dict[str, Any]:
        first = c.post("/api/capabilities/fault.crash/invoke", json={"args": {}})
        second = c.post("/api/capabilities/fault.crash/invoke", json={"args": {}})
        starts = [r for r in c.get("/api/audit", params={"limit": 100, "event": "process_started"}).json()["records"]
                  if r.get("owner") == "fault.crash"]
        return {"first": {"status": first.status_code, "body": first.json()},
                "second": {"status": second.status_code, "body": second.json()},
                "process_starts_for_plugin": len(starts), "backend_still_up": c.get("/api/health").status_code == 200}

    def security_probes(self, c: httpx.Client) -> dict[str, Any]:
        lan = sorted({a.address for addrs in psutil.net_if_addrs().values() for a in addrs
                      if a.family == socket.AF_INET and not a.address.startswith("127.") and not a.address.startswith("169.254.")})
        probes = {}
        for ip in lan:
            for port in (self.settings.server.port, 11434):
                s = socket.socket()
                s.settimeout(1.5)
                try:
                    s.connect((ip, port))
                    probes[f"{ip}:{port}"] = "CONNECTED (exposed!)"
                except OSError as exc:
                    probes[f"{ip}:{port}"] = f"not reachable ({type(exc).__name__})"
                finally:
                    s.close()
        no_token = httpx.get(f"{self.backend.base}/api/health", trust_env=False)
        foreign_host = c.get("/api/health", headers={"Host": "attacker.example"})
        cross = c.post("/api/ingestion/pause", json={}, headers={"Origin": "http://evil.example"})
        listening = sorted({f"{conn.laddr.ip}:{conn.laddr.port}" for conn in psutil.net_connections(kind="tcp")
                            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port in
                            (self.settings.server.port, 11434)})
        return {"non_loopback_interfaces": probes, "listening_addresses": listening, "no_token_status": no_token.status_code,
                "foreign_host_status": foreign_host.status_code, "cross_origin_post_status": cross.status_code}

    # ------------------------------------------------------------------ run

    def run(self) -> int:
        require_quiet_machine()
        keep_awake()
        if port_busy(11434) or port_busy(self.settings.server.port) or port_busy(HOG_PORT):
            raise SystemExit("Ports 11434, 11435 and 8765 must be free: stop Ollama and the backend first.")
        env = environment(self.settings)
        log(run_ps(START_OLLAMA, self.out))
        self.backend.start()
        try:
            with self.backend.client(timeout=120) as c:
                sse(c, "/api/chat", {"message": "Say hello."})  # warm
                for name, fn in [("F1_ollama_down", self.f1_ollama_down), ("F2_database_locked", self.f2_database_locked),
                                 ("F3_out_of_vram", self.f3_out_of_vram), ("F3b_recovery_after_vram_freed", self.f3_recovery),
                                 ("F4_oversized_file", self.f4_oversized), ("F5_malformed_manifests", self.f5_malformed_manifests),
                                 ("F6_timeout", self.f6_timeout), ("F7_cancel_mid_stream", self.f7_cancel_mid_stream),
                                 ("F8_plugin_crash", self.f8_plugin_crash), ("S_security_probes", self.security_probes)]:
                    self.fault(name, fn, c)
                backend_pid = self.backend.proc.pid if self.backend.proc else None
        finally:
            tree = [p.pid for p in process_tree(self.backend.psutil)] if self.backend.proc and self.backend.proc.poll() is None else []
            self.backend.stop()
            run_ps(STOP_OLLAMA, self.out)
        time.sleep(3)
        orphans = [pid for pid in tree if psutil.pid_exists(pid)]
        self.results["after_shutdown"] = {"backend_pid": backend_pid, "processes_in_tree": len(tree), "still_running": orphans}
        write_json(self.out / "results.json", {"environment": env, "results": self.results})
        lines = [f"# Gate F failure injection {env['timestamp']}", "", "| Fault | Harness error | Audit chain | integrity_check | "
                 "Backend answers |", "|---|---|---|---|---|"]
        for name, r in self.results.items():
            if "after" in r:
                a = r["after"]
                lines.append(f"| {name} | {r['harness_error'] or ''} | {a['audit_ok']} | {a['integrity_check']} | {a['backend_answers']} |")
        lines += ["", "```json", json.dumps(self.results, indent=1, default=str), "```"]
        (self.out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
        log(f"Results: {self.out}")
        return 0


if __name__ == "__main__":
    sys.exit(Faults().run())
