"""Task state, its event stream (SSE, with T+ offsets) and the task log."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from xodus.security.secrets import scrub

TERMINAL = frozenset({"completed", "failed_safe", "blocked", "budget_exceeded", "step_limit", "cancelled", "error"})


@dataclass
class StepRecord:
    index: int
    capability_id: str
    args: Any
    origin: str
    status: str
    code: str | None = None
    message: str | None = None
    approval_id: str | None = None
    duration_ms: float | None = None
    output_bytes: int | None = None


class TaskLog:
    """Every task event except streamed tokens, one JSON line each, in logs/tasks.jsonl."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, task_id: str, event: dict[str, Any]) -> None:
        line = json.dumps(scrub({"task_id": task_id, **event}), ensure_ascii=False, default=str)
        with self._lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class Task:
    def __init__(self, task_id: str, goal: str, arm: str, offered: frozenset[str], log: TaskLog | None = None) -> None:
        self.id = task_id
        self.goal = goal
        self.arm = arm
        self.offered = offered
        self.created_at = time.time()
        self.status = "queued"
        self.message: str | None = None
        self.answer: str | None = None
        self.tainted = False
        self.steps: list[StepRecord] = []
        self.budget: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []
        self._log = log
        self._t0 = time.monotonic()
        self._changed = asyncio.Event()
        self.runner: asyncio.Task[None] | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def emit(self, type_: str, **data: Any) -> dict[str, Any]:
        event = {"seq": len(self.events) + 1, "t_ms": round((time.monotonic() - self._t0) * 1000), "type": type_, **data}
        self.events.append(event)
        if self._log is not None and type_ != "token":
            self._log.write(self.id, event)
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()
        return event

    def finish(self, status: str, **data: Any) -> None:
        """Set the terminal status and emit the final event in one step, so a subscriber never sees one without the other."""
        self.status = status
        self.message = data.get("message")
        self.emit("task_finished", status=status, **data)

    async def follow(self, after: int = 0) -> AsyncIterator[dict[str, Any]]:
        index = max(0, after)
        while True:
            changed = self._changed
            while index < len(self.events):
                yield self.events[index]
                index += 1
            if self.terminal:
                return
            await changed.wait()

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "arm": self.arm,
            "status": self.status,
            "message": self.message,
            "answer": self.answer,
            "offered_capabilities": sorted(self.offered),
            "tainted": self.tainted,
            "steps": [asdict(s) for s in self.steps],
            "budget": self.budget,
            "created_at": self.created_at,
            "events": len(self.events),
        }
