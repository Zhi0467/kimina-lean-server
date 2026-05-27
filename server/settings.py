import os
import re
from enum import Enum
from pathlib import Path
from typing import cast

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

    lean_version: str = "v4.26.0"
    repl_path: Path = BASE_DIR / "repl/.lake/build/bin/repl"
    project_dir: Path = BASE_DIR / "mathlib4"

    max_repls: int = max((os.cpu_count() or 1) - 1, 1)
    max_repl_uses: int = -1
    max_repl_mem: int = 12
    max_wait: int = 60
    max_pantograph_workers: int = max_repls
    pantograph_buffer_limit: int = 2_000_000

    init_repls: dict[str, int] = {}

    state_store_dir: Path = BASE_DIR / ".leanfoundry-state"
    state_ttl_seconds: int = 3600
    state_gc_interval_seconds: int = 300

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
