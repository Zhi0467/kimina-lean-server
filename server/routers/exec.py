from __future__ import annotations

import asyncio
from time import perf_counter
from typing import Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_key
from ..exec_backends import (
    StepBatchBackendConfig,
    StepBatchCapError,
    execute_step_batch_request,
    goal_infos_from_worker,
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
    ExecDebugInfo,
    ExecDiagnostics,
    ExecLimitsResponse,
    ExecMessage,
    ExecPos,
    ExecStatsResponse,
    StateInfo,
    StepBatchRequest,
    StepBatchResponse,
    VerifyItem,
    VerifyRequest,
    VerifyResponse,
    VerifyResult,
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
                messages=[
                    _message(f"item {item.item_id!r} is cancelled", severity="error")
                ],
                diagnostics=_diagnostics(),
            )

        split_result = split_snippet(item.code)
        item_header_hash = header_hash(split_result.header)
        lease = None
        acquire_start = perf_counter()
        try:
            lease = await pantograph_manager.get_worker(
                env_profile=request.env_profile,
                header=split_result.header,
                timeout=item.acquire_timeout_ms / 1000,
            )
            acquire_ms = _elapsed_ms(acquire_start)
            if lifecycle.should_cancel(item.item_id):
                return CreateStatesResult(
                    item_id=item.item_id,
                    status="cancelled",
                    messages=[
                        _message(
                            f"item {item.item_id!r} is cancelled",
                            severity="error",
                        )
                    ],
                    diagnostics=_diagnostics(acquire_ms=acquire_ms),
                )
            lease.worker.set_timeout_seconds(_timeout_seconds(item.step_timeout_ms))
            lean_start = perf_counter()
            result = await lease.worker.create_states_from_code(
                split_result.body,
                state_dir=state_store.root_dir,
                debug=item.debug,
            )
            diagnostics = _diagnostics(
                acquire_ms=acquire_ms,
                lean_ms=_elapsed_ms(lean_start),
                debug=_debug_from_worker_debug(result.debug),
            )
            if lifecycle.should_cancel(item.item_id):
                discard_unpromoted_state_paths(state.path for state in result.states)
                return CreateStatesResult(
                    item_id=item.item_id,
                    status="cancelled",
                    messages=[
                        _message(
                            f"item {item.item_id!r} is cancelled",
                            severity="error",
                        )
                    ],
                    diagnostics=diagnostics,
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
                    goals=goal_infos_from_worker(state.goals),
                )
                for state in result.states
            ]
            return CreateStatesResult(
                item_id=item.item_id,
                status=result.status,
                states=states,
                messages=_offset_messages(
                    result.messages,
                    line_offset=split_result.header_line_count,
                ),
                diagnostics=diagnostics,
            )
        except NoAvailablePantographWorkerError as exc:
            acquire_ms = _elapsed_ms(acquire_start)
            return CreateStatesResult(
                item_id=item.item_id,
                status="overloaded",
                messages=[_message(str(exc), severity="error")],
                diagnostics=_diagnostics(acquire_ms=acquire_ms),
            )
        except Exception as exc:
            return CreateStatesResult(
                item_id=item.item_id,
                status="error",
                messages=[_message(str(exc), severity="error")],
                diagnostics=_diagnostics(),
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


@router.post(
    "/verify",
    response_model=VerifyResponse,
    response_model_exclude_none=True,
)
async def verify(
    request: VerifyRequest,
    pantograph_manager: PantographManager = Depends(get_pantograph_manager),
    lifecycle: ItemLifecycleRegistry = Depends(get_exec_lifecycle),
    limiter: ExecRequestLimiter = Depends(get_exec_request_limiter),
    metrics: ExecMetrics = Depends(get_exec_metrics),
    settings: Settings = Depends(get_settings),
    _api_key: str | None = Depends(require_key),
) -> VerifyResponse:
    try:
        _validate_verify_request(request, settings)
    except HTTPException:
        metrics.record_rejection("verify.cap")
        raise

    async def verify_one(item: VerifyItem) -> VerifyResult:
        begin = lifecycle.begin(item.item_id)
        if not begin.started:
            return VerifyResult(
                item_id=item.item_id,
                theorem_name=item.theorem_name,
                status="cancelled",
                messages=[
                    _message(f"item {item.item_id!r} is cancelled", severity="error")
                ],
                diagnostics=_diagnostics(),
            )

        split_result = split_snippet(item.code)
        lease = None
        acquire_start = perf_counter()
        try:
            lease = await pantograph_manager.get_worker(
                env_profile=request.env_profile,
                header=split_result.header,
                timeout=item.acquire_timeout_ms / 1000,
            )
            acquire_ms = _elapsed_ms(acquire_start)
            if lifecycle.should_cancel(item.item_id):
                return VerifyResult(
                    item_id=item.item_id,
                    theorem_name=item.theorem_name,
                    status="cancelled",
                    messages=[
                        _message(
                            f"item {item.item_id!r} is cancelled",
                            severity="error",
                        )
                    ],
                    diagnostics=_diagnostics(acquire_ms=acquire_ms),
                )
            lease.worker.set_timeout_seconds(_timeout_seconds(item.step_timeout_ms))
            lean_start = perf_counter()
            allowed_axioms = (
                item.allowed_axioms
                if item.allowed_axioms is not None
                else settings.verify_allowed_axioms
            )
            result = await lease.worker.verify_complete_proof(
                split_result.body,
                theorem_name=item.theorem_name,
                allowed_axioms=allowed_axioms,
                debug=item.debug,
            )
            diagnostics = _diagnostics(
                acquire_ms=acquire_ms,
                lean_ms=_elapsed_ms(lean_start),
                debug=_debug_from_worker_debug(result.debug),
            )
            return VerifyResult(
                item_id=item.item_id,
                theorem_name=item.theorem_name,
                status=result.status,
                axioms=result.axioms,
                messages=_offset_messages(
                    result.messages,
                    line_offset=split_result.header_line_count,
                ),
                diagnostics=diagnostics,
            )
        except NoAvailablePantographWorkerError as exc:
            acquire_ms = _elapsed_ms(acquire_start)
            return VerifyResult(
                item_id=item.item_id,
                theorem_name=item.theorem_name,
                status="overloaded",
                messages=[_message(str(exc), severity="error")],
                diagnostics=_diagnostics(acquire_ms=acquire_ms),
            )
        except Exception as exc:
            return VerifyResult(
                item_id=item.item_id,
                theorem_name=item.theorem_name,
                status="error",
                messages=[_message(str(exc), severity="error")],
                diagnostics=_diagnostics(),
            )
        finally:
            lifecycle.finish(item.item_id)
            await _return_worker(pantograph_manager, lease)

    try:
        async with limiter.slot():
            results = await asyncio.gather(*(verify_one(item) for item in request.items))
            response = VerifyResponse(items=list(results))
            metrics.record_endpoint("verify")
            metrics.record_exec_statuses(item.status for item in response.items)
            return response
    except ExecRequestRejected as exc:
        metrics.record_rejection("verify.request_limiter")
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _timeout_seconds(timeout_ms: int) -> int:
    return max((timeout_ms + 999) // 1000, 1)


def _elapsed_ms(start: float) -> float:
    return max((perf_counter() - start) * 1000, 0.0)


def _diagnostics(
    *,
    acquire_ms: float = 0.0,
    lean_ms: float = 0.0,
    debug: ExecDebugInfo | None = None,
) -> ExecDiagnostics:
    return ExecDiagnostics(acquire_ms=acquire_ms, lean_ms=lean_ms, debug=debug)


def _debug_from_worker_debug(debug: object) -> ExecDebugInfo | None:
    if debug is None:
        return None
    return ExecDebugInfo(
        cpu_max=float(getattr(debug, "cpu_max")),
        memory_max=int(getattr(debug, "memory_max")),
    )


def _message(
    data: str,
    *,
    severity: Literal["trace", "info", "warning", "error"],
) -> ExecMessage:
    return ExecMessage(severity=severity, data=data)


def _offset_messages(
    messages: list[ExecMessage],
    *,
    line_offset: int,
) -> list[ExecMessage]:
    if line_offset <= 0:
        return messages
    return [
        ExecMessage(
            severity=message.severity,
            data=message.data,
            pos=_offset_pos(message.pos, line_offset),
            end_pos=_offset_pos(message.end_pos, line_offset),
        )
        for message in messages
    ]


def _offset_pos(pos: ExecPos | None, line_offset: int) -> ExecPos | None:
    if pos is None:
        return None
    return ExecPos(line=pos.line + line_offset, col=pos.col)


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
        max_state_store_bytes=settings.max_state_store_bytes,
        allow_unbounded_exec=settings.allow_unbounded_exec,
        max_acquire_timeout_ms=settings.max_acquire_timeout_ms,
        max_step_timeout_ms=settings.max_step_timeout_ms,
        recommended_items_per_step_batch=settings.recommended_items_per_step_batch,
        recommended_in_flight_step_batches=(
            settings.recommended_in_flight_step_batches
        ),
        single_process=settings.single_process,
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


def _validate_verify_request(
    request: VerifyRequest,
    settings: Settings,
) -> None:
    if len(request.items) > settings.max_create_items_per_request:
        raise HTTPException(
            status_code=422,
            detail=(
                "verify item count exceeds "
                f"max_create_items_per_request={settings.max_create_items_per_request}"
            ),
        )
    for item in request.items:
        if item.acquire_timeout_ms > settings.max_acquire_timeout_ms:
            raise HTTPException(
                status_code=422,
                detail=(
                    "verify acquire timeout exceeds "
                    f"max_acquire_timeout_ms={settings.max_acquire_timeout_ms}"
                ),
            )
        if item.step_timeout_ms > settings.max_step_timeout_ms:
            raise HTTPException(
                status_code=422,
                detail=(
                    "verify step timeout exceeds "
                    f"max_step_timeout_ms={settings.max_step_timeout_ms}"
                ),
            )
