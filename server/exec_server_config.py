from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, Self, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from .settings import Settings


DEFAULT_MAX_IN_FLIGHT_EXEC_REQUESTS = 8
DEFAULT_MAX_QUEUED_EXEC_REQUESTS = 32
DEFAULT_MAX_STATE_STORE_BYTES = 16 * 2**30
DEFAULT_RECOMMENDED_IN_FLIGHT_STEP_BATCHES = 8


class ExecSafetySettings(Protocol):
    @property
    def max_in_flight_exec_requests(self) -> int: ...

    @property
    def max_queued_exec_requests(self) -> int: ...

    @property
    def max_state_store_bytes(self) -> int: ...

    @property
    def allow_unbounded_exec(self) -> bool: ...


def default_exec_worker_count() -> int:
    return max((os.cpu_count() or 1) - 1, 1)


def _copy_workers(data: dict[str, object]) -> int:
    value = data["workers"]
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError("workers must be an int")


def validate_bounded_exec_caps(config: ExecSafetySettings) -> None:
    _validate_exec_cap_value(
        "max_in_flight_exec_requests",
        config.max_in_flight_exec_requests,
        allow_zero=False,
    )
    _validate_exec_cap_value(
        "max_queued_exec_requests",
        config.max_queued_exec_requests,
        allow_zero=True,
    )
    _validate_exec_cap_value(
        "max_state_store_bytes",
        config.max_state_store_bytes,
        allow_zero=True,
    )
    if config.allow_unbounded_exec:
        return

    unbounded = [
        name
        for name, value in (
            ("max_in_flight_exec_requests", config.max_in_flight_exec_requests),
            ("max_queued_exec_requests", config.max_queued_exec_requests),
            ("max_state_store_bytes", config.max_state_store_bytes),
        )
        if value == -1
    ]
    if unbounded:
        joined = ", ".join(f"{name}=-1" for name in unbounded)
        raise ValueError(
            "unbounded /exec safety caps require allow_unbounded_exec=True: "
            f"{joined}"
        )


class ExecServerConfig(BaseModel):
    """Programmatic /exec server configuration.

    This is the typed app-launch contract used by CLI launchers. It maps onto
    ``Settings`` without requiring callers to import the server settings class.
    """

    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    workers: int = Field(default_factory=default_exec_worker_count, ge=1)

    max_lean_processes_per_env_profile: int = Field(
        default_factory=_copy_workers,
        ge=1,
    )
    max_items_per_step_batch: int = Field(default=1024, ge=1)
    max_tactics_per_step_item: int = Field(default=64, ge=1)
    max_attempts_per_step_batch: int = Field(default=8192, ge=1)
    max_create_items_per_request: int = Field(default=1024, ge=1)
    max_acquire_timeout_ms: int = Field(default=600_000, ge=1)
    max_step_timeout_ms: int = Field(default=600_000, ge=1)

    max_in_flight_exec_requests: int = DEFAULT_MAX_IN_FLIGHT_EXEC_REQUESTS
    max_queued_exec_requests: int = DEFAULT_MAX_QUEUED_EXEC_REQUESTS
    max_state_store_bytes: int = DEFAULT_MAX_STATE_STORE_BYTES
    allow_unbounded_exec: bool = False

    recommended_items_per_step_batch: int = Field(
        default_factory=_copy_workers,
        ge=1,
    )
    recommended_in_flight_step_batches: int = (
        DEFAULT_RECOMMENDED_IN_FLIGHT_STEP_BATCHES
    )

    state_store_dir: Path | None = None
    single_process: bool = True

    @model_validator(mode="after")
    def validate_exec_safety(self) -> Self:
        validate_bounded_exec_caps(self)
        return self

    @classmethod
    def validate_settings(cls, settings: ExecSafetySettings) -> None:
        validate_bounded_exec_caps(settings)

    def to_settings(self, base: Settings | None = None) -> Settings:
        if base is None:
            from .settings import Settings

            base = Settings()

        base.host = self.host
        base.port = self.port
        base.max_pantograph_workers = self.workers
        base.max_lean_processes_per_env_profile = (
            self.max_lean_processes_per_env_profile
        )
        base.max_items_per_step_batch = self.max_items_per_step_batch
        base.max_tactics_per_step_item = self.max_tactics_per_step_item
        base.max_attempts_per_step_batch = self.max_attempts_per_step_batch
        base.max_create_items_per_request = self.max_create_items_per_request
        base.max_acquire_timeout_ms = self.max_acquire_timeout_ms
        base.max_step_timeout_ms = self.max_step_timeout_ms
        base.max_in_flight_exec_requests = self.max_in_flight_exec_requests
        base.max_queued_exec_requests = self.max_queued_exec_requests
        base.max_state_store_bytes = self.max_state_store_bytes
        base.allow_unbounded_exec = self.allow_unbounded_exec
        base.recommended_items_per_step_batch = self.recommended_items_per_step_batch
        base.recommended_in_flight_step_batches = (
            self.recommended_in_flight_step_batches
        )
        if self.state_store_dir is not None:
            base.state_store_dir = self.state_store_dir
        base.single_process = self.single_process
        return base

def _validate_exec_cap_value(
    name: str,
    value: int,
    *,
    allow_zero: bool,
) -> None:
    minimum = 0 if allow_zero else 1
    if value == -1 or value >= minimum:
        return
    if allow_zero:
        raise ValueError(f"{name} must be -1 or non-negative")
    raise ValueError(f"{name} must be -1 or positive")
