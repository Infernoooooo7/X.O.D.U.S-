"""Measure the memory of the sample extensions as XODUS counts them (optional tier), against a running backend.

Run from backend/ with the backend running on 127.0.0.1:8765:  uv run python ..\\scripts\\measure_extensions.py
Starts each extension by calling one of its capabilities, then samples /api/resources five times (1 s apart) and
writes experiments/results/gate-e/extension_footprint.json. Only measures; changes nothing but starting the two
extension processes, which stop by themselves after the idle timeout.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8765"
OUT = Path(__file__).resolve().parents[1] / "experiments" / "results" / "gate-e"
CALLS = {"text.stats": {"text": "One two three. Four five."}, "calc.evaluate": {"expression": "(17.5 * 4) / 3 + sqrt(2)"}}


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=30, trust_env=False)
    started = {}
    for cap, args in CALLS.items():
        t0 = time.perf_counter()
        r = client.post(f"/api/capabilities/{cap}/invoke", json={"args": args})
        started[cap] = {"status": r.status_code, "first_call_ms": round((time.perf_counter() - t0) * 1000, 1)}
        t0 = time.perf_counter()
        client.post(f"/api/capabilities/{cap}/invoke", json={"args": args})
        started[cap]["warm_call_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    samples = []
    for _ in range(5):
        view = client.get("/api/resources").json()
        by_owner: dict[str, dict[str, float]] = {}
        for p in view["processes"]:
            if p["tier"] != "optional":
                continue
            row = by_owner.setdefault(p["owner"], {"processes": 0, "uss_mib": 0.0, "working_set_mib": 0.0, "private_commit_mib": 0.0})
            row["processes"] += 1
            for key in ("uss_mib", "working_set_mib", "private_commit_mib"):
                row[key] += p[key] or 0
        optional_line = next(line for line in view["lines"] if line["line"] == "optional")
        samples.append({"by_owner": by_owner, "optional_line_uss_mib": optional_line["uss_mib"], "total_uss_mib": view["totals"]["uss_mib"],
                        "sample_ms": view["measurement"]["sample_ms"]})
        time.sleep(1)
    owners = sorted({o for s in samples for o in s["by_owner"]})
    summary = {o: {"processes": samples[-1]["by_owner"].get(o, {}).get("processes"),
                   "uss_mib_median": round(statistics.median(s["by_owner"][o]["uss_mib"] for s in samples if o in s["by_owner"]), 1),
                   "working_set_mib_median": round(statistics.median(s["by_owner"][o]["working_set_mib"] for s in samples if o in s["by_owner"]), 1),
                   "private_commit_mib_median": round(statistics.median(s["by_owner"][o]["private_commit_mib"] for s in samples if o in s["by_owner"]), 1)}
               for o in owners}
    result = {"what": "sample extensions, optional tier, as counted by /api/resources (5 samples, 1 s apart)",
              "calls": started, "summary": summary, "samples": samples}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "extension_footprint.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps({"calls": started, "summary": summary}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
