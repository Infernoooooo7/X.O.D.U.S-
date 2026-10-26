"""Hard execution budgets (master prompt section 5): steps, model calls, tokens, wall time and
tool-output bytes. Budgets are fixed from config when a task starts; nothing a model outputs can
change them. Time spent waiting for the user's approval does not count against wall time.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

from xodus.providers.base import RequestBudget, Usage


@dataclass(frozen=True)
class ExecutionBudget:
    max_steps: int
    max_model_calls: int
    max_total_tokens: int
    max_wall_s: float
    max_tool_output_bytes: int
    max_replans: int
    num_ctx: int
    max_output_tokens: int


class BudgetExceeded(Exception):
    status = "budget_exceeded"

    def __init__(self, which: str, used: float, limit: float) -> None:
        super().__init__(f"The {which} budget is used up ({used:g} of {limit:g}); stopping safely.")
        self.which = which
        self.used = used
        self.limit = limit
        self.message = str(self)


class StepLimitReached(BudgetExceeded):
    status = "step_limit"


class BudgetTracker:
    def __init__(self, budget: ExecutionBudget, clock: Callable[[], float] = time.monotonic) -> None:
        self.budget = budget
        self._clock = clock
        self._t0 = clock()
        self._paused_total = 0.0
        self._paused_at: float | None = None
        self.steps = 0
        self.model_calls = 0
        self.tokens = 0
        self.replans = 0

    def elapsed_s(self) -> float:
        paused = self._paused_total + ((self._clock() - self._paused_at) if self._paused_at is not None else 0.0)
        return self._clock() - self._t0 - paused

    def remaining_s(self) -> float:
        return max(0.0, self.budget.max_wall_s - self.elapsed_s())

    def pause(self) -> None:
        if self._paused_at is None:
            self._paused_at = self._clock()

    def resume(self) -> None:
        if self._paused_at is not None:
            self._paused_total += self._clock() - self._paused_at
            self._paused_at = None

    def check_time(self) -> None:
        if self.elapsed_s() > self.budget.max_wall_s:
            raise BudgetExceeded("wall-time (s)", round(self.elapsed_s(), 1), self.budget.max_wall_s)

    def start_model_call(self) -> None:
        self.check_time()
        if self.model_calls >= self.budget.max_model_calls:
            raise BudgetExceeded("model-call", self.model_calls, self.budget.max_model_calls)
        if self.tokens >= self.budget.max_total_tokens:
            raise BudgetExceeded("token", self.tokens, self.budget.max_total_tokens)
        self.model_calls += 1

    def record_usage(self, usage: Usage) -> None:
        self.tokens += usage.total

    def start_step(self) -> None:
        self.check_time()
        if self.steps >= self.budget.max_steps:
            raise StepLimitReached("step", self.steps, self.budget.max_steps)
        self.steps += 1

    def start_replan(self) -> bool:
        if self.replans >= self.budget.max_replans:
            return False
        self.replans += 1
        return True

    def request_budget(self, max_output_tokens: int | None = None) -> RequestBudget:
        """Per-call limits: never more output than the task allows, never longer than the time left."""
        out = min(max_output_tokens or self.budget.max_output_tokens, self.budget.max_output_tokens)
        return RequestBudget(num_ctx=self.budget.num_ctx, max_output_tokens=out,
                             timeout_s=max(1.0, min(300.0, self.remaining_s())))

    def snapshot(self) -> dict[str, object]:
        return {"steps": self.steps, "model_calls": self.model_calls, "tokens": self.tokens,
                "replans": self.replans, "elapsed_s": round(self.elapsed_s(), 2), "limits": asdict(self.budget)}
