import os
import re
from enum import Enum
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .exec_server_config import (
    DEFAULT_MAX_STATE_STORE_BYTES,
    DEFAULT_RECOMMENDED_IN_FLIGHT_STEP_BATCHES,
    default_exec_worker_count,
    default_max_in_flight_exec_requests,
    default_max_queued_exec_requests,
)


class Environment(str, Enum):
    dev = "dev"
    prod = "prod"


BASE_DIR = Path(__file__).resolve().parent.parent  # Repository root directory


def _copy_max_pantograph_workers(data: dict[str, object]) -> int:
    value = data["max_pantograph_workers"]
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError("max_pantograph_workers must be an int")


def _default_max_in_flight_exec_requests(data: dict[str, object]) -> int:
    return default_max_in_flight_exec_requests(_copy_max_pantograph_workers(data))


def _default_max_queued_exec_requests(data: dict[str, object]) -> int:
    value = data["max_in_flight_exec_requests"]
    if isinstance(value, int):
        return default_max_queued_exec_requests(value)
    if isinstance(value, str):
        return default_max_queued_exec_requests(int(value))
    raise TypeError("max_in_flight_exec_requests must be an int")


class Settings(BaseSettings):
    mode: Literal["verify", "exec"] = "exec"

    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    api_key: str | None = None

    environment: Environment = Environment.dev

    lean_version: str = "v4.29.1"
    repl_path: Path = BASE_DIR / "repl/.lake/build/bin/repl"
    project_dir: Path = BASE_DIR / "mathlib4"

    max_repls: int = Field(default_factory=default_exec_worker_count)
    max_repl_uses: int = -1
    max_repl_mem: int = 12
    max_wait: int = 60
    max_pantograph_workers: int = Field(default_factory=default_exec_worker_count)
    max_pantograph_worker_uses: int = -1
    pantograph_buffer_limit: int = 2_000_000
    pantograph_worker_startup_timeout_seconds: int = 600
    max_lean_processes_per_env_profile: int = Field(
        default_factory=_copy_max_pantograph_workers
    )
    max_items_per_step_batch: int = 1024
    max_tactics_per_step_item: int = 64
    max_attempts_per_step_batch: int = 8192
    max_create_items_per_request: int = 1024
    max_acquire_timeout_ms: int = 600_000
    max_step_timeout_ms: int = 600_000
    # Axioms ``/exec/verify`` trusts by default when an item omits its own
    # allow-list. The three Lean 4 / Mathlib core axioms — a version-stable set;
    # ``sorryAx`` is intentionally absent, so ``sorry`` is always rejected.
    verify_allowed_axioms: list[str] = Field(
        default_factory=lambda: ["Classical.choice", "propext", "Quot.sound"]
    )
    max_in_flight_exec_requests: int = Field(
        default_factory=_default_max_in_flight_exec_requests
    )
    max_queued_exec_requests: int = Field(
        default_factory=_default_max_queued_exec_requests
    )
    allow_unbounded_exec: bool = False
    recommended_items_per_step_batch: int = Field(
        default_factory=_copy_max_pantograph_workers
    )
    recommended_in_flight_step_batches: int = (
        DEFAULT_RECOMMENDED_IN_FLIGHT_STEP_BATCHES
    )
    item_lifecycle_terminal_retention_seconds: int = 660

    init_repls: dict[str, int] = {}

    state_store_dir: Path = BASE_DIR / ".leanfoundry-state"
    state_ttl_seconds: int = 3600
    state_gc_interval_seconds: int = 300
    max_state_store_bytes: int = DEFAULT_MAX_STATE_STORE_BYTES
    single_process: bool = True

    database_url: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_prefix="LEAN_SERVER_"
    )

    @field_validator("max_repl_mem", mode="before")
    def _parse_max_mem(cls, v: str) -> int:
        if isinstance(v, int):
            return cast(int, v * 1024)
        m = re.fullmatch(r"(\d+)([MmGg])", v)
        if m:
            n, unit = m.groups()
            n = int(n)
            return n if unit.lower() == "m" else n * 1024
        raise ValueError("max_repl_mem must be an int or '<number>[M|G]'")

    @field_validator("max_repls", mode="before")
    @classmethod
    def _parse_max_repls(cls, v: int | str) -> int:
        if isinstance(v, str) and v.strip() == "":
            return os.cpu_count() or 1
        return cast(int, v)


settings = Settings()
