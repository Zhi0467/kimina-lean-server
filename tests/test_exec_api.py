from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from fastapi.testclient import TestClient

from server.exec_backends import return_worker as _return_worker
from server.main import create_app
from server.routers.exec import cleanup as cleanup_endpoint
from server.schemas_exec import CleanupRequest
from server.settings import Environment, Settings
from server.state_store import StateStore


def _write_state(path: Path, data: bytes = b"state") -> Path:
    path.write_bytes(data)
    return path


def _test_client(
    tmp_path: Path,
    *,
    exec_backend: Literal["pantograph_pool", "pantograph_task"] = "pantograph_pool",
) -> TestClient:
    settings = Settings(_env_file=None)
    settings.database_url = None
    settings.environment = Environment.prod
    settings.init_repls = {}
    settings.state_store_dir = tmp_path / "state-store"
    settings.max_pantograph_workers = 1
    settings.exec_backend = exec_backend
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
class _FakeWorker:
    alive: bool
    gc_error: Exception | None = None
    die_after_gc: bool = False
    gc_calls: int = 0

    def is_alive(self) -> bool:
        return self.alive

    async def agc(self) -> None:
        self.gc_calls += 1
        if self.gc_error is not None:
            raise self.gc_error
        if self.die_after_gc:
            self.alive = False


@dataclass
class _FakeLease:
    worker: _FakeWorker


def test_exec_backend_defaults_to_pool_without_per_profile_cap() -> None:
    settings = Settings(_env_file=None)

    assert settings.exec_backend == "pantograph_pool"
    assert settings.max_lean_processes_per_env_profile == -1


@pytest.mark.asyncio
async def test_return_worker_gc_releases_healthy_destroys_dead() -> None:
    manager = _FakeManager()

    healthy_worker = _FakeWorker(alive=True)
    healthy = _FakeLease(worker=healthy_worker)
    await _return_worker(cast(Any, manager), cast(Any, healthy))
    assert healthy_worker.gc_calls == 1
    assert manager.released == [healthy]
    assert manager.destroyed == []

    dead_worker = _FakeWorker(alive=False)
    dead = _FakeLease(worker=dead_worker)
    await _return_worker(cast(Any, manager), cast(Any, dead))
    assert dead_worker.gc_calls == 0
    assert manager.destroyed == [dead]
    assert manager.released == [healthy]

    gc_failed_worker = _FakeWorker(alive=True, gc_error=RuntimeError("gc failed"))
    gc_failed = _FakeLease(worker=gc_failed_worker)
    await _return_worker(cast(Any, manager), cast(Any, gc_failed))
    assert gc_failed_worker.gc_calls == 1
    assert manager.destroyed == [dead, gc_failed]
    assert manager.released == [healthy]

    died_after_gc_worker = _FakeWorker(alive=True, die_after_gc=True)
    died_after_gc = _FakeLease(worker=died_after_gc_worker)
    await _return_worker(cast(Any, manager), cast(Any, died_after_gc))
    assert died_after_gc_worker.gc_calls == 1
    assert manager.destroyed == [dead, gc_failed, died_after_gc]
    assert manager.released == [healthy]

    # A missing lease (worker never acquired) is a no-op.
    await _return_worker(cast(Any, manager), None)
    assert manager.released == [healthy]
    assert manager.destroyed == [dead, gc_failed, died_after_gc]


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


def test_exec_stats_route_reports_effective_task_profile_cap(tmp_path: Path) -> None:
    with _test_client(tmp_path, exec_backend="pantograph_task") as client:
        response = client.get("/exec/stats")

        assert response.status_code == 200
        payload = response.json()
        assert payload["settings"]["exec_backend"] == "pantograph_task"
        assert payload["settings"]["max_lean_processes_per_env_profile"] == -1
        assert (
            payload["settings"]["effective_max_lean_processes_per_env_profile"]
            == 1
        )
        assert payload["pantograph_pool"]["max_workers_per_env_profile"] == 1


@pytest.mark.parametrize("exec_backend", ["pantograph_pool", "pantograph_task"])
def test_exec_create_step_and_cleanup_real_pantograph_e2e(
    tmp_path: Path,
    exec_backend: Literal["pantograph_pool", "pantograph_task"],
) -> None:
    with _test_client(tmp_path, exec_backend=exec_backend) as client:
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
        store = cast(StateStore, cast(Any, client.app).state.state_store)
        assert store.resolve(state["state_token"]).backend_kind == exec_backend

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
