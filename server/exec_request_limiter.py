from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator


class ExecRequestRejected(Exception):
    """Raised when an exec request cannot enter the server-side queue."""


@dataclass(frozen=True)
class ExecRequestLimiterStats:
    max_in_flight: int
    max_queued: int
    in_flight: int
    queued: int


class ExecRequestLimiter:
    def __init__(self, *, max_in_flight: int = -1, max_queued: int = -1) -> None:
        if max_in_flight == 0 or max_in_flight < -1:
            raise ValueError("max_in_flight must be -1 or positive")
        if max_queued < -1:
            raise ValueError("max_queued must be -1 or non-negative")
        self.max_in_flight = max_in_flight
        self.max_queued = max_queued
        self._in_flight = 0
        self._queued = 0
        self._condition = asyncio.Condition()

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        await self.acquire()
        try:
            yield
        finally:
            await self.release()

    async def acquire(self) -> None:
        if self.max_in_flight < 0:
            self._in_flight += 1
            return

        async with self._condition:
            if self._in_flight < self.max_in_flight:
                self._in_flight += 1
                return
            if self.max_queued >= 0 and self._queued >= self.max_queued:
                raise ExecRequestRejected("exec request queue is full")
            self._queued += 1
            try:
                while self._in_flight >= self.max_in_flight:
                    await self._condition.wait()
                self._queued -= 1
                self._in_flight += 1
            except BaseException:
                self._queued -= 1
                self._condition.notify(1)
                raise

    async def release(self) -> None:
        if self.max_in_flight < 0:
            self._in_flight = max(self._in_flight - 1, 0)
            return

        async with self._condition:
            self._in_flight = max(self._in_flight - 1, 0)
            self._condition.notify(1)

    def stats(self) -> ExecRequestLimiterStats:
        return ExecRequestLimiterStats(
            max_in_flight=self.max_in_flight,
            max_queued=self.max_queued,
            in_flight=self._in_flight,
            queued=self._queued,
        )
