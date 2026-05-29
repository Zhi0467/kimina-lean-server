import os
import re
from enum import Enum
from pathlib import Path
from typing import Literal, cast

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    dev = "dev"
    prod = "prod"


BASE_DIR = Path(__file__).resolve().parent.parent  # Repository root directory


class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    api_key: str | None = None

    environment: Environment = Environment.dev

    lean_version: str = "v4.29.1"
    repl_path: Path = BASE_DIR / "repl/.lake/build/bin/repl"
    project_dir: Path = BASE_DIR / "mathlib4"

    max_repls: int = max((os.cpu_count() or 1) - 1, 1)
    max_repl_uses: int = -1
    max_repl_mem: int = 12
    max_wait: int = 60
    max_pantograph_workers: int = max_repls
    max_pantograph_worker_uses: int = -1
    pantograph_buffer_limit: int = 2_000_000
    pantograph_worker_startup_timeout_seconds: int = 600
    exec_backend: Literal[
        "pantograph_pool", "pantograph_task", "pantograph_process_pool"
    ] = "pantograph_process_pool"
    max_items_per_step_batch: int = 16
    max_tactics_per_step_item: int = 8
    max_attempts_per_step_batch: int = 128
    max_items_per_worker_batch: int = 16
    max_parallel_items_per_lean_process: int = 16
    max_lean_processes_per_env_profile: int = -1

    init_repls: dict[str, int] = {}

    state_store_dir: Path = BASE_DIR / ".leanfoundry-state"
    state_ttl_seconds: int = 3600
    state_gc_interval_seconds: int = 300
    max_state_store_bytes: int = -1

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

# When the bounded process-pool backend is selected but no explicit per-env
# cap is configured, fall back to this many worker lanes per (env_profile,
# header). Mathlib's ~3-4 GB of ``.olean`` data is memory-mapped and shared
# across processes, so each extra worker costs only ~0.75 GB of private memory;
# the throughput ceiling is the number of physical cores, not memory. Operators
# should override ``max_lean_processes_per_env_profile`` to match their host.
DEFAULT_PROCESS_POOL_LANES = 4


def effective_max_lean_processes_per_env_profile(settings: Settings) -> int:
    if settings.max_lean_processes_per_env_profile >= 0:
        return settings.max_lean_processes_per_env_profile
    # Unbounded (< 0) is meaningless for backends that must cap process count.
    if settings.exec_backend == "pantograph_task":
        return 1
    if settings.exec_backend == "pantograph_process_pool":
        return DEFAULT_PROCESS_POOL_LANES
    return settings.max_lean_processes_per_env_profile
