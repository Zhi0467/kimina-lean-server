from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from pydantic import ValidationError

import server.main as main_module
from server.main import create_app
from server.settings import Environment, Settings


def _route_paths(app: FastAPI) -> set[str]:
    return {route.path for route in app.routes if hasattr(route, "path")}


def test_exec_mode_mounts_only_exec_routes() -> None:
    app = create_app(Settings(_env_file=None, mode="exec"))

    paths = _route_paths(app)

    assert "/exec/create_states" in paths
    assert "/exec/step_batch" in paths
    assert "/exec/verify" in paths
    assert "/exec/limits" in paths
    assert "/exec/stats" in paths
    assert "/health" in paths
    assert "/api/check" not in paths
    assert "/verify" not in paths


def test_verify_mode_mounts_only_verify_routes() -> None:
    app = create_app(Settings(_env_file=None, mode="verify"))

    paths = _route_paths(app)

    assert "/api/check" in paths
    assert "/verify" in paths
    assert "/health" in paths
    assert "/exec/create_states" not in paths
    assert "/exec/step_batch" not in paths
    assert "/exec/verify" not in paths
    assert "/exec/limits" not in paths
    assert "/exec/stats" not in paths


def test_invalid_mode_fails_fast() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mode="search")


@pytest.mark.asyncio
async def test_exec_mode_lifespan_constructs_only_exec_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedManager:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("verify manager should not be constructed")

    async def unexpected_connect() -> None:
        raise AssertionError("database should not connect in exec mode")

    monkeypatch.setattr(main_module, "Manager", UnexpectedManager)
    monkeypatch.setattr(main_module.db, "connect", unexpected_connect)

    settings = Settings(
        _env_file=None,
        mode="exec",
        database_url="postgresql://unused",
        environment=Environment.prod,
        state_store_dir=tmp_path / "state-store",
        max_pantograph_workers=1,
    )
    app = create_app(settings)

    async with LifespanManager(app):
        assert app.state.settings is settings
        assert hasattr(app.state, "state_store")
        assert hasattr(app.state, "exec_lifecycle")
        assert hasattr(app.state, "exec_metrics")
        assert hasattr(app.state, "exec_request_limiter")
        assert hasattr(app.state, "pantograph_manager")
        assert not hasattr(app.state, "manager")


@pytest.mark.asyncio
async def test_verify_mode_lifespan_constructs_only_verify_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeManager:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            events.append("manager.constructed")

        async def initialize_repls(self) -> None:
            events.append("manager.initialized")

        async def cleanup(self) -> None:
            events.append("manager.cleaned")

    class UnexpectedPantographManager:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("exec manager should not be constructed")

    async def fake_connect() -> None:
        events.append("db.connected")
        main_module.db.connected = True

    async def fake_disconnect() -> None:
        events.append("db.disconnected")
        main_module.db.connected = False

    monkeypatch.setattr(main_module, "Manager", FakeManager)
    monkeypatch.setattr(main_module, "PantographManager", UnexpectedPantographManager)
    monkeypatch.setattr(main_module.db, "connect", fake_connect)
    monkeypatch.setattr(main_module.db, "disconnect", fake_disconnect)
    main_module.db.connected = False

    settings = Settings(
        _env_file=None,
        mode="verify",
        database_url="postgresql://unused",
        environment=Environment.prod,
        state_store_dir=tmp_path / "unused-state-store",
        max_in_flight_exec_requests=-1,
    )
    app = create_app(settings)

    async with LifespanManager(app):
        assert app.state.settings is settings
        assert hasattr(app.state, "manager")
        assert not hasattr(app.state, "pantograph_manager")
        assert not hasattr(app.state, "state_store")
        assert not hasattr(app.state, "exec_lifecycle")
        assert not hasattr(app.state, "exec_metrics")
        assert not hasattr(app.state, "exec_request_limiter")
        assert events == [
            "db.connected",
            "manager.constructed",
            "manager.initialized",
        ]

    assert events == [
        "db.connected",
        "manager.constructed",
        "manager.initialized",
        "manager.cleaned",
        "db.disconnected",
    ]
