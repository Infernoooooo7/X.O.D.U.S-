"""Resource threshold levels (master prompt 3.7) evaluated against a sampler snapshot.

This module only classifies; the resource governor (governor.py) acts on the result. What each check compares, as
decided by the user on 29 Oct 2026 (Gate F):

- RAM: the summed USS of every XODUS-related process (the brief's compliance metric). Unknown when USS could not be read
  for every process: summing an incomplete set would understate it. Their shared GPU memory (system RAM used on the
  GPU's behalf) is reported next to it but not added: in the Gate F dry run it stayed at 634 MiB while the model was
  fully on the GPU, and USS + shared exceeded the system-wide increase over the idle baseline by about 300 MiB in every
  steady window, so adding it counts memory twice (protocol amendment 2, 29 Oct 2026).
- VRAM: XODUS's own VRAM, the dedicated GPU memory of XODUS-related processes from the Windows "GPU Process Memory"
  counters. Other applications' GPU use is not counted. If the counters cannot be read the check falls back to
  device-wide use (NVML, all applications), which can only overstate XODUS's share, and says so.
- System available memory: device-independent floor for the whole machine.
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


def _gb(value: int | None) -> float | None:
    return None if value is None else round(value / GB, 2)


def xodus_vram(snapshot: dict[str, Any]) -> tuple[int | None, str]:
    """XODUS's own VRAM in bytes and the basis it was measured on."""
    if not (snapshot.get("errors") or {}).get("gpu_counters"):
        return snapshot["totals"].get("gpu_dedicated_xodus") or 0, "xodus_processes"
    device = (snapshot.get("device_vram") or {}).get("used")
    return device, "device_fallback" if device is not None else "unavailable"


def evaluate(snapshot: dict[str, Any], t: ThresholdSettings, governor: dict[str, Any] | None = None) -> dict[str, Any]:
    totals = snapshot["totals"]
    uss = totals["uss"] if snapshot.get("uss_complete") else None
    shared = totals.get("gpu_shared_xodus") or 0
    ram = uss
    vram, basis = xodus_vram(snapshot)
    available = snapshot["system_memory"]["available"]
    vram_note = {
        "xodus_processes": "Dedicated GPU memory of XODUS's own processes (Windows GPU counters). Other applications' GPU "
                           "use is not counted.",
        "device_fallback": "Per-process GPU counters unavailable: device-wide use (all applications) is shown instead, "
                           "which overstates XODUS's share.",
        "unavailable": "Neither the per-process GPU counters nor NVML could be read.",
    }[basis]
    checks = [
        {"metric": "xodus_uss", "label": "XODUS-related USS (core and optional tiers)",
         "value_gb": _gb(ram), "warn_gb": t.warn_uss_gb, "stop_gb": t.stop_uss_gb,
         "level": _level(ram and ram / GB, t.warn_uss_gb, t.stop_uss_gb),
         "uss_gb": _gb(uss), "shared_gpu_gb": _gb(shared),
         "note": (f"Also {_gb(shared)} GB of shared GPU memory (system RAM used by the GPU driver), shown apart and not "
                  "added: in the Gate F measurements, adding it exceeded the system-wide increase in memory use."
                  if uss is not None else
                  "USS could not be read for every process; summing an incomplete set would understate it.")},
        {"metric": "xodus_vram", "label": "XODUS VRAM (dedicated GPU memory of XODUS processes)", "value_gb": _gb(vram),
         "warn_gb": t.warn_vram_gb, "stop_gb": t.stop_vram_gb, "level": _level(vram and vram / GB, t.warn_vram_gb, t.stop_vram_gb),
         "basis": basis, "device_used_gb": _gb((snapshot.get("device_vram") or {}).get("used")), "note": vram_note},
        {"metric": "system_available", "label": "System memory available", "value_gb": _gb(available),
         "warn_gb": None, "stop_gb": t.stop_available_gb, "level": _level(available / GB, None, t.stop_available_gb, below=True),
         "note": None},
    ]
    worst = max((c["level"] for c in checks), key=lambda lv: ORDER[lv])
    enforced = governor is not None and governor.get("enabled", False)
    if enforced:
        note = (f"Enforced: the governor is at '{governor['level']}'. Warn pauses background ingestion, stops idle optional "
                "processes and refuses new ones; stop also refuses new heavy work (tasks, ingestion, model switches) and "
                f"keeps chat available. It steps down after {t.recover_after_s:.0f} s below a level.")
    else:
        note = "Levels are shown only: the resource governor is switched off in the configuration."
    return {"level": worst, "checks": checks, "enforced": enforced, "enforcement_note": note, "governor": governor}
