"""Resource threshold levels (master prompt 3.7) evaluated against a sampler snapshot, for display.

This module only classifies. Nothing acts on the result automatically yet: pausing ingestion, stopping idle optional
processes and refusing heavy work at these levels is Gate F. The resource view says so next to the levels.
"""

from __future__ import annotations

from typing import Any

from xodus.config.settings import ThresholdSettings

GB = 10**9
ORDER = {"ok": 0, "warn": 1, "stop": 2, "unknown": -1}


def _level(value: float | None, warn: float | None, stop: float, *, below: bool = False) -> str:
    if value is None:
        return "unknown"
    if below:  # available memory: lower is worse
        return "stop" if value < stop else "ok"
    if value >= stop:
        return "stop"
    return "warn" if warn is not None and value >= warn else "ok"


def evaluate(snapshot: dict[str, Any], t: ThresholdSettings) -> dict[str, Any]:
    uss = snapshot["totals"]["uss"] if snapshot.get("uss_complete") else None
    vram = (snapshot.get("device_vram") or {}).get("used")
    available = snapshot["system_memory"]["available"]
    checks = [
        {"metric": "xodus_uss", "label": "XODUS-related USS (core + optional tiers)", "value_gb": uss and round(uss / GB, 2),
         "warn_gb": t.warn_uss_gb, "stop_gb": t.stop_uss_gb, "level": _level(uss and uss / GB, t.warn_uss_gb, t.stop_uss_gb),
         "note": None if uss is not None else "USS could not be read for every process; summing an incomplete set would understate it."},
        {"metric": "device_vram", "label": "Device VRAM in use (NVML, all applications)", "value_gb": vram and round(vram / GB, 2),
         "warn_gb": t.warn_vram_gb, "stop_gb": t.stop_vram_gb, "level": _level(vram and vram / GB, t.warn_vram_gb, t.stop_vram_gb),
         "note": "Device-wide: includes the desktop and other apps. Whether to measure against an idle baseline is an open decision."},
        {"metric": "system_available", "label": "System memory available", "value_gb": round(available / GB, 2),
         "warn_gb": None, "stop_gb": t.stop_available_gb, "level": _level(available / GB, None, t.stop_available_gb, below=True),
         "note": None},
    ]
    worst = max((c["level"] for c in checks), key=lambda lv: ORDER[lv])
    return {"level": worst, "checks": checks, "enforced": False,
            "enforcement_note": "Levels are evaluated and shown only. Automatic throttling at these levels is not implemented "
                                "yet (Gate F); use Pause ingestion and Stop process below to act by hand."}
