from __future__ import annotations

import asyncio
from typing import cast

from fastapi import APIRouter, Depends, Request

from ..auth import require_key
from ..pantograph_manager import (
    NoAvailablePantographWorkerError,
    PantographManager,
    PantographWorkerLease,
    header_hash,
)
from ..schemas_exec import (
    CleanupRequest,
    CleanupResponse,
    CleanupResult,
    CreateStatesItem,
    CreateStatesRequest,
    CreateStatesResponse,
    CreateStatesResult,
    StateInfo,
    StepBatchItem,
    StepBatchRequest,
    StepBatchResponse,
    StepBatchResult,
    StepResult,
)
from ..split import split_snippet
from ..state_store import StateStore, StateTokenNotFound

router = APIRouter(prefix="/exec")


def get_state_store(request: Request) -> StateStore:
    return cast(StateStore, request.app.state.state_store)


def get_pantograph_manager(request: Request) -> PantographManager:
    return cast(PantographManager, request.app.state.pantograph_manager)


@router.post(
    "/create_states",
    response_model=CreateStatesResponse,
    response_model_exclude_none=True,
)
async def create_states(
    request: CreateStatesRequest,
    state_store: StateStore = Depends(get_state_store),
    pantograph_manager: PantographManager = Depends(get_pantograph_manager),
    _api_key: str | None = Depends(require_key),
) -> CreateStatesResponse:
    async def create_one(item: CreateStatesItem) -> CreateStatesResult:
        split_result = split_snippet(item.code)
        item_header_hash = header_hash(split_result.header)
        lease = None
        try:
            lease = await pantograph_manager.get_worker(
                env_profile=request.env_profile,
                header=split_result.header,
                timeout=item.timeout_ms / 1000,
            )
            lease.worker.set_timeout_seconds(_timeout_seconds(item.timeout_ms))
            result = await lease.worker.create_states_from_code(
                split_result.body,
                state_dir=state_store.root_dir,
            )
            states = [
                StateInfo(
                    state_token=state_store.put(
                        state.path,
                        item_id=item.item_id,
                        env_profile=request.env_profile,
                        header=split_result.header,
                        header_hash=item_header_hash,
                    ),
                    goals=state.goals,
                )
                for state in result.states
            ]
            return CreateStatesResult(
                item_id=item.item_id,
                status=result.status,
                states=states,
                messages=result.messages,
            )
        except NoAvailablePantographWorkerError as exc:
            return CreateStatesResult(
                item_id=item.item_id,
                status="error",
                messages=[str(exc)],
            )
        except Exception as exc:
            return CreateStatesResult(
                item_id=item.item_id,
                status="error",
                messages=[str(exc)],
            )
        finally:
            await _return_worker(pantograph_manager, lease)

    results = await asyncio.gather(*(create_one(item) for item in request.items))
    return CreateStatesResponse(items=list(results))


@router.post(
    "/step_batch",
    response_model=StepBatchResponse,
    response_model_exclude_none=True,
)
async def step_batch(
    request: StepBatchRequest,
    state_store: StateStore = Depends(get_state_store),
    pantograph_manager: PantographManager = Depends(get_pantograph_manager),
    _api_key: str | None = Depends(require_key),
) -> StepBatchResponse:
    async def step_one(item: StepBatchItem) -> StepBatchResult:
        try:
            record = state_store.resolve(item.state_token)
        except StateTokenNotFound as exc:
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
        except NoAvailablePantographWorkerError as exc:
            return StepBatchResult(
                node_id=item.node_id,
                results=[
                    StepResult(tactic=tactic, status="error", messages=[str(exc)])
                    for tactic in item.tactics
                ],
            )
        except Exception as exc:
            return StepBatchResult(
                node_id=item.node_id,
                results=[
                    StepResult(tactic=tactic, status="error", messages=[str(exc)])
                    for tactic in item.tactics
                ],
            )
        finally:
            await _return_worker(pantograph_manager, lease)

    results = await asyncio.gather(*(step_one(item) for item in request.items))
    return StepBatchResponse(items=list(results))


def _timeout_seconds(timeout_ms: int) -> int:
    return max((timeout_ms + 999) // 1000, 1)


async def _return_worker(
    pantograph_manager: PantographManager,
    lease: PantographWorkerLease | None,
) -> None:
    """Release a healthy worker back to the pool, or destroy a dead one.

    A Pantograph command timeout or crash leaves the worker's subprocess dead
    (``proc is None``); recycling it would poison every later request routed to
    the same env/header. Such workers are destroyed so the pool starts fresh.
    """
    if lease is None:
        return
    if lease.worker.is_alive():
        await pantograph_manager.release_worker(lease)
    else:
        await pantograph_manager.destroy_worker(lease)


@router.post(
    "/cleanup",
    response_model=CleanupResponse,
    response_model_exclude_none=True,
)
async def cleanup(
    request: CleanupRequest,
    state_store: StateStore = Depends(get_state_store),
    _api_key: str | None = Depends(require_key),
) -> CleanupResponse:
    deleted_items = []
    for item_id in request.item_ids:
        stats = state_store.delete_by_item_id(item_id)
        deleted_items.append(
            CleanupResult(
                item_id=item_id,
                deleted_states=stats.deleted_states,
                deleted_bytes=stats.deleted_bytes,
            )
        )
    return CleanupResponse(deleted_items=deleted_items)
