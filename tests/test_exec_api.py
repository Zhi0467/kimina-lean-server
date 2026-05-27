from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from server.main import create_app
from server.routers.exec import cleanup as cleanup_endpoint
from server.schemas_exec import CleanupRequest
from server.settings import Environment, Settings
from server.state_store import StateStore


def _write_state(path: Path, data: bytes = b"state") -> Path:
    path.write_bytes(data)
    return path


def _test_client(tmp_path: Path) -> TestClient:
    settings = Settings(_env_file=None)
    settings.database_url = None
    settings.environment = Environment.prod
    settings.init_repls = {}
    settings.state_store_dir = tmp_path / "state-store"
    return TestClient(create_app(settings))


@pytest.mark.asyncio
async def test_cleanup_endpoint_deletes_by_item_id_unit(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "store", token_factory=lambda: "st_root")
    token = store.put(
        _write_state(tmp_path / "root.bin", b"root"),
        item_id="theorem_42:a0",
        env_profile="env",
        header_hash="header",
    )
    state_path = store.resolve(token).path

    response = await cleanup_endpoint(
        CleanupRequest(item_ids=["theorem_42:a0"]),
        state_store=store,
        _api_key=None,
    )

    assert response.deleted_items[0].item_id == "theorem_42:a0"
    assert response.deleted_items[0].deleted_states == 1
    assert response.deleted_items[0].deleted_bytes == len(b"root")
    assert not state_path.exists()


def test_exec_cleanup_route_deletes_state_files_e2e(tmp_path: Path) -> None:
    with _test_client(tmp_path) as client:
        app = cast(Any, client.app)
        store = cast(StateStore, app.state.state_store)
        token = store.put(
            _write_state(tmp_path / "root.bin", b"root"),
            item_id="theorem_42:a0",
            env_profile="env",
            header_hash="header",
        )
        state_path = store.resolve(token).path

        response = client.post("/exec/cleanup", json={"item_ids": ["theorem_42:a0"]})

        assert response.status_code == 200
        assert response.json() == {
            "deleted_items": [
                {
                    "item_id": "theorem_42:a0",
                    "deleted_states": 1,
                    "deleted_bytes": len(b"root"),
                }
            ]
        }
        assert not state_path.exists()
        assert store.stats().state_count == 0


def test_exec_create_and_step_routes_validate_before_501_e2e(tmp_path: Path) -> None:
    with _test_client(tmp_path) as client:
        create_response = client.post(
            "/exec/create_states",
            json={
                "env_profile": "lean4.29.1_mathlib_x",
                "items": [
                    {
                        "item_id": "theorem_42:a0",
                        "code": "theorem t : True := by\n  sorry",
                    }
                ],
            },
        )
        assert create_response.status_code == 501
        assert create_response.json()["detail"] == (
            "Pantograph create_states is not implemented yet"
        )

        invalid_create_response = client.post(
            "/exec/create_states",
            json={
                "env_profile": "lean4.29.1_mathlib_x",
                "items": [
                    {"item_id": "dup", "code": "theorem t : True := by sorry"},
                    {"item_id": "dup", "code": "theorem t : True := by sorry"},
                ],
            },
        )
        assert invalid_create_response.status_code == 422

        step_response = client.post(
            "/exec/step_batch",
            json={
                "items": [
                    {
                        "node_id": "theorem_42:a0:n0",
                        "state_token": "st_root",
                        "tactics": ["simp"],
                    }
                ]
            },
        )
        assert step_response.status_code == 501
        assert step_response.json()["detail"] == (
            "Pantograph step_batch is not implemented yet"
        )

        invalid_step_response = client.post(
            "/exec/step_batch",
            json={
                "items": [
                    {
                        "node_id": "theorem_42:a0:n0",
                        "state_token": "st_root",
                        "tactics": [],
                    }
                ]
            },
        )
        assert invalid_step_response.status_code == 422
