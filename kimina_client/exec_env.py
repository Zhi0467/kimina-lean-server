from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections.abc import Callable
from typing import Protocol

from ._exec_env_utils import single_step_result
from .async_client import AsyncKiminaClient
from .exec_journal import ExecMicrobatchJournal, UncertainMicrobatchError
from .exec_models import (
    ExecCancelResponse,
    ExecCleanupResponse,
    ExecCreateStateItem,
    ExecCreateStatesResponse,
    ExecLimitsResponse,
    ExecStatsResponse,
    ExecStepBatchItem,
    ExecStepBatchRequest,
    ExecStepBatchResponse,
    ExecStepBatchResult,
)


class AsyncLeanExecEnv:
    """Search-facing Lean execution environment.

    This wrapper keeps search code in proof-state terms while delegating HTTP
    transport details to ``AsyncKiminaClient``.
    """

    def __init__(
        self,
        client: AsyncKiminaClient,
        *,
        env_profile: str = "default",
        timeout_ms: int = 5000,
        acquire_timeout_ms: int | None = None,
        step_timeout_ms: int | None = None,
    ) -> None:
        self.client = client
        self.env_profile = env_profile
        self.acquire_timeout_ms = acquire_timeout_ms or timeout_ms
        self.step_timeout_ms = step_timeout_ms or timeout_ms
        self.timeout_ms = self.step_timeout_ms

    async def create_states(
        self,
        item_id: str,
        code: str,
        *,
        env_profile: str | None = None,
        timeout_ms: int | None = None,
        acquire_timeout_ms: int | None = None,
        step_timeout_ms: int | None = None,
    ) -> ExecCreateStatesResponse:
        return await self.create_states_batch(
            [
                ExecCreateStateItem(
                    item_id=item_id,
                    code=code,
                    acquire_timeout_ms=(
                        acquire_timeout_ms or timeout_ms or self.acquire_timeout_ms
                    ),
                    step_timeout_ms=step_timeout_ms or timeout_ms or self.step_timeout_ms,
                )
            ],
            env_profile=env_profile,
        )

    async def create_states_batch(
        self,
        items: list[ExecCreateStateItem],
        *,
        env_profile: str | None = None,
    ) -> ExecCreateStatesResponse:
        return await self.client.exec_create_states(
            env_profile or self.env_profile,
            items,
        )

    async def step_node(
        self,
        node_id: str,
        state_token: str,
        tactics: list[str],
        *,
        timeout_ms: int | None = None,
        acquire_timeout_ms: int | None = None,
        step_timeout_ms: int | None = None,
    ) -> ExecStepBatchResult:
        response = await self.step_batch(
            [
                ExecStepBatchItem(
                    node_id=node_id,
                    state_token=state_token,
                    tactics=tactics,
                    acquire_timeout_ms=(
                        acquire_timeout_ms or timeout_ms or self.acquire_timeout_ms
                    ),
                    step_timeout_ms=step_timeout_ms or timeout_ms or self.step_timeout_ms,
                )
            ]
        )
        return single_step_result(response, node_id)

    async def step_batch(
        self,
        items: list[ExecStepBatchItem],
    ) -> ExecStepBatchResponse:
        return await self.client.exec_step_batch(items)

    async def cleanup(self, item_ids: list[str]) -> ExecCleanupResponse:
        return await self.client.exec_cleanup(item_ids)

    async def cancel(self, item_ids: list[str]) -> ExecCancelResponse:
        return await self.client.exec_cancel(item_ids)

    async def limits(self) -> ExecLimitsResponse:
        return await self.client.exec_limits()

    async def stats(self) -> ExecStatsResponse:
        return await self.client.exec_stats()

    async def step_batch_resumable(
        self,
        *,
        env_call_id: str,
        items: list[ExecStepBatchItem],
        journal: ExecMicrobatchJournal,
        microbatch_size: int,
    ) -> ExecStepBatchResponse:
        if microbatch_size <= 0:
            raise ValueError("microbatch_size must be positive")

        merged: list[ExecStepBatchResult] = []
        for microbatch_id, start in enumerate(range(0, len(items), microbatch_size)):
            batch_items = items[start : start + microbatch_size]
            request = ExecStepBatchRequest(items=batch_items)
            request_payload = request.model_dump()
            record = journal.get(env_call_id, microbatch_id)
            if record is not None:
                if record.request_payload != request_payload:
                    raise ValueError(
                        "persisted microbatch request does not match current request"
                    )
                if record.status == "complete" and record.response_payload is not None:
                    merged.extend(
                        ExecStepBatchResponse.model_validate(
                            record.response_payload
                        ).items
                    )
                    continue
                raise UncertainMicrobatchError(
                    env_call_id,
                    microbatch_id,
                    record.status,
                )

            journal.put(
                env_call_id=env_call_id,
                microbatch_id=microbatch_id,
                status="running",
                request_payload=request_payload,
            )
            try:
                response = await self.step_batch(batch_items)
            except Exception:
                journal.put(
                    env_call_id=env_call_id,
                    microbatch_id=microbatch_id,
                    status="unknown",
                    request_payload=request_payload,
                )
                raise
            journal.put(
                env_call_id=env_call_id,
                microbatch_id=microbatch_id,
                status="complete",
                request_payload=request_payload,
                response_payload=response.model_dump(),
            )
            merged.extend(response.items)
        return ExecStepBatchResponse(items=merged)


