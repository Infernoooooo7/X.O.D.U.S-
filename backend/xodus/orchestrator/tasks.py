"""Task manager: creates tasks, runs them in the background, cancels them, keeps a bounded history."""

from __future__ import annotations

import asyncio
import secrets
from collections import OrderedDict

from xodus.config.settings import Settings
from xodus.orchestrator.budget import ExecutionBudget
from xodus.orchestrator.runner import ARMS, ExecutorDeps
from xodus.orchestrator.task import Task, TaskLog


class TaskLimit(Exception):
    pass


class TaskManager:
    def __init__(self, deps: ExecutorDeps, settings: Settings, log: TaskLog, max_kept: int = 50) -> None:
        self.deps = deps
        self.settings = settings
        self.log = log
        self.max_kept = max_kept
        self._tasks: OrderedDict[str, Task] = OrderedDict()

    def budget_for(self, arm: str) -> ExecutionBudget:
        limits = getattr(self.settings.executor, arm)
        return ExecutionBudget(
            max_steps=limits.max_steps,
            max_model_calls=limits.max_model_calls,
            max_total_tokens=limits.max_total_tokens,
            max_wall_s=limits.max_wall_s,
            max_tool_output_bytes=self.settings.executor.max_tool_output_bytes,
            max_replans=limits.max_replans,
            num_ctx=self.settings.ollama.num_ctx,
            max_output_tokens=self.settings.ollama.max_output_tokens,
        )

    def create(self, goal: str, arm: str | None = None) -> Task:
        arm = arm or self.settings.executor.default_arm
        if arm not in ARMS:
            raise ValueError(f"arm must be one of {sorted(ARMS)}")
        active = [t for t in self._tasks.values() if not t.terminal]
        if len(active) >= self.settings.executor.max_active_tasks:
            raise TaskLimit(f"{len(active)} tasks are already running (limit {self.settings.executor.max_active_tasks}).")
        # Capability selection: every available capability (the H2 control). Fixed here, before any
        # untrusted content is read; the task can only narrow this set later, never widen it.
        offered = frozenset(self.deps.registry.available_ids())
        task = Task(secrets.token_hex(6), goal, arm, offered, self.log)
        runner = ARMS[arm](task, self.deps, self.budget_for(arm))
        task.runner = asyncio.create_task(runner.run(), name=f"xodus-task-{task.id}")
        self._tasks[task.id] = task
        self._prune()
        return task

    def _prune(self) -> None:
        while len(self._tasks) > self.max_kept:
            oldest = next((tid for tid, t in self._tasks.items() if t.terminal), None)
            if oldest is None:
                break
            self._tasks.pop(oldest)

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def list(self) -> list[Task]:
        return list(reversed(self._tasks.values()))

    async def cancel(self, task: Task, wait_s: float = 15.0) -> None:
        if task.runner is not None and not task.terminal:
            task.runner.cancel()
            await asyncio.wait({task.runner}, timeout=wait_s)
        if not task.terminal:
            # Cancelled before its coroutine ever ran: run() never started, so record the end here.
            self.deps.approvals.withdraw_for_task(task.id)
            task.finish("cancelled", message="Cancelled before it started.")
            self.deps.audit.append("task_finished", task_id=task.id, status="cancelled", message="cancelled before start")

    async def shutdown(self) -> None:
        running = [t for t in self._tasks.values() if not t.terminal]
        for task in running:
            await self.cancel(task, wait_s=5.0)
