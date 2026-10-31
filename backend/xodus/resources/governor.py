"""Resource governor (master prompt 3.7): acts on the threshold levels that thresholds.evaluate() classifies.

Every thresholds.check_interval_s it takes one sampler snapshot. The enforced level rises at once; it steps down only
after the measured level has stayed lower for thresholds.recover_after_s, to the highest level seen during that wait
(hysteresis, so a value hovering at a threshold does not flap). A check that cannot be measured never raises the level.

warn  hold background ingestion (the worker stops, its job resumes later from its last committed batch), stop optional
      processes that serve no call and were unused for idle_grace_s, refuse new optional processes.
stop  as warn, plus refuse new heavy work with a clear message: tasks, ingestion requests, model switches, heavy
      capabilities, and knowledge search or ask while the embedding model is not loaded (loading it costs about 1 GB).
      Chat stays available.
ok    reached again after recovery: release ingestion and record the episode (start, end, peaks, actions).

XODUS measures, throttles and refuses. It cannot prevent Windows from running out of memory and never stops a
process it did not launch.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from xodus.config.settings import ThresholdSettings
from xodus.obs import log_event
from xodus.resources.thresholds import evaluate

LEVELS = ("ok", "warn", "stop")
RANK = {name: i for i, name in enumerate(LEVELS)}
HEAVY = {"tasks": "new tasks", "ingest": "new ingestion requests", "model_switch": "model switches",
         "capability": "heavy capabilities", "search": "knowledge searches that would load the embedding model"}


class ResourcePressure(Exception):
    code = "resource_pressure"

    def __init__(self, message: str, retry_after_s: float) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after_s = retry_after_s


class ResourceGovernor:
    def __init__(self, thresholds: ThresholdSettings, sample: Callable[[], dict[str, Any]], ingestion: Any, supervisor: Any,
                 audit: Any, embedder_loaded: Callable[[], Awaitable[bool]] | None = None,
                 unload_embedder: Callable[[], bool] | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.t = thresholds
        self.sample = sample
        self.ingestion = ingestion
        self.supervisor = supervisor
        self.audit = audit
        self.embedder_loaded = embedder_loaded
        self.unload_embedder = unload_embedder
        self.clock = clock
        self.level = "ok"
        self.last: dict[str, Any] | None = None  # latest evaluation
        self.last_snapshot: tuple[float, dict[str, Any]] | None = None
        self.episode: dict[str, Any] | None = None
        self.episodes_total = 0
        self.errors = 0
        self._lower_since: float | None = None
        self._lower_max = 0
        if thresholds.enforce:
            supervisor.optional_admission = self._optional_refusal

    @property
    def enabled(self) -> bool:
        return self.t.enforce

    # ------------------------------------------------------------------ evaluation

    def tick(self) -> dict[str, Any]:
        """One check: sample, classify, act. Blocking (sampler and process stops); run it in a thread."""
        snap = self.sample()
        self.last_snapshot = (self.clock(), snap)
        return self.apply(evaluate(snap, self.t, self.state()))

    def apply(self, evaluation: dict[str, Any]) -> dict[str, Any]:
        self.last = evaluation
        if not self.enabled:
            return evaluation
        measured = evaluation["level"]
        now = self.clock()
        if measured in RANK:  # "unknown" never moves the level
            m, current = RANK[measured], RANK[self.level]
            if m > current:
                self._change(measured, evaluation, "rose")
            elif m < current:
                if self._lower_since is None:
                    self._lower_since, self._lower_max = now, m
                else:
                    self._lower_max = max(self._lower_max, m)
                if now - self._lower_since >= self.t.recover_after_s:
                    self._change(LEVELS[self._lower_max], evaluation, "recovered")
            else:
                self._lower_since = None
        if self.episode is not None:
            self._track_peaks(evaluation)
        if RANK[self.level] >= RANK["warn"]:
            self._enforce()
        evaluation["governor"] = self.state()
        return evaluation

    def _change(self, new: str, evaluation: dict[str, Any], how: str) -> None:
        old, self.level = self.level, new
        self._lower_since = None
        breaches = self._breaches(evaluation)
        log_event("resource_level_changed", logging.WARNING if new != "ok" else logging.INFO,
                  old=old, new=new, how=how, breaches=breaches)
        self.audit.append("resource_level_changed", old=old, new=new, how=how, breaches=breaches)
        if old == "ok":
            self.episode = {"started_at": time.time(), "highest": new, "peaks": {}, "actions": [], "first_breaches": breaches}
            self.episodes_total += 1
        if self.episode is not None and RANK[new] > RANK[self.episode["highest"]]:
            self.episode["highest"] = new
        if new == "ok":
            self._release()

    def _breaches(self, evaluation: dict[str, Any]) -> list[str]:
        out = []
        for c in evaluation["checks"]:
            if c["level"] in ("warn", "stop"):
                limit = c["stop_gb"] if c["level"] == "stop" else c["warn_gb"]
                sign = "<" if c["metric"] == "system_available" else ">="
                out.append(f"{c['label']} {c['value_gb']} GB {sign} {limit} GB ({c['level']})")
        return out

    def _track_peaks(self, evaluation: dict[str, Any]) -> None:
        peaks = self.episode["peaks"]  # type: ignore[index]
        for c in evaluation["checks"]:
            if c["value_gb"] is None:
                continue
            if c["metric"] == "system_available":
                peaks["system_available_min_gb"] = min(peaks.get("system_available_min_gb", c["value_gb"]), c["value_gb"])
            else:
                peaks[c["metric"] + "_max_gb"] = max(peaks.get(c["metric"] + "_max_gb", c["value_gb"]), c["value_gb"])

    # ------------------------------------------------------------------ actions

    def _action(self, what: str, **fields: Any) -> None:
        if self.episode is not None:
            self.episode["actions"].append({"t": round(time.time() - self.episode["started_at"], 1), "action": what, **fields})
        log_event("resource_action", logging.WARNING, resource_level=self.level, action=what, **fields)
        self.audit.append("resource_action", resource_level=self.level, action=what, **fields)

    def _enforce(self) -> None:
        if self.ingestion is not None and self.ingestion.held is None:
            result = self.ingestion.hold(f"resource {self.level} level")
            self._action("ingestion_held", worker_stopped=result["worker_stopped"], requeued_jobs=result["requeued_jobs"])
            if self.unload_embedder is not None:
                # Holding ingestion alone frees almost nothing: the embedding model it loaded stays resident for its
                # keep-alive (measured in the Gate F dry run: 928 MiB on the CPU), so the level could never fall and
                # ingestion stalled. A later search loads it again on demand.
                self._action("embedder_unloaded", ok=self.unload_embedder())
        for mp in self.supervisor.idle_optional(self.t.idle_grace_s):
            self.supervisor.stop(mp, f"resource {self.level} level: idle optional process")
            self._action("optional_process_stopped", pid=mp.pid, owner=mp.owner, role=mp.role)

    def _release(self) -> None:
        if self.ingestion is not None and self.ingestion.held is not None:
            result = self.ingestion.release("resources back below the warning level")
            self._action("ingestion_released", worker_started=result["worker_started"])
        if self.episode is not None:
            ep, self.episode = self.episode, None
            ep["ended_at"] = time.time()
            ep["duration_s"] = round(ep["ended_at"] - ep["started_at"], 1)
            log_event("resource_episode", **ep)
            self.audit.append("resource_episode", **ep)

    # ------------------------------------------------------------------ refusals

    def _reason(self) -> str:
        breaches = self._breaches(self.last) if self.last else []
        return "; ".join(breaches) if breaches else f"the reading has fallen back but must stay down for {self.t.recover_after_s:.0f} s"

    def _optional_refusal(self) -> str | None:
        if not self.enabled or self.level == "ok":
            return None
        return (f"XODUS is at its resource {self.level} level ({self._reason()}); new optional processes are refused until "
                f"memory use stays below the warning level for {self.t.recover_after_s:.0f} s.")

    def check(self, kind: str) -> None:
        """Raise ResourcePressure if `kind` of heavy work must be refused now (stop level only)."""
        if self.enabled and self.level == "stop":
            raise ResourcePressure(
                f"XODUS is at its resource stop level ({self._reason()}). {HEAVY[kind].capitalize()} are refused until "
                f"memory use stays below the warning level for {self.t.recover_after_s:.0f} s. Chat stays available.",
                self.t.recover_after_s)

    async def check_search(self) -> None:
        """At the stop level a search is refused only if it would have to load the embedding model."""
        if self.enabled and self.level == "stop" and not (self.embedder_loaded and await self.embedder_loaded()):
            self.check("search")

    # ------------------------------------------------------------------ reporting

    def recent_snapshot(self, max_age_s: float) -> dict[str, Any] | None:
        if self.last_snapshot is None or self.clock() - self.last_snapshot[0] > max_age_s:
            return None
        return self.last_snapshot[1]

    def state(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "level": self.level,
                "stepping_down_in_s": (round(max(0.0, self.t.recover_after_s - (self.clock() - self._lower_since)), 1)
                                       if self._lower_since is not None else None),
                "episode": self.episode, "episodes_total": self.episodes_total, "check_errors": self.errors,
                "check_interval_s": self.t.check_interval_s, "recover_after_s": self.t.recover_after_s}

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception as exc:  # the governor must never die; a failed check changes nothing
                self.errors += 1
                log_event("resource_check_failed", logging.ERROR, detail=repr(exc))
            await asyncio.sleep(self.t.check_interval_s)
