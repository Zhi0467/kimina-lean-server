from __future__ import annotations

import asyncio
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .pantograph_manager import (
    NoAvailablePantographWorkerError,
    PantographManager,
    PantographWorkerLease,
)
from .pantograph_worker import (
    PantographBatchStepInput,
    PantographBatchStepItemResult,
    PantographStepResult,
)
from .schemas_exec import (
    StepBatchItem,
    StepBatchRequest,
    StepBatchResponse,
    StepBatchResult,
    StepResult,
)
from .state_store import StateRecord, StateStore, StateTokenNotFound


PANTOGRAPH_STATE_FORMAT = "pantograph_goal_state_file"


@dataclass(frozen=True)
class StepBatchBackendConfig:
    exec_backend: str
    max_items_per_step_batch: int
    max_tactics_per_step_item: int
    max_attempts_per_step_batch: int
    max_items_per_worker_batch: int
    max_parallel_items_per_lean_process: int
    # Track A: pre-realize lazy Lean Environment state before parallel fanout.
    pantograph_task_warmup: bool = True


@dataclass(frozen=True)
class ResolvedStepItem:
    index: int
    item: StepBatchItem
    record: StateRecord


class StepBatchCapError(ValueError):
    """Raised when a request exceeds configured step-batch caps."""


async def execute_step_batch_request(
    request: StepBatchRequest,
    *,
    state_store: StateStore,
    pantograph_manager: PantographManager,
    config: StepBatchBackendConfig,
) -> StepBatchResponse:
    validate_step_batch_caps(request, config)
    if config.exec_backend == "pantograph_pool":
        items = await execute_step_batch_pool(
            request,
            state_store=state_store,
            pantograph_manager=pantograph_manager,
        )
    elif config.exec_backend == "pantograph_task":
        items = await execute_step_batch_task(
            request,
            state_store=state_store,
            pantograph_manager=pantograph_manager,
            config=config,
        )
    else:
        raise StepBatchCapError(f"unsupported exec_backend: {config.exec_backend}")
    return StepBatchResponse(items=items)


def validate_step_batch_caps(
    request: StepBatchRequest,
    config: StepBatchBackendConfig,
) -> None:
    if len(request.items) > config.max_items_per_step_batch:
        raise StepBatchCapError(
            "step_batch item count exceeds "
            f"max_items_per_step_batch={config.max_items_per_step_batch}"
        )
    attempts = 0
    for item in request.items:
        if len(item.tactics) > config.max_tactics_per_step_item:
            raise StepBatchCapError(
                "step_batch tactic count exceeds "
                f"max_tactics_per_step_item={config.max_tactics_per_step_item}"
            )
        attempts += len(item.tactics)
    if attempts > config.max_attempts_per_step_batch:
        raise StepBatchCapError(
            "step_batch tactic attempts exceed "
            f"max_attempts_per_step_batch={config.max_attempts_per_step_batch}"
        )


async def execute_step_batch_pool(
    request: StepBatchRequest,
    *,
    state_store: StateStore,
    pantograph_manager: PantographManager,
) -> list[StepBatchResult]:
    async def step_one(item: StepBatchItem) -> StepBatchResult:
        pinned = False
        try:
            record = state_store.resolve_and_pin(item.state_token)
            pinned = True
        except StateTokenNotFound as exc:
            return _invalid_token_result(item, exc)

        lease = None
        try:
            lease = await pantograph_manager.get_worker(
                env_profile=record.env_profile,
                header=record.header,
                timeout=item.timeout_ms / 1000,
            )
            lease.worker.set_timeout_seconds(_timeout_seconds(item.timeout_ms))
            worker_results = await lease.worker.step_state_with_tactics(
                record.path,
                item.tactics,
                state_dir=state_store.root_dir,
            )
            return _step_batch_result_from_worker_results(
                item,
                worker_results,
                state_store=state_store,
                backend_kind="pantograph_pool",
            )
        except NoAvailablePantographWorkerError as exc:
            return _item_error_result(item, str(exc))
        except Exception as exc:
            return _item_error_result(item, str(exc))
        finally:
            await return_worker(pantograph_manager, lease)
            if pinned:
                state_store.unpin(item.state_token)

    return list(await asyncio.gather(*(step_one(item) for item in request.items)))


async def execute_step_batch_task(
    request: StepBatchRequest,
    *,
    state_store: StateStore,
    pantograph_manager: PantographManager,
    config: StepBatchBackendConfig,
) -> list[StepBatchResult]:
    slots: list[StepBatchResult | None] = [None] * len(request.items)
    resolved: list[ResolvedStepItem] = []
    pinned_tokens: list[str] = []
    for index, item in enumerate(request.items):
        try:
            record = state_store.resolve_and_pin(item.state_token)
        except StateTokenNotFound as exc:
            slots[index] = _invalid_token_result(item, exc)
            continue
        pinned_tokens.append(item.state_token)
        if record.state_format != PANTOGRAPH_STATE_FORMAT:
            slots[index] = _item_error_result(
                item,
                f"unsupported state_format for pantograph_task: {record.state_format}",
            )
            continue
        resolved.append(ResolvedStepItem(index=index, item=item, record=record))

    try:
        chunks = _compatible_task_chunks(
            resolved,
            max_items_per_worker_batch=config.max_items_per_worker_batch,
        )
        await asyncio.gather(
            *(
                _execute_task_chunk(
                    chunk,
                    slots=slots,
                    state_store=state_store,
                    pantograph_manager=pantograph_manager,
                    config=config,
                )
                for chunk in chunks
            )
        )
    finally:
        state_store.unpin_many(pinned_tokens)

    return [
        result
        if result is not None
        else _item_error_result(request.items[index], "step_batch item was not executed")
        for index, result in enumerate(slots)
    ]


