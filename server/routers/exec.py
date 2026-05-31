from __future__ import annotations

import asyncio
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_key
from ..exec_backends import (
    StepBatchBackendConfig,
    StepBatchCapError,
    execute_step_batch_request,
    return_worker as _return_worker,
)
from ..exec_lifecycle import ItemLifecycleRegistry
from ..exec_metrics import ExecMetrics
from ..exec_request_limiter import ExecRequestLimiter, ExecRequestRejected
from ..exec_state_files import discard_unpromoted_state_paths
from ..exec_stats import collect_exec_stats
from ..pantograph_manager import (
    NoAvailablePantographWorkerError,
    PantographManager,
    header_hash,
)
from ..schemas_exec import (
    CancelRequest,
    CancelResponse,
    CancelResult,
    CancelStatus,
    CleanupRequest,
    CleanupResponse,
    CleanupResult,
    CreateStatesItem,
    CreateStatesRequest,
    CreateStatesResponse,
    CreateStatesResult,
    ExecLimitsResponse,
    ExecStatsResponse,
    StateInfo,
    StepBatchRequest,
    StepBatchResponse,
)
from ..split import split_snippet
from ..settings import Settings
from ..state_store import StateStore

router = APIRouter(prefix="/exec")


def get_state_store(request: Request) -> StateStore:
    return cast(StateStore, request.app.state.state_store)


def get_pantograph_manager(request: Request) -> PantographManager:
    return cast(PantographManager, request.app.state.pantograph_manager)


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_exec_lifecycle(request: Request) -> ItemLifecycleRegistry:
    return cast(ItemLifecycleRegistry, request.app.state.exec_lifecycle)


def get_exec_request_limiter(request: Request) -> ExecRequestLimiter:
    return cast(ExecRequestLimiter, request.app.state.exec_request_limiter)


def get_exec_metrics(request: Request) -> ExecMetrics:
    return cast(ExecMetrics, request.app.state.exec_metrics)


