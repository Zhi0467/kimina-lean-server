from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


ExecStatus = Literal["open", "complete", "error", "invalid_state_token"]


class CreateStatesItem(BaseModel):
    item_id: str = Field(min_length=1)
    code: str = Field(min_length=1)
    timeout_ms: int = Field(default=5000, ge=1)


class CreateStatesRequest(BaseModel):
    env_profile: str = Field(min_length=1)
    items: list[CreateStatesItem] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_item_ids(self) -> "CreateStatesRequest":
        item_ids = [item.item_id for item in self.items]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("item_id values must be unique")
        return self


class StepBatchItem(BaseModel):
    node_id: str = Field(min_length=1)
    state_token: str = Field(min_length=1)
    tactics: list[str] = Field(min_length=1)
    timeout_ms: int = Field(default=5000, ge=1)


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


class StateInfo(BaseModel):
    state_token: str = Field(min_length=1)
    goals: list[str] = Field(default_factory=list)


class CreateStatesResult(BaseModel):
    item_id: str
    status: ExecStatus
    states: list[StateInfo] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)


class CreateStatesResponse(BaseModel):
    items: list[CreateStatesResult]


class StepResult(BaseModel):
    tactic: str
    status: ExecStatus
    state_token: str | None = None
    goals: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)


class StepBatchResult(BaseModel):
    node_id: str
    results: list[StepResult] = Field(default_factory=list)


class StepBatchResponse(BaseModel):
    items: list[StepBatchResult]


class CleanupResult(BaseModel):
    item_id: str
    deleted_states: int
    deleted_bytes: int


class CleanupResponse(BaseModel):
    deleted_items: list[CleanupResult]