class LeanExecEnvProtocol(Protocol):
    timeout_ms: int

    async def step_batch(
        self,
        items: list[ExecStepBatchItem],
    ) -> ExecStepBatchResponse: ...

    async def limits(self) -> ExecLimitsResponse: ...


@dataclass(frozen=True)
class _QueuedStep:
    item: ExecStepBatchItem
    future: asyncio.Future[ExecStepBatchResult]


class AsyncLeanExecBatcher:
    """Microbatch streaming proof-state expansions into item-level requests."""

    def __init__(
        self,
        env: LeanExecEnvProtocol,
        *,
        max_items: int = 32,
        max_wait_ms: int = 5,
        max_in_flight_batches: int = 2,
        max_overloaded_retries: int = 3,
        overload_backoff_seconds: float = 0.1,
        item_id_from_node_id: Callable[[str], str] | None = None,
    ) -> None:
        if max_items <= 0:
            raise ValueError("max_items must be positive")
        if max_wait_ms < 0:
            raise ValueError("max_wait_ms must be non-negative")
        if max_in_flight_batches <= 0:
            raise ValueError("max_in_flight_batches must be positive")
        if max_overloaded_retries < 0:
            raise ValueError("max_overloaded_retries must be non-negative")
        if overload_backoff_seconds < 0:
            raise ValueError("overload_backoff_seconds must be non-negative")

        self.env = env
        self.max_items = max_items
        self.max_wait_ms = max_wait_ms
        self.max_overloaded_retries = max_overloaded_retries
        self.overload_backoff_seconds = overload_backoff_seconds
        self.item_id_from_node_id = item_id_from_node_id or _default_item_id_from_node_id
        self._lock = asyncio.Lock()
        self._queue: list[_QueuedStep] = []
        self._timer_task: asyncio.Task[None] | None = None
        self._send_tasks: set[asyncio.Task[None]] = set()
        self._semaphore = asyncio.Semaphore(max_in_flight_batches)
        self._item_locks: dict[str, asyncio.Lock] = {}
        self._closed = False

    @classmethod
    async def from_server_limits(
        cls,
        env: LeanExecEnvProtocol,
        *,
        max_overloaded_retries: int = 3,
        overload_backoff_seconds: float = 0.1,
        item_id_from_node_id: Callable[[str], str] | None = None,
    ) -> "AsyncLeanExecBatcher":
        limits = await env.limits()
        return cls(
            env,
            max_items=limits.recommended_items_per_step_batch,
            max_wait_ms=0,
            max_in_flight_batches=limits.recommended_in_flight_step_batches,
            max_overloaded_retries=max_overloaded_retries,
            overload_backoff_seconds=overload_backoff_seconds,
            item_id_from_node_id=item_id_from_node_id,
        )

    async def submit_step(
        self,
        node_id: str,
        state_token: str,
        tactics: list[str],
        *,
        timeout_ms: int | None = None,
        acquire_timeout_ms: int | None = None,
        step_timeout_ms: int | None = None,
    ) -> ExecStepBatchResult:
        item = ExecStepBatchItem(
            node_id=node_id,
            state_token=state_token,
            tactics=tactics,
            acquire_timeout_ms=acquire_timeout_ms or timeout_ms or self.env.timeout_ms,
            step_timeout_ms=step_timeout_ms or timeout_ms or self.env.timeout_ms,
        )
        future: asyncio.Future[ExecStepBatchResult] = (
            asyncio.get_running_loop().create_future()
        )

        async with self._lock:
            if self._closed:
                raise RuntimeError("batcher is closed")
            self._queue.append(_QueuedStep(item=item, future=future))
            if len(self._queue) >= self.max_items or self.max_wait_ms == 0:
                self._schedule_flush_locked()
            elif self._timer_task is None or self._timer_task.done():
                self._timer_task = asyncio.create_task(self._flush_after_wait())

        return await future

    async def step_batch(
        self,
        items: list[ExecStepBatchItem],
    ) -> ExecStepBatchResponse:
        return await self.env.step_batch(items)

    async def flush(self) -> None:
        batches: list[list[_QueuedStep]] = []
        current_task = asyncio.current_task()
        async with self._lock:
            if self._timer_task is not None and self._timer_task is not current_task:
                self._timer_task.cancel()
            self._timer_task = None
            while self._queue:
                batch = self._queue[: self.max_items]
                del self._queue[: self.max_items]
                batches.append(batch)

        if not batches:
            return

        send_tasks = [self._track_send_task(batch) for batch in batches]
        await asyncio.gather(*send_tasks)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
        await self.flush()
        if self._send_tasks:
            await asyncio.gather(*list(self._send_tasks))

    async def _flush_after_wait(self) -> None:
        await asyncio.sleep(self.max_wait_ms / 1000)
        await self.flush()

    def _schedule_flush_locked(self) -> None:
        if self._timer_task is not None:
            self._timer_task.cancel()
            self._timer_task = None
        self._track_send_task_from_flush()

    def _track_send_task_from_flush(self) -> None:
        task = asyncio.create_task(self.flush())
        self._send_tasks.add(task)
        task.add_done_callback(self._send_tasks.discard)

    def _track_send_task(self, batch: list[_QueuedStep]) -> asyncio.Task[None]:
        task = asyncio.create_task(self._send_batch(batch))
        self._send_tasks.add(task)
        task.add_done_callback(self._send_tasks.discard)
        return task

    async def _send_batch(self, batch: list[_QueuedStep]) -> None:
        item_ids = sorted({self.item_id_from_node_id(item.item.node_id) for item in batch})
        locks = [self._item_locks.setdefault(item_id, asyncio.Lock()) for item_id in item_ids]
        for lock in locks:
            await lock.acquire()
        try:
            await self._send_batch_with_overload_retries(batch)
        finally:
            for lock in reversed(locks):
                lock.release()

    async def _send_batch_with_overload_retries(
        self,
        batch: list[_QueuedStep],
    ) -> None:
        async with self._semaphore:
            pending = list(batch)
            for attempt in range(self.max_overloaded_retries + 1):
                try:
                    response = await self.env.step_batch(
                        [queued.item for queued in pending]
                    )
                except Exception as exc:
                    for queued in pending:
                        if not queued.future.done():
                            queued.future.set_exception(exc)
                    return

                results_by_node_id = {item.node_id: item for item in response.items}
                overloaded: list[_QueuedStep] = []
                for queued in pending:
                    result = results_by_node_id.get(queued.item.node_id)
                    if queued.future.done():
                        continue
                    if result is None:
                        queued.future.set_exception(
                            RuntimeError(
                                "backend response missing node_id "
                                f"{queued.item.node_id!r}"
                            )
                        )
                    elif _is_overloaded_result(result) and (
                        attempt < self.max_overloaded_retries
                    ):
                        overloaded.append(queued)
                    else:
                        queued.future.set_result(result)

                if not overloaded:
                    return
                pending = overloaded
                if self.overload_backoff_seconds:
                    await asyncio.sleep(
                        self.overload_backoff_seconds * (2**attempt)
                    )


def _is_overloaded_result(result: ExecStepBatchResult) -> bool:
    return bool(result.results) and all(
        step.status == "overloaded" for step in result.results
    )


def _default_item_id_from_node_id(node_id: str) -> str:
    head, separator, _tail = node_id.rpartition(":")
    return head if separator else node_id