@router.post(
    "/create_states",
    response_model=CreateStatesResponse,
    response_model_exclude_none=True,
)
async def create_states(
    request: CreateStatesRequest,
    state_store: StateStore = Depends(get_state_store),
    pantograph_manager: PantographManager = Depends(get_pantograph_manager),
    lifecycle: ItemLifecycleRegistry = Depends(get_exec_lifecycle),
    limiter: ExecRequestLimiter = Depends(get_exec_request_limiter),
    metrics: ExecMetrics = Depends(get_exec_metrics),
    settings: Settings = Depends(get_settings),
    _api_key: str | None = Depends(require_key),
) -> CreateStatesResponse:
    try:
        _validate_create_request(request, settings)
    except HTTPException:
        metrics.record_rejection("create_states.cap")
        raise

    async def create_one(item: CreateStatesItem) -> CreateStatesResult:
        begin = lifecycle.begin(item.item_id)
        if not begin.started:
            return CreateStatesResult(
                item_id=item.item_id,
                status="cancelled",
                messages=[f"item {item.item_id!r} is cancelled"],
            )

        split_result = split_snippet(item.code)
        item_header_hash = header_hash(split_result.header)
        lease = None
        try:
            lease = await pantograph_manager.get_worker(
                env_profile=request.env_profile,
                header=split_result.header,
                timeout=item.acquire_timeout_ms / 1000,
            )
            if lifecycle.should_cancel(item.item_id):
                return CreateStatesResult(
                    item_id=item.item_id,
                    status="cancelled",
                    messages=[f"item {item.item_id!r} is cancelled"],
                )
            lease.worker.set_timeout_seconds(_timeout_seconds(item.step_timeout_ms))
            result = await lease.worker.create_states_from_code(
                split_result.body,
                state_dir=state_store.root_dir,
            )
            if lifecycle.should_cancel(item.item_id):
                discard_unpromoted_state_paths(state.path for state in result.states)
                return CreateStatesResult(
                    item_id=item.item_id,
                    status="cancelled",
                    messages=[f"item {item.item_id!r} is cancelled"],
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
                status="overloaded",
                messages=[str(exc)],
            )
        except Exception as exc:
            return CreateStatesResult(
                item_id=item.item_id,
                status="error",
                messages=[str(exc)],
            )
        finally:
            lifecycle.finish(item.item_id)
            await _return_worker(pantograph_manager, lease)

    try:
        async with limiter.slot():
            results = await asyncio.gather(*(create_one(item) for item in request.items))
            response = CreateStatesResponse(items=list(results))
            metrics.record_endpoint("create_states")
            metrics.record_exec_statuses(item.status for item in response.items)
            return response
    except ExecRequestRejected as exc:
        metrics.record_rejection("create_states.request_limiter")
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post(
    "/step_batch",
    response_model=StepBatchResponse,
    response_model_exclude_none=True,
)
async def step_batch(
    request: StepBatchRequest,
    state_store: StateStore = Depends(get_state_store),
    pantograph_manager: PantographManager = Depends(get_pantograph_manager),
    lifecycle: ItemLifecycleRegistry = Depends(get_exec_lifecycle),
    limiter: ExecRequestLimiter = Depends(get_exec_request_limiter),
    metrics: ExecMetrics = Depends(get_exec_metrics),
    settings: Settings = Depends(get_settings),
    _api_key: str | None = Depends(require_key),
) -> StepBatchResponse:
    config = StepBatchBackendConfig(
        max_items_per_step_batch=settings.max_items_per_step_batch,
        max_tactics_per_step_item=settings.max_tactics_per_step_item,
        max_attempts_per_step_batch=settings.max_attempts_per_step_batch,
        max_lean_processes_per_env_profile=(
            settings.max_lean_processes_per_env_profile
        ),
        max_acquire_timeout_ms=settings.max_acquire_timeout_ms,
        max_step_timeout_ms=settings.max_step_timeout_ms,
    )
    try:
        async with limiter.slot():
            response = await execute_step_batch_request(
                request,
                state_store=state_store,
                pantograph_manager=pantograph_manager,
                lifecycle=lifecycle,
                config=config,
            )
            metrics.record_endpoint("step_batch")
            metrics.record_exec_statuses(
                result.status
                for item in response.items
                for result in item.results
            )
            return response
    except StepBatchCapError as exc:
        metrics.record_rejection("step_batch.cap")
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ExecRequestRejected as exc:
        metrics.record_rejection("step_batch.request_limiter")
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _timeout_seconds(timeout_ms: int) -> int:
    return max((timeout_ms + 999) // 1000, 1)


@router.post(
    "/cleanup",
    response_model=CleanupResponse,
    response_model_exclude_none=True,
)
async def cleanup(
    request: CleanupRequest,
    state_store: StateStore = Depends(get_state_store),
    lifecycle: ItemLifecycleRegistry = Depends(get_exec_lifecycle),
    metrics: ExecMetrics = Depends(get_exec_metrics),
    _api_key: str | None = Depends(require_key),
) -> CleanupResponse:
    deleted_items: list[CleanupResult] = []
    for item_id in request.item_ids:
        decision = lifecycle.cleanup_decision(item_id)
        if not decision.should_delete:
            deleted_items.append(
                CleanupResult(
                    item_id=item_id,
                    status="deferred",
                    reason="in_flight",
                    in_flight=decision.snapshot.in_flight,
                    deleted_states=0,
                    deleted_bytes=0,
                )
            )
            continue

        delete_decision = state_store.delete_by_item_id_all_or_none(item_id)
        if not delete_decision.deleted:
            deleted_items.append(
                CleanupResult(
                    item_id=item_id,
                    status="deferred",
                    reason="pinned",
                    pinned_states=delete_decision.pinned_states,
                    deleted_states=0,
                    deleted_bytes=0,
                )
            )
            continue

        lifecycle.mark_cleaned(item_id)
        deleted_items.append(
            CleanupResult(
                item_id=item_id,
                status="deleted",
                deleted_states=delete_decision.stats.deleted_states,
                deleted_bytes=delete_decision.stats.deleted_bytes,
            )
        )
    response = CleanupResponse(deleted_items=deleted_items)
    metrics.record_endpoint("cleanup")
    metrics.record_cleanup_statuses(item.status for item in response.deleted_items)
    return response


@router.post(
    "/cancel",
    response_model=CancelResponse,
    response_model_exclude_none=True,
)
async def cancel(
    request: CancelRequest,
    lifecycle: ItemLifecycleRegistry = Depends(get_exec_lifecycle),
    metrics: ExecMetrics = Depends(get_exec_metrics),
    _api_key: str | None = Depends(require_key),
) -> CancelResponse:
    response = CancelResponse(
        items=[
            CancelResult(
                item_id=item_id,
                status=cast(CancelStatus, (snapshot := lifecycle.cancel(item_id)).status),
                in_flight=snapshot.in_flight,
            )
            for item_id in request.item_ids
        ]
    )
    metrics.record_endpoint("cancel")
    metrics.record_cancel_statuses(item.status for item in response.items)
    return response


@router.get(
    "/limits",
    response_model=ExecLimitsResponse,
)
async def limits(
    settings: Settings = Depends(get_settings),
    metrics: ExecMetrics = Depends(get_exec_metrics),
    _api_key: str | None = Depends(require_key),
) -> ExecLimitsResponse:
    metrics.record_endpoint("limits")
    return ExecLimitsResponse(
        max_items_per_step_batch=settings.max_items_per_step_batch,
        max_tactics_per_step_item=settings.max_tactics_per_step_item,
        max_attempts_per_step_batch=settings.max_attempts_per_step_batch,
        max_create_items_per_request=settings.max_create_items_per_request,
        max_pantograph_workers=settings.max_pantograph_workers,
        max_lean_processes_per_env_profile=settings.max_lean_processes_per_env_profile,
        max_in_flight_exec_requests=settings.max_in_flight_exec_requests,
        max_queued_exec_requests=settings.max_queued_exec_requests,
        max_acquire_timeout_ms=settings.max_acquire_timeout_ms,
        max_step_timeout_ms=settings.max_step_timeout_ms,
        recommended_items_per_step_batch=settings.recommended_items_per_step_batch,
        recommended_in_flight_step_batches=(
            settings.recommended_in_flight_step_batches
        ),
        same_item_id_pipelining=False,
        cleanup_policy="defer_while_in_flight",
    )


@router.get(
    "/stats",
    response_model=ExecStatsResponse,
)
async def stats(
    state_store: StateStore = Depends(get_state_store),
    pantograph_manager: PantographManager = Depends(get_pantograph_manager),
    lifecycle: ItemLifecycleRegistry = Depends(get_exec_lifecycle),
    limiter: ExecRequestLimiter = Depends(get_exec_request_limiter),
    metrics: ExecMetrics = Depends(get_exec_metrics),
    _api_key: str | None = Depends(require_key),
) -> ExecStatsResponse:
    metrics.record_endpoint("stats")
    return await collect_exec_stats(
        state_store=state_store,
        pantograph_manager=pantograph_manager,
        lifecycle=lifecycle,
        limiter=limiter,
        metrics=metrics,
    )


def _validate_create_request(
    request: CreateStatesRequest,
    settings: Settings,
) -> None:
    if len(request.items) > settings.max_create_items_per_request:
        raise HTTPException(
            status_code=422,
            detail=(
                "create_states item count exceeds "
                f"max_create_items_per_request={settings.max_create_items_per_request}"
            ),
        )
    for item in request.items:
        if item.acquire_timeout_ms > settings.max_acquire_timeout_ms:
            raise HTTPException(
                status_code=422,
                detail=(
                    "create_states acquire timeout exceeds "
                    f"max_acquire_timeout_ms={settings.max_acquire_timeout_ms}"
                ),
            )
        if item.step_timeout_ms > settings.max_step_timeout_ms:
            raise HTTPException(
                status_code=422,
                detail=(
                    "create_states step timeout exceeds "
                    f"max_step_timeout_ms={settings.max_step_timeout_ms}"
                ),
            )
