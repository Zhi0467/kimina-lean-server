from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from server.main import create_app
from server.routers.exec import _return_worker, cleanup as cleanup_endpoint
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
    settings.max_pantograph_workers = 1
    return TestClient(create_app(settings))


class _FakeManager:
    def __init__(self) -> None:
        self.released: list[Any] = []
        self.destroyed: list[Any] = []

    async def release_worker(self, lease: Any) -> None:
        self.released.append(lease)

    async def destroy_worker(self, lease: Any) -> None:
        self.destroyed.append(lease)


@dataclass
class _FakeLease:
    alive: bool

    @property
    def worker(self) -> Any:
        lease = self

        class _Worker:
            def is_alive(self) -> bool:
                return lease.alive

        return _Worker()


@pytest.mark.asyncio
async def test_return_worker_releases_healthy_destroys_dead() -> None:
    manager = _FakeManager()

    healthy = _FakeLease(alive=True)
    await _return_worker(cast(Any, manager), cast(Any, healthy))
    assert manager.released == [healthy]
    assert manager.destroyed == []

    dead = _FakeLease(alive=False)
    await _return_worker(cast(Any, manager), cast(Any, dead))
    assert manager.destroyed == [dead]
    assert manager.released == [healthy]

    # A missing lease (worker never acquired) is a no-op.
    await _return_worker(cast(Any, manager), None)
    assert manager.released == [healthy]
    assert manager.destroyed == [dead]


@pytest.mark.asyncio
async def test_cleanup_endpoint_deletes_by_item_id_unit(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "store", token_factory=lambda: "st_root")
    token = store.put(
        _write_state(tmp_path / "root.bin", b"root"),
        item_id="theorem_42:a0",
        env_profile="env",
        header="import Init",
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
            header="import Init",
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


def test_exec_create_step_and_cleanup_real_pantograph_e2e(tmp_path: Path) -> None:
    with _test_client(tmp_path) as client:
        create_response = client.post(
            "/exec/create_states",
            json={
                "env_profile": "lean_init_test",
                "items": [
                    {
                        "item_id": "theorem_42:a0",
                        "code": "theorem t (n : Nat) : n + 0 = n := by\n  sorry",
                        "timeout_ms": 30000,
                    }
                ],
            },
        )
        assert create_response.status_code == 200
        create_payload = create_response.json()
        assert create_payload["items"][0]["status"] == "open"
        state = create_payload["items"][0]["states"][0]
        assert state["state_token"].startswith("st_")
        assert state["goals"] == ["n : Nat\n⊢ n + 0 = n"]

        step_response = client.post(
            "/exec/step_batch",
            json={
                "items": [
                    {
                        "node_id": "theorem_42:a0:n0",
                        "state_token": state["state_token"],
                        "tactics": ["simp", "rw [Nat.add_comm]", "bad_tactic"],
                        "timeout_ms": 30000,
                    }
                ]
            },
        )
        assert step_response.status_code == 200
        step_item = step_response.json()["items"][0]
        assert step_item["node_id"] == "theorem_42:a0:n0"
        assert [result["status"] for result in step_item["results"]] == [
            "complete",
            "open",
            "error",
        ]
        assert step_item["results"][1]["state_token"].startswith("st_")
        assert step_item["results"][1]["goals"] == ["n : Nat\n⊢ 0 + n = n"]
        assert step_item["results"][2]["messages"]

        cleanup_response = client.post(
            "/exec/cleanup",
            json={"item_ids": ["theorem_42:a0"]},
        )
        assert cleanup_response.status_code == 200
        assert cleanup_response.json()["deleted_items"][0]["deleted_states"] == 2


def test_exec_routes_validate_and_report_invalid_tokens_e2e(tmp_path: Path) -> None:
    with _test_client(tmp_path) as client:
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
        assert step_response.status_code == 200
        assert step_response.json()["items"][0]["results"][0]["status"] == (
            "invalid_state_token"
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
