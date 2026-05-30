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


@pytest.mark.asyncio
async def test_exec_request_limiter_cancelled_waiter_releases_queue_slot() -> None:
    limiter = ExecRequestLimiter(max_in_flight=1, max_queued=2)

    await limiter.acquire()
    cancelled_waiter = asyncio.create_task(limiter.acquire())
    while limiter.stats().queued == 0:
        await asyncio.sleep(0)

    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter

    assert limiter.stats().in_flight == 1
    assert limiter.stats().queued == 0

    acquired = asyncio.Event()

    async def waiter() -> None:
        async with limiter.slot():
            acquired.set()

    next_waiter = asyncio.create_task(waiter())
    while limiter.stats().queued == 0:
        await asyncio.sleep(0)

    await limiter.release()
    await asyncio.wait_for(acquired.wait(), timeout=1)
    await next_waiter
    assert limiter.stats().in_flight == 0
    assert limiter.stats().queued == 0
