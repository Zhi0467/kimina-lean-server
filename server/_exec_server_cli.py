from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Sequence

from .exec_server_config import ExecServerConfig
from .settings import Settings


def parse_exec_server_config(argv: Sequence[str] | None = None) -> ExecServerConfig:
    values = _parse_cli_values(argv)
    values.pop("mode", None)
    return ExecServerConfig.model_validate(values)


def settings_from_cli_args(argv: Sequence[str] | None = None) -> Settings:
    values = _parse_cli_values(argv)
    mode = values.pop("mode", None)
    settings = Settings()
    workers = values.pop("workers", None)
    if workers is not None:
        settings.max_pantograph_workers = workers
        if "max_lean_processes_per_env_profile" not in values:
            settings.max_lean_processes_per_env_profile = workers
        if "recommended_items_per_step_batch" not in values:
            settings.recommended_items_per_step_batch = workers
    for key, value in values.items():
        setattr(settings, key, value)
    if mode is not None:
        settings.mode = mode
    _validate_settings_as_exec_config(settings)
    return settings


def _parse_cli_values(argv: Sequence[str] | None = None) -> dict[str, Any]:
    parser = _build_parser()
    namespace = parser.parse_args(argv)
    return {
        key: value
        for key, value in vars(namespace).items()
        if value is not None
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m server",
        description="Run the Kimina Lean Server app.",
    )
    parser.add_argument("--mode", choices=("verify", "exec"))
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--workers",
        type=int,
        help="Pantograph Lean worker process count for /exec.",
    )
    parser.add_argument("--max-lean-processes-per-env-profile", type=int)
    parser.add_argument("--max-items-per-step-batch", type=int)
    parser.add_argument("--max-tactics-per-step-item", type=int)
    parser.add_argument("--max-attempts-per-step-batch", type=int)
    parser.add_argument("--max-create-items-per-request", type=int)
    parser.add_argument("--max-acquire-timeout-ms", type=int)
    parser.add_argument("--max-step-timeout-ms", type=int)
    parser.add_argument("--max-in-flight-exec-requests", type=int)
    parser.add_argument("--max-queued-exec-requests", type=int)
    parser.add_argument("--max-state-store-bytes", type=int)
    parser.add_argument(
        "--allow-unbounded-exec",
        action="store_true",
        default=None,
    )
    parser.add_argument("--recommended-items-per-step-batch", type=int)
    parser.add_argument("--recommended-in-flight-step-batches", type=int)
    parser.add_argument("--state-store-dir", type=Path)
    parser.add_argument(
        "--single-process",
        dest="single_process",
        action="store_true",
        default=None,
    )
    parser.add_argument(
        "--no-single-process",
        dest="single_process",
        action="store_false",
    )
    return parser


def _validate_settings_as_exec_config(settings: Settings) -> None:
    ExecServerConfig(
        host=settings.host,
        port=settings.port,
        workers=settings.max_pantograph_workers,
        max_lean_processes_per_env_profile=(
            settings.max_lean_processes_per_env_profile
        ),
        max_items_per_step_batch=settings.max_items_per_step_batch,
        max_tactics_per_step_item=settings.max_tactics_per_step_item,
        max_attempts_per_step_batch=settings.max_attempts_per_step_batch,
        max_create_items_per_request=settings.max_create_items_per_request,
        max_acquire_timeout_ms=settings.max_acquire_timeout_ms,
        max_step_timeout_ms=settings.max_step_timeout_ms,
        max_in_flight_exec_requests=settings.max_in_flight_exec_requests,
        max_queued_exec_requests=settings.max_queued_exec_requests,
        max_state_store_bytes=settings.max_state_store_bytes,
        allow_unbounded_exec=settings.allow_unbounded_exec,
        recommended_items_per_step_batch=settings.recommended_items_per_step_batch,
        recommended_in_flight_step_batches=(
            settings.recommended_in_flight_step_batches
        ),
        state_store_dir=settings.state_store_dir,
        single_process=settings.single_process,
    )
