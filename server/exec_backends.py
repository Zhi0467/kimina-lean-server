from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .exec_backend_utils import (
    compatibility_key,
    distribute_items_across_lanes,
    group_items_by_compatibility,
)
from .exec_lifecycle import ItemLifecycleRegistry
from .pantograph_manager import (
    NoAvailablePantographWorkerError,
    PantographManager,
    PantographWorkerLease,
)
from .pantograph_worker import PantographStepResult
from .schemas_exec import (
    ExecStatus,
    StepBatchItem,
    StepBatchRequest,
    StepBatchResponse,
    StepBatchResult,
    StepResult,
)
from .state_store import StateRecord, StateStore, StateTokenNotFound


@dataclass(frozen=True)
class StepBatchBackendConfig:
    max_items_per_step_batch: int
    max_tactics_per_step_item: int
    max_attempts_per_step_batch: int
    max_lean_processes_per_env_profile: int
    max_acquire_timeout_ms: int
    max_step_timeout_ms: int


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
    lifecycle: ItemLifecycleRegistry,
    config: StepBatchBackendConfig,
) -> StepBatchResponse:
    validate_step_batch_caps(request, config)
    items = await execute_step_batch_process_pool(
        request,
        state_store=state_store,
        pantograph_manager=pantograph_manager,
        lifecycle=lifecycle,
        config=config,
    )
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
        if item.acquire_timeout_ms > config.max_acquire_timeout_ms:
            raise StepBatchCapError(
                "step_batch acquire timeout exceeds "
                f"max_acquire_timeout_ms={config.max_acquire_timeout_ms}"
            )
        if item.step_timeout_ms > config.max_step_timeout_ms:
            raise StepBatchCapError(
                "step_batch step timeout exceeds "
                f"max_step_timeout_ms={config.max_step_timeout_ms}"
            )
    if attempts > config.max_attempts_per_step_batch:
        raise StepBatchCapError(
            "step_batch tactic attempts exceed "
            f"max_attempts_per_step_batch={config.max_attempts_per_step_batch}"
        )


async def execute_step_batch_process_pool(
    request: StepBatchRequest,
    *,
    state_store: StateStore,
    pantograph_manager: PantographManager,
    lifecycle: ItemLifecycleRegistry,
    config: StepBatchBackendConfig,
) -> list[StepBatchResult]:
    """Run step items through bounded old-command Pantograph worker lanes.

    Each lane owns one leased Pantograph process and steps its assigned items
    sequentially with the existing ``goal_load`` + ``goal_tactic`` path. This
    keeps Lean semantics identical to the old item-at-a-time implementation
    while avoiding per-item lease churn and bounding same-profile process count.
    """
    slots: list[StepBatchResult | None] = [None] * len(request.items)
    resolved: list[ResolvedStepItem] = []
    pinned_tokens: list[str] = []
    active_item_ids: list[str] = []
    for index, item in enumerate(request.items):
        try:
            record = state_store.resolve_and_pin(item.state_token)
        except StateTokenNotFound as exc:
            slots[index] = _invalid_token_result(item, exc)
            continue
        begin = lifecycle.begin(record.item_id)
        if not begin.started:
            state_store.unpin(item.state_token)
            slots[index] = _item_status_result(
                item,
                "cancelled",
                f"item {record.item_id!r} is cancelled",
            )
            continue
        pinned_tokens.append(item.state_token)
        active_item_ids.append(record.item_id)
        resolved.append(ResolvedStepItem(index=index, item=item, record=record))

    try:
        groups = group_items_by_compatibility(
            resolved,
            key_of_item=lambda resolved_item: compatibility_key(
                resolved_item.record.env_profile,
                resolved_item.record.header_hash,
            ),
        )
        await asyncio.gather(
            *(
                _run_process_pool_group(
                    group.items,
                    slots=slots,
                    state_store=state_store,
                    pantograph_manager=pantograph_manager,
                    lifecycle=lifecycle,
                    max_lanes=config.max_lean_processes_per_env_profile,
                )
                for group in groups
            )
        )
    finally:
        state_store.unpin_many(pinned_tokens)
        for item_id in active_item_ids:
            lifecycle.finish(item_id)

    return [
        result
        if result is not None
        else _item_error_result(request.items[index], "step_batch item was not executed")
        for index, result in enumerate(slots)
    ]


async def _run_process_pool_group(
    group_items: list[ResolvedStepItem],
    *,
    slots: list[StepBatchResult | None],
    state_store: StateStore,
    pantograph_manager: PantographManager,
    lifecycle: ItemLifecycleRegistry,
    max_lanes: int,
) -> None:
    lanes = distribute_items_across_lanes(group_items, max_lanes)
    await asyncio.gather(
        *(
            _run_process_pool_lane(
                lane,
                slots=slots,
                state_store=state_store,
                pantograph_manager=pantograph_manager,
                lifecycle=lifecycle,
            )
            for lane in lanes
        )
    )


async def _run_process_pool_lane(
    lane_items: list[ResolvedStepItem],
    *,
    slots: list[StepBatchResult | None],
    state_store: StateStore,
    pantograph_manager: PantographManager,
    lifecycle: ItemLifecycleRegistry,
) -> None:
    lease: PantographWorkerLease | None = None
    try:
        for resolved_item in lane_items:
            if lifecycle.should_cancel(resolved_item.record.item_id):
                slots[resolved_item.index] = _item_status_result(
                    resolved_item.item,
                    "cancelled",
                    f"item {resolved_item.record.item_id!r} is cancelled",
                )
                continue
            if lease is None:
                try:
                    lease = await pantograph_manager.get_worker(
                        env_profile=resolved_item.record.env_profile,
                        header=resolved_item.record.header,
                        timeout=resolved_item.item.acquire_timeout_ms / 1000,
                    )
                except NoAvailablePantographWorkerError as exc:
                    slots[resolved_item.index] = _item_status_result(
                        resolved_item.item,
                        "overloaded",
                        str(exc),
                    )
                    continue
            lease.worker.set_timeout_seconds(
                _timeout_seconds(resolved_item.item.step_timeout_ms)
            )
            try:
                worker_results = await lease.worker.step_state_with_tactics(
                    resolved_item.record.path,
                    resolved_item.item.tactics,
                    state_dir=state_store.root_dir,
                )
                slots[resolved_item.index] = _step_batch_result_from_worker_results(
                    resolved_item.item,
                    worker_results,
                    state_store=state_store,
                )
            except Exception as exc:
                slots[resolved_item.index] = _item_error_result(
                    resolved_item.item,
                    str(exc),
                )
            if not lease.worker.is_alive():
                await return_worker(pantograph_manager, lease)
                lease = None
    finally:
        await return_worker(pantograph_manager, lease)


def _step_batch_result_from_worker_results(
    item: StepBatchItem,
    worker_results: list[PantographStepResult],
    *,
    state_store: StateStore,
) -> StepBatchResult:
    results: list[StepResult] = []
    for result in worker_results:
        state_token = None
        if result.status == "open" and result.state_path is not None:
            state_token = state_store.create_child(
                item.state_token,
                result.state_path,
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
    return _item_status_result(item, "error", message)


def _item_status_result(
    item: StepBatchItem,
    status: ExecStatus,
    message: str,
) -> StepBatchResult:
    return StepBatchResult(
        node_id=item.node_id,
        results=[
            StepResult(tactic=tactic, status=status, messages=[message])
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
