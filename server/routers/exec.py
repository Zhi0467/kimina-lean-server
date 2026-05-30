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
from ..pantograph_manager import (
    NoAvailablePantographWorkerError,
    PantographManager,
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
    )
    try:
        return await execute_step_batch_request(
            request,
            state_store=state_store,
            pantograph_manager=pantograph_manager,
            config=config,
        )
    except StepBatchCapError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
    _api_key: str | None = Depends(require_key),
) -> CleanupResponse:
    deleted_items: list[CleanupResult] = []
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
