from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


DEFAULT_MAX_IN_FLIGHT_EXEC_REQUESTS = 8
DEFAULT_MAX_QUEUED_EXEC_REQUESTS = 32
DEFAULT_MAX_STATE_STORE_BYTES = 16 * 2**30
DEFAULT_RECOMMENDED_IN_FLIGHT_STEP_BATCHES = 8


class ExecSafetyConfig(Protocol):
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


def validate_bounded_exec_caps(config: ExecSafetyConfig) -> None:
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


def append_cli_option(args: list[str], flag: str, value: int | str | Path) -> None:
    args.extend([flag, str(value)])


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
