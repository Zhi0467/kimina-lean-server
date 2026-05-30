from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from pydantic import BaseModel, Field, model_validator


ExecStatus = Literal[
    "open",
    "complete",
    "error",
    "invalid_state_token",
    "overloaded",
    "cancelled",
]
CleanupStatus = Literal["deleted", "deferred"]
CleanupDeferredReason = Literal["in_flight", "pinned"]
CancelStatus = Literal["cancelling", "drained", "cleaned"]


class _TimeoutItem(BaseModel):
    acquire_timeout_ms: int = Field(default=5000, ge=1)
    step_timeout_ms: int = Field(default=5000, ge=1)

    @model_validator(mode="before")
    @classmethod
    def apply_timeout_alias(cls, data: object) -> object:
        if not isinstance(data, Mapping):
            return data
        raw = cast(Mapping[str, object], data)
        timeout_ms: object | None = raw.get("timeout_ms")
        if timeout_ms is None:
            return dict(raw)
        copied: dict[str, object] = dict(raw)
        copied.setdefault("acquire_timeout_ms", timeout_ms)
        copied.setdefault("step_timeout_ms", timeout_ms)
        return copied

    @property
    def timeout_ms(self) -> int:
        """Deprecated compatibility alias for older callers."""
        return self.step_timeout_ms


class ExecCreateStateItem(_TimeoutItem):
    item_id: str = Field(min_length=1)
    code: str = Field(min_length=1)


class ExecCreateStatesRequest(BaseModel):
    env_profile: str = Field(min_length=1)
    items: list[ExecCreateStateItem] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_item_ids(self) -> "ExecCreateStatesRequest":
        item_ids = [item.item_id for item in self.items]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("item_id values must be unique")
        return self


class ExecStateInfo(BaseModel):
    state_token: str = Field(min_length=1)
    goals: list[str] = Field(default_factory=list[str])


class ExecCreateStatesResult(BaseModel):
    item_id: str
    status: ExecStatus
    states: list[ExecStateInfo] = Field(default_factory=list[ExecStateInfo])
    messages: list[str] = Field(default_factory=list[str])


class ExecCreateStatesResponse(BaseModel):
    items: list[ExecCreateStatesResult]


class ExecStepBatchItem(_TimeoutItem):
    node_id: str = Field(min_length=1)
    state_token: str = Field(min_length=1)
    tactics: list[str] = Field(min_length=1)


class ExecStepBatchRequest(BaseModel):
    items: list[ExecStepBatchItem] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_node_ids(self) -> "ExecStepBatchRequest":
        node_ids = [item.node_id for item in self.items]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("node_id values must be unique")
        return self


class ExecStepResult(BaseModel):
    tactic: str
    status: ExecStatus
    state_token: str | None = None
    goals: list[str] = Field(default_factory=list[str])
    messages: list[str] = Field(default_factory=list[str])


class ExecStepBatchResult(BaseModel):
    node_id: str
    results: list[ExecStepResult] = Field(default_factory=list[ExecStepResult])


class ExecStepBatchResponse(BaseModel):
    items: list[ExecStepBatchResult]


class ExecCleanupRequest(BaseModel):
    item_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_item_ids(self) -> "ExecCleanupRequest":
        if len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError("item_ids must be unique")
        return self


class ExecCleanupResult(BaseModel):
    item_id: str
    status: CleanupStatus = "deleted"
    reason: CleanupDeferredReason | None = None
    in_flight: int = 0
    pinned_states: int = 0
    deleted_states: int
    deleted_bytes: int


class ExecCleanupResponse(BaseModel):
    deleted_items: list[ExecCleanupResult]


class ExecCancelRequest(BaseModel):
    item_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_item_ids(self) -> "ExecCancelRequest":
        if len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError("item_ids must be unique")
        return self


class ExecCancelResult(BaseModel):
    item_id: str
    status: CancelStatus
    in_flight: int = 0


class ExecCancelResponse(BaseModel):
    items: list[ExecCancelResult]


class ExecLimitsResponse(BaseModel):
    max_items_per_step_batch: int
    max_tactics_per_step_item: int
    max_attempts_per_step_batch: int
    max_create_items_per_request: int
    max_pantograph_workers: int
    max_lean_processes_per_env_profile: int
    max_in_flight_exec_requests: int
    max_queued_exec_requests: int
    max_acquire_timeout_ms: int
    max_step_timeout_ms: int
    recommended_items_per_step_batch: int
    recommended_in_flight_step_batches: int
    same_item_id_pipelining: bool = False
    cleanup_policy: str = "defer_while_in_flight"
