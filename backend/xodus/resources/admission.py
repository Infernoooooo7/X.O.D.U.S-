"""Bounded admission for model requests: max_running in flight, at most max_queued waiting."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class QueueFull(Exception):
    pass


class AdmissionGate:
    def __init__(self, max_running: int, max_queued: int) -> None:
        self._sem = asyncio.Semaphore(max_running)
        self.max_running = max_running
        self.max_queued = max_queued
        self.waiting = 0
        self.running = 0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        if self._sem.locked() and self.waiting >= self.max_queued:
            raise QueueFull(
                f"XODUS is busy ({self.running} running, {self.waiting} queued; the limit is "
                f"{self.max_queued} queued). Try again in a moment."
            )
        self.waiting += 1
        try:
            await self._sem.acquire()
        finally:
            self.waiting -= 1
        self.running += 1
        try:
            yield
        finally:
            self.running -= 1
            self._sem.release()
