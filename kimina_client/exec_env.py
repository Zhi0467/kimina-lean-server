from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from ._exec_env_utils import single_step_result
from .async_client import AsyncKiminaClient
from .exec_models import (
    ExecCleanupResponse,
    ExecCreateStateItem,
    ExecCreateStatesResponse,
    ExecStepBatchItem,
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
    ) -> None:
        self.client = client
        self.env_profile = env_profile
        self.timeout_ms = timeout_ms

    async def create_states(
        self,
        item_id: str,
        code: str,
        *,
        env_profile: str | None = None,
        timeout_ms: int | None = None,
    ) -> ExecCreateStatesResponse:
        return await self.create_states_batch(
            [
                ExecCreateStateItem(
                    item_id=item_id,
                    code=code,
                    timeout_ms=timeout_ms or self.timeout_ms,
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
    ) -> ExecStepBatchResult:
        response = await self.step_batch(
            [
                ExecStepBatchItem(
                    node_id=node_id,
                    state_token=state_token,
                    tactics=tactics,
                    timeout_ms=timeout_ms or self.timeout_ms,
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


class LeanExecEnvProtocol(Protocol):
    timeout_ms: int

    async def step_batch(
        self,
        items: list[ExecStepBatchItem],
    ) -> ExecStepBatchResponse: ...


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
    ) -> None:
        if max_items <= 0:
            raise ValueError("max_items must be positive")
        if max_wait_ms < 0:
            raise ValueError("max_wait_ms must be non-negative")
        if max_in_flight_batches <= 0:
            raise ValueError("max_in_flight_batches must be positive")

        self.env = env
        self.max_items = max_items
        self.max_wait_ms = max_wait_ms
        self._lock = asyncio.Lock()
        self._queue: list[_QueuedStep] = []
        self._timer_task: asyncio.Task[None] | None = None
        self._send_tasks: set[asyncio.Task[None]] = set()
        self._semaphore = asyncio.Semaphore(max_in_flight_batches)
        self._closed = False

    async def submit_step(
        self,
        node_id: str,
        state_token: str,
        tactics: list[str],
        *,
        timeout_ms: int | None = None,
    ) -> ExecStepBatchResult:
        item = ExecStepBatchItem(
            node_id=node_id,
            state_token=state_token,
            tactics=tactics,
            timeout_ms=timeout_ms or self.env.timeout_ms,
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
        async with self._semaphore:
            try:
                response = await self.env.step_batch([queued.item for queued in batch])
            except Exception as exc:
                for queued in batch:
                    if not queued.future.done():
                        queued.future.set_exception(exc)
                return

        results_by_node_id = {item.node_id: item for item in response.items}
        for queued in batch:
            result = results_by_node_id.get(queued.item.node_id)
            if queued.future.done():
                continue
            if result is None:
                queued.future.set_exception(
                    RuntimeError(
                        f"backend response missing node_id {queued.item.node_id!r}"
                    )
                )
            else:
                queued.future.set_result(result)