async def _execute_task_chunk(
    chunk: list[ResolvedStepItem],
    *,
    slots: list[StepBatchResult | None],
    state_store: StateStore,
    pantograph_manager: PantographManager,
    config: StepBatchBackendConfig,
) -> None:
    if not chunk:
        return
    # This is a command-level timeout. Keep worker chunks no larger than the
    # Lean-side parallel-item cap unless timeout semantics become per item.
    timeout_ms = max(item.item.timeout_ms for item in chunk)
    first = chunk[0]
    lease = None
    try:
        lease = await pantograph_manager.get_worker(
            env_profile=first.record.env_profile,
            header=first.record.header,
            timeout=timeout_ms / 1000,
        )
        lease.worker.set_timeout_seconds(_timeout_seconds(timeout_ms))
        worker_items = [
            PantographBatchStepInput(
                item_index=item.index,
                state_path=item.record.path,
                tactics=item.item.tactics,
            )
            for item in chunk
        ]
        with tempfile.TemporaryDirectory(
            dir=state_store.root_dir,
            prefix="pg_batch_",
        ) as output_dir:
            worker_results = await lease.worker.step_state_batch_with_tactics(
                worker_items,
                state_dir=Path(output_dir),
                max_parallel_items=config.max_parallel_items_per_lean_process,
                warmup=config.pantograph_task_warmup,
            )
            _promote_task_worker_results(
                worker_results,
                by_index={item.index: item for item in chunk},
                slots=slots,
                state_store=state_store,
            )
    except NoAvailablePantographWorkerError as exc:
        for item in chunk:
            slots[item.index] = _item_error_result(item.item, str(exc))
    except Exception as exc:
        for item in chunk:
            slots[item.index] = _item_error_result(item.item, str(exc))
    finally:
        await return_worker(pantograph_manager, lease)


def _promote_task_worker_results(
    worker_results: list[PantographBatchStepItemResult],
    *,
    by_index: dict[int, ResolvedStepItem],
    slots: list[StepBatchResult | None],
    state_store: StateStore,
) -> None:
    for item_result in worker_results:
        source = by_index.get(item_result.item_index)
        if source is None:
            continue
        try:
            slots[source.index] = _step_batch_result_from_worker_results(
                source.item,
                item_result.results,
                state_store=state_store,
                backend_kind="pantograph_task",
            )
        except Exception as exc:
            slots[source.index] = _item_error_result(source.item, str(exc))


def _compatible_task_chunks(
    items: list[ResolvedStepItem],
    *,
    max_items_per_worker_batch: int,
) -> list[list[ResolvedStepItem]]:
    chunk_size = max(max_items_per_worker_batch, 1)
    groups: dict[tuple[str, str, str], list[ResolvedStepItem]] = defaultdict(list)
    for item in items:
        groups[
            (
                item.record.env_profile,
                item.record.header_hash,
                item.record.header,
            )
        ].append(item)

    chunks: list[list[ResolvedStepItem]] = []
    for group in groups.values():
        for start in range(0, len(group), chunk_size):
            chunks.append(group[start : start + chunk_size])
    return chunks


def _step_batch_result_from_worker_results(
    item: StepBatchItem,
    worker_results: list[PantographStepResult],
    *,
    state_store: StateStore,
    backend_kind: str,
) -> StepBatchResult:
    results: list[StepResult] = []
    for result in worker_results:
        state_token = None
        if result.status == "open" and result.state_path is not None:
            state_token = state_store.create_child(
                item.state_token,
                result.state_path,
                backend_kind=backend_kind,
                state_format=PANTOGRAPH_STATE_FORMAT,
            )
        results.append(
            StepResult(
                tactic=result.tactic,
                status=result.status,
                state_token=state_token,
                goals=result.goals,
                messages=result.messages,
            )
        )
    return StepBatchResult(node_id=item.node_id, results=results)


def _invalid_token_result(
    item: StepBatchItem,
    exc: StateTokenNotFound,
) -> StepBatchResult:
    return StepBatchResult(
        node_id=item.node_id,
        results=[
            StepResult(
                tactic=tactic,
                status="invalid_state_token",
                messages=[str(exc)],
            )
            for tactic in item.tactics
        ],
    )


def _item_error_result(item: StepBatchItem, message: str) -> StepBatchResult:
    return StepBatchResult(
        node_id=item.node_id,
        results=[
            StepResult(tactic=tactic, status="error", messages=[message])
            for tactic in item.tactics
        ],
    )


def _timeout_seconds(timeout_ms: int) -> int:
    return max((timeout_ms + 999) // 1000, 1)


async def return_worker(
    pantograph_manager: PantographManager,
    lease: PantographWorkerLease | None,
) -> None:
    """Release a healthy worker back to the pool, or destroy a dead one."""
    if lease is None:
        return
    if not lease.worker.is_alive():
        await pantograph_manager.destroy_worker(lease)
        return
    try:
        await lease.worker.agc()
    except Exception:
        await pantograph_manager.destroy_worker(lease)
        return
    if lease.worker.is_alive():
        await pantograph_manager.release_worker(lease)
    else:
        await pantograph_manager.destroy_worker(lease)
