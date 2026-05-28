from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


ExecStatus = Literal["open", "complete", "error", "invalid_state_token"]


class ExecCreateStateItem(BaseModel):
    item_id: str = Field(min_length=1)
    code: str = Field(min_length=1)
    timeout_ms: int = Field(default=5000, ge=1)


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
    goals: list[str] = Field(default_factory=list)


class ExecCreateStatesResult(BaseModel):
    item_id: str
    status: ExecStatus
    states: list[ExecStateInfo] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)


class ExecCreateStatesResponse(BaseModel):
    items: list[ExecCreateStatesResult]


class ExecStepBatchItem(BaseModel):
    node_id: str = Field(min_length=1)
    state_token: str = Field(min_length=1)
    tactics: list[str] = Field(min_length=1)
    timeout_ms: int = Field(default=5000, ge=1)


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
    goals: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)


class ExecStepBatchResult(BaseModel):
    node_id: str
    results: list[ExecStepResult] = Field(default_factory=list)


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
    deleted_states: int
    deleted_bytes: int


class ExecCleanupResponse(BaseModel):
    deleted_items: list[ExecCleanupResult]
