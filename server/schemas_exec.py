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
        """Deprecated compatibility alias for older internal callers."""
        return self.step_timeout_ms


class CreateStatesItem(_TimeoutItem):
    item_id: str = Field(min_length=1)
    code: str = Field(min_length=1)


class CreateStatesRequest(BaseModel):
    env_profile: str = Field(min_length=1)
    items: list[CreateStatesItem] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_item_ids(self) -> "CreateStatesRequest":
        item_ids = [item.item_id for item in self.items]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("item_id values must be unique")
        return self


class StepBatchItem(_TimeoutItem):
    node_id: str = Field(min_length=1)
    state_token: str = Field(min_length=1)
    tactics: list[str] = Field(min_length=1)


class StepBatchRequest(BaseModel):
    items: list[StepBatchItem] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_node_ids(self) -> "StepBatchRequest":
        node_ids = [item.node_id for item in self.items]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("node_id values must be unique")
        return self


class CleanupRequest(BaseModel):
    item_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_item_ids(self) -> "CleanupRequest":
        if len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError("item_ids must be unique")
        return self


class CancelRequest(BaseModel):
    item_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_item_ids(self) -> "CancelRequest":
        if len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError("item_ids must be unique")
        return self


class StateInfo(BaseModel):
    state_token: str = Field(min_length=1)
    goals: list[str] = Field(default_factory=list[str])


class CreateStatesResult(BaseModel):
    item_id: str
    status: ExecStatus
    states: list[StateInfo] = Field(default_factory=list[StateInfo])
    messages: list[str] = Field(default_factory=list[str])


class CreateStatesResponse(BaseModel):
    items: list[CreateStatesResult]


class StepResult(BaseModel):
    tactic: str
    status: ExecStatus
    state_token: str | None = None
    goals: list[str] = Field(default_factory=list[str])
    messages: list[str] = Field(default_factory=list[str])


class StepBatchResult(BaseModel):
    node_id: str
    results: list[StepResult] = Field(default_factory=list[StepResult])


class StepBatchResponse(BaseModel):
    items: list[StepBatchResult]


class CleanupResult(BaseModel):
    item_id: str
    status: CleanupStatus = "deleted"
    reason: CleanupDeferredReason | None = None
    in_flight: int = 0
    pinned_states: int = 0
    deleted_states: int
    deleted_bytes: int


class CleanupResponse(BaseModel):
    deleted_items: list[CleanupResult]


class CancelResult(BaseModel):
    item_id: str
    status: CancelStatus
    in_flight: int = 0


class CancelResponse(BaseModel):
    items: list[CancelResult]


class ExecLimitsResponse(BaseModel):
    max_items_per_step_batch: int
    max_tactics_per_step_item: int
    max_attempts_per_step_batch: int
    max_create_items_per_request: int
    max_pantograph_workers: int
    max_lean_processes_per_env_profile: int
    max_in_flight_exec_requests: int
    max_queued_exec_requests: int
    max_state_store_bytes: int
    allow_unbounded_exec: bool = False
    max_acquire_timeout_ms: int
    max_step_timeout_ms: int
    recommended_items_per_step_batch: int
    recommended_in_flight_step_batches: int
    single_process: bool = True
    same_item_id_pipelining: bool = False
    cleanup_policy: str = "defer_while_in_flight"


class ExecWorkerStats(BaseModel):
    env_profile: str
    header_hash: str
    status: str
    use_count: int
    pid: int | None = None
    rss_bytes: int | None = None


class ExecWorkerPoolStats(BaseModel):
    max_workers: int
    max_workers_per_env_profile: int
    worker_startup_timeout_seconds: int
    lease_requests: int
    lease_timeouts: int
    lease_wait_ms_total: float
    lease_wait_ms_max: float
    free_workers: int
    busy_workers: int
    starting_workers: int
    total_workers: int
    workers_by_env_profile: dict[str, int] = Field(default_factory=dict[str, int])
    workers: list[ExecWorkerStats] = Field(default_factory=list[ExecWorkerStats])


class ExecStateStoreStats(BaseModel):
    state_count: int
    total_bytes: int
    item_count: int
    pinned_states: int
    pin_refs: int


class ExecLifecycleStats(BaseModel):
    total_items: int
    active_items: int
    cancelling_items: int
    drained_items: int
    cleaned_items: int
    in_flight_items: int
    total_in_flight: int


class ExecRequestLimiterStats(BaseModel):
    max_in_flight: int
    max_queued: int
    in_flight: int
    queued: int


class ExecObservedMetrics(BaseModel):
    endpoint_requests: dict[str, int] = Field(default_factory=dict[str, int])
    rejected_requests: dict[str, int] = Field(default_factory=dict[str, int])
    exec_status_counts: dict[str, int] = Field(default_factory=dict[str, int])
    cleanup_status_counts: dict[str, int] = Field(default_factory=dict[str, int])
    cancel_status_counts: dict[str, int] = Field(default_factory=dict[str, int])


class ExecStatsResponse(BaseModel):
    state_store: ExecStateStoreStats
    worker_pool: ExecWorkerPoolStats
    lifecycle: ExecLifecycleStats
    request_limiter: ExecRequestLimiterStats
    metrics: ExecObservedMetrics
