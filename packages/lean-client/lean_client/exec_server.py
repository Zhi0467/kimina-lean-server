from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from ._exec_server_config_utils import (
    DEFAULT_MAX_STATE_STORE_BYTES,
    DEFAULT_RECOMMENDED_IN_FLIGHT_STEP_BATCHES,
    ExecSafetyConfig,
    append_cli_option,
    default_exec_worker_count,
    default_max_in_flight_exec_requests,
    default_max_queued_exec_requests,
    validate_bounded_exec_caps,
)


@dataclass(frozen=True)
class ExecServerConfig:
    """Dependency-light mirror of the server's programmatic launch config."""

    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = field(default_factory=default_exec_worker_count)

    max_lean_processes_per_env_profile: int | None = None
    max_items_per_step_batch: int = 1024
    max_tactics_per_step_item: int = 64
    max_attempts_per_step_batch: int = 8192
    max_create_items_per_request: int = 1024
    max_acquire_timeout_ms: int = 600_000
    max_step_timeout_ms: int = 600_000

    max_in_flight_exec_requests: int | None = None
    max_queued_exec_requests: int | None = None
    max_state_store_bytes: int = DEFAULT_MAX_STATE_STORE_BYTES
    allow_unbounded_exec: bool = False

    recommended_items_per_step_batch: int | None = None
    recommended_in_flight_step_batches: int = (
        DEFAULT_RECOMMENDED_IN_FLIGHT_STEP_BATCHES
    )

    state_store_dir: str | Path | None = None
    single_process: bool = True

    def __post_init__(self) -> None:
        if self.max_lean_processes_per_env_profile is None:
            object.__setattr__(
                self,
                "max_lean_processes_per_env_profile",
                self.workers,
            )
        if self.recommended_items_per_step_batch is None:
            object.__setattr__(
                self,
                "recommended_items_per_step_batch",
                self.workers,
            )
        if self.max_in_flight_exec_requests is None:
            object.__setattr__(
                self,
                "max_in_flight_exec_requests",
                default_max_in_flight_exec_requests(self.workers),
            )
        if self.max_queued_exec_requests is None:
            object.__setattr__(
                self,
                "max_queued_exec_requests",
                default_max_queued_exec_requests(
                    _required_int(self.max_in_flight_exec_requests)
                ),
            )
        _validate_positive("port", self.port)
        _validate_positive("workers", self.workers)
        _validate_positive(
            "max_lean_processes_per_env_profile",
            self.max_lean_processes_per_env_profile,
        )
        _validate_positive("max_items_per_step_batch", self.max_items_per_step_batch)
        _validate_positive("max_tactics_per_step_item", self.max_tactics_per_step_item)
        _validate_positive(
            "max_attempts_per_step_batch",
            self.max_attempts_per_step_batch,
        )
        _validate_positive(
            "max_create_items_per_request",
            self.max_create_items_per_request,
        )
        _validate_positive("max_acquire_timeout_ms", self.max_acquire_timeout_ms)
        _validate_positive("max_step_timeout_ms", self.max_step_timeout_ms)
        _validate_positive(
            "recommended_items_per_step_batch",
            self.recommended_items_per_step_batch,
        )
        _validate_positive(
            "recommended_in_flight_step_batches",
            self.recommended_in_flight_step_batches,
        )
        validate_bounded_exec_caps(cast(ExecSafetyConfig, self))

    def to_cli_args(self) -> list[str]:
        args: list[str] = []
        append_cli_option(args, "--host", self.host)
        append_cli_option(args, "--port", self.port)
        append_cli_option(args, "--workers", self.workers)
        append_cli_option(
            args,
            "--max-lean-processes-per-env-profile",
            _required_int(self.max_lean_processes_per_env_profile),
        )
        append_cli_option(
            args,
            "--max-items-per-step-batch",
            self.max_items_per_step_batch,
        )
        append_cli_option(
            args,
            "--max-tactics-per-step-item",
            self.max_tactics_per_step_item,
        )
        append_cli_option(
            args,
            "--max-attempts-per-step-batch",
            self.max_attempts_per_step_batch,
        )
        append_cli_option(
            args,
            "--max-create-items-per-request",
            self.max_create_items_per_request,
        )
        append_cli_option(args, "--max-acquire-timeout-ms", self.max_acquire_timeout_ms)
        append_cli_option(args, "--max-step-timeout-ms", self.max_step_timeout_ms)
        append_cli_option(
            args,
            "--max-in-flight-exec-requests",
            _required_int(self.max_in_flight_exec_requests),
        )
        append_cli_option(
            args,
            "--max-queued-exec-requests",
            _required_int(self.max_queued_exec_requests),
        )
        append_cli_option(args, "--max-state-store-bytes", self.max_state_store_bytes)
        if self.allow_unbounded_exec:
            args.append("--allow-unbounded-exec")
        append_cli_option(
            args,
            "--recommended-items-per-step-batch",
            _required_int(self.recommended_items_per_step_batch),
        )
        append_cli_option(
            args,
            "--recommended-in-flight-step-batches",
            self.recommended_in_flight_step_batches,
        )
        if self.state_store_dir is not None:
            append_cli_option(args, "--state-store-dir", self.state_store_dir)
        args.append("--single-process" if self.single_process else "--no-single-process")
        return args


def launch_server(
    cfg: ExecServerConfig,
    *,
    server_python: str | Path = sys.executable,
) -> subprocess.Popen[bytes]:
    command = [str(server_python), "-m", "server", *cfg.to_cli_args()]
    return subprocess.Popen(command, start_new_session=True)


def _validate_positive(name: str, value: int | None) -> None:
    if value is None:
        raise ValueError(f"{name} must be set")
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _required_int(value: int | None) -> int:
    if value is None:
        raise ValueError("expected config default to be populated")
    return value
