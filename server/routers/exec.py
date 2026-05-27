from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_key
from ..schemas_exec import (
    CleanupRequest,
    CleanupResponse,
    CleanupResult,
    CreateStatesRequest,
    CreateStatesResponse,
    StepBatchRequest,
    StepBatchResponse,
)
from ..state_store import StateStore

router = APIRouter(prefix="/exec")


def get_state_store(request: Request) -> StateStore:
    return cast(StateStore, request.app.state.state_store)


@router.post(
    "/create_states",
    response_model=CreateStatesResponse,
    response_model_exclude_none=True,
)
async def create_states(
    request: CreateStatesRequest,
    _api_key: str | None = Depends(require_key),
) -> CreateStatesResponse:
    raise HTTPException(501, "Pantograph create_states is not implemented yet")


@router.post(
    "/step_batch",
    response_model=StepBatchResponse,
    response_model_exclude_none=True,
)
async def step_batch(
    request: StepBatchRequest,
    _api_key: str | None = Depends(require_key),
) -> StepBatchResponse:
    raise HTTPException(501, "Pantograph step_batch is not implemented yet")


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
