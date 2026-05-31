from __future__ import annotations

import pytest

from server._exec_server_cli import parse_exec_server_config
from server.exec_server_config import ExecServerConfig
from server.settings import Settings


def test_settings_pool_dependent_defaults_follow_pantograph_workers() -> None:
    settings = Settings(_env_file=None, max_pantograph_workers=3)

    assert settings.max_lean_processes_per_env_profile == 3
    assert settings.recommended_items_per_step_batch == 3
    assert settings.max_in_flight_exec_requests == 8
    assert settings.max_queued_exec_requests == 32
    assert settings.max_state_store_bytes == 16 * 2**30
    assert settings.recommended_in_flight_step_batches == 8
    assert settings.single_process is True


def test_exec_server_config_maps_to_settings() -> None:
    config = ExecServerConfig(
        host="127.0.0.1",
        port=8123,
        workers=5,
        max_acquire_timeout_ms=700_000,
        max_step_timeout_ms=800_000,
        max_in_flight_exec_requests=6,
        max_queued_exec_requests=24,
    )

    settings = config.to_settings()

    assert settings.host == "127.0.0.1"
    assert settings.port == 8123
    assert settings.max_pantograph_workers == 5
    assert settings.max_lean_processes_per_env_profile == 5
    assert settings.recommended_items_per_step_batch == 5
    assert settings.max_acquire_timeout_ms == 700_000
    assert settings.max_step_timeout_ms == 800_000
    assert settings.max_in_flight_exec_requests == 6
    assert settings.max_queued_exec_requests == 24


def test_exec_server_config_refuses_unbounded_without_opt_in() -> None:
    with pytest.raises(ValueError, match="allow_unbounded_exec=True"):
        ExecServerConfig(max_state_store_bytes=-1)


def test_exec_server_config_allows_unbounded_with_opt_in() -> None:
    config = ExecServerConfig(
        max_state_store_bytes=-1,
        allow_unbounded_exec=True,
    )

    assert config.max_state_store_bytes == -1


def test_python_module_cli_flags_map_to_config() -> None:
    config = parse_exec_server_config(
        [
            "--host",
            "127.0.0.1",
            "--port",
            "8123",
            "--workers",
            "6",
            "--max-in-flight-exec-requests",
            "7",
            "--max-queued-exec-requests",
            "28",
            "--max-state-store-bytes",
            "4096",
            "--max-acquire-timeout-ms",
            "111",
            "--max-step-timeout-ms",
            "222",
            "--no-single-process",
        ]
    )

    assert config.host == "127.0.0.1"
    assert config.port == 8123
    assert config.workers == 6
    assert config.max_lean_processes_per_env_profile == 6
    assert config.recommended_items_per_step_batch == 6
    assert config.max_in_flight_exec_requests == 7
    assert config.max_queued_exec_requests == 28
    assert config.max_state_store_bytes == 4096
    assert config.max_acquire_timeout_ms == 111
    assert config.max_step_timeout_ms == 222
    assert config.single_process is False
