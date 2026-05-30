from __future__ import annotations

import asyncio

import pytest

from server.exec_request_limiter import ExecRequestLimiter, ExecRequestRejected


@pytest.mark.asyncio
async def test_exec_request_limiter_rejects_when_queue_is_full() -> None:
    limiter = ExecRequestLimiter(max_in_flight=1, max_queued=0)

    await limiter.acquire()
    try:
        with pytest.raises(ExecRequestRejected):
            await limiter.acquire()
    finally:
        await limiter.release()


@pytest.mark.asyncio
async def test_exec_request_limiter_queues_until_slot_is_released() -> None:
    limiter = ExecRequestLimiter(max_in_flight=1, max_queued=1)

    await limiter.acquire()
    acquired = asyncio.Event()

    async def waiter() -> None:
        async with limiter.slot():
            acquired.set()

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    assert not acquired.is_set()

    await limiter.release()
    await asyncio.wait_for(acquired.wait(), timeout=1)
    await task
