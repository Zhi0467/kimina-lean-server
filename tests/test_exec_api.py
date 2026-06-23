from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, cast

import pytest
from fastapi.testclient import TestClient

from server.exec_backends import return_worker as _return_worker
from server.exec_request_limiter import ExecRequestLimiter, ExecRequestRejected
from server.main import create_app
from server.routers.exec import cleanup as cleanup_endpoint
from server.routers.exec import create_states as create_states_endpoint
from server.exec_lifecycle import ItemLifecycleRegistry
from server.exec_metrics import ExecMetrics
from server.pantograph_goal import PantographGoal
from server.pantograph_worker import PantographCreateResult, PantographSavedState
from server.schemas_exec import CleanupRequest, CreateStatesRequest
from server.settings import Environment, Settings
from server.state_store import StateStore


def _write_state(path: Path, data: bytes = b"state") -> Path:
    path.write_bytes(data)
    return path


def _test_client(tmp_path: Path, **overrides: Any) -> TestClient:
    settings = Settings(
        _env_file=None,
        database_url=None,
        environment=Environment.prod,
        init_repls={},
        state_store_dir=tmp_path / "state-store",
        max_pantograph_workers=1,
        **overrides,
    )
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
    worker: Any


class _BlockingCreateWorker:
    def __init__(self, *, started: Any, release: Any, state_path: Path) -> None:
        self.started = started
        self.release = release
        self.state_path = state_path
        self.timeout_seconds: int | None = None
        self.gc_calls = 0
        self.create_calls = 0

    def set_timeout_seconds(self, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds

    async def create_states_from_code(
        self,
        code: str,
        *,
        state_dir: Path,
        debug: bool = False,
    ) -> PantographCreateResult:
        _ = debug
        self.create_calls += 1
        self.started.set()
        await self.release.wait()
        self.state_path.write_bytes(b"root")
        return PantographCreateResult(
            status="open",
            states=[
                PantographSavedState(
                    path=self.state_path,
                    goals=[PantographGoal(target="True", pretty="⊢ True")],
                )
            ],
        )

    def is_alive(self) -> bool:
        return True

    async def agc(self) -> None:
        self.gc_calls += 1


class _BlockingCreateManager:
    def __init__(self, worker: _BlockingCreateWorker) -> None:
        self.worker = worker
        self.released: list[Any] = []
        self.destroyed: list[Any] = []

    async def get_worker(
        self,
        *,
        env_profile: str,
        header: str,
        timeout: float,
    ) -> Any:
        return _FakeLease(worker=self.worker)

    async def release_worker(self, lease: Any) -> None:
        self.released.append(lease)

    async def destroy_worker(self, lease: Any) -> None:
        self.destroyed.append(lease)


class _DelayedCreateManager(_BlockingCreateManager):
    def __init__(
        self,
        worker: _BlockingCreateWorker,
        *,
        acquire_started: asyncio.Event,
        release_acquire: asyncio.Event,
    ) -> None:
        super().__init__(worker)
        self.acquire_started = acquire_started
        self.release_acquire = release_acquire

    async def get_worker(
        self,
        *,
        env_profile: str,
        header: str,
        timeout: float,
    ) -> Any:
        self.acquire_started.set()
        await self.release_acquire.wait()
        return _FakeLease(worker=self.worker)


class _RejectingLimiter:
    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        raise ExecRequestRejected("exec request queue is full")
        yield


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
        lifecycle=ItemLifecycleRegistry(),
        metrics=ExecMetrics(),
        _api_key=None,
    )

    assert response.deleted_items[0].item_id == "theorem_42:a0"
    assert response.deleted_items[0].status == "deleted"
    assert response.deleted_items[0].deleted_states == 1
    assert response.deleted_items[0].deleted_bytes == len(b"root")
    assert not state_path.exists()


@pytest.mark.asyncio
async def test_cleanup_endpoint_defers_while_lifecycle_is_active(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "store", token_factory=lambda: "st_root")
    lifecycle = ItemLifecycleRegistry()
    token = store.put(
        _write_state(tmp_path / "root.bin", b"root"),
        item_id="theorem_42:a0",
        env_profile="env",
        header="import Init",
        header_hash="header",
    )
    state_path = store.resolve(token).path
    lifecycle.begin("theorem_42:a0")

    deferred = await cleanup_endpoint(
        CleanupRequest(item_ids=["theorem_42:a0"]),
        state_store=store,
        lifecycle=lifecycle,
        metrics=ExecMetrics(),
        _api_key=None,
    )

    assert deferred.deleted_items[0].status == "deferred"
    assert deferred.deleted_items[0].reason == "in_flight"
    assert deferred.deleted_items[0].deleted_states == 0
    assert state_path.exists()

    lifecycle.finish("theorem_42:a0")
    deleted = await cleanup_endpoint(
        CleanupRequest(item_ids=["theorem_42:a0"]),
        state_store=store,
        lifecycle=lifecycle,
        metrics=ExecMetrics(),
        _api_key=None,
    )

    assert deleted.deleted_items[0].status == "deleted"
    assert not state_path.exists()


@pytest.mark.asyncio
async def test_create_states_discards_outputs_when_cancelled_before_promotion(
    tmp_path: Path,
) -> None:
    started: asyncio.Event = asyncio.Event()
    release: asyncio.Event = asyncio.Event()
    scratch_path = tmp_path / "store" / "pg_root.bin"
    worker = _BlockingCreateWorker(
        started=started,
        release=release,
        state_path=scratch_path,
    )
    manager = _BlockingCreateManager(worker)
    store = StateStore(tmp_path / "store")
    lifecycle = ItemLifecycleRegistry()

    task = asyncio.create_task(
        create_states_endpoint(
            CreateStatesRequest(
                env_profile="env",
                items=[
                    {
                        "item_id": "theorem_42:a0",
                        "code": "theorem t : True := by\n  sorry",
                        "timeout_ms": 1000,
                    }
                ],
            ),
            state_store=store,
            pantograph_manager=cast(Any, manager),
            lifecycle=lifecycle,
            limiter=ExecRequestLimiter(),
            metrics=ExecMetrics(),
            settings=Settings(_env_file=None),
            _api_key=None,
        )
    )
    await started.wait()
    lifecycle.cancel("theorem_42:a0")
    release.set()
    response = await task

    assert response.items[0].status == "cancelled"
    assert response.items[0].states == []
    assert store.count_by_item_id("theorem_42:a0") == 0
    assert not scratch_path.exists()
    assert manager.released


@pytest.mark.asyncio
async def test_create_states_cancelled_while_waiting_for_worker_does_not_run_lean(
    tmp_path: Path,
) -> None:
    acquire_started = asyncio.Event()
    release_acquire = asyncio.Event()
    worker_started = asyncio.Event()
    worker_release = asyncio.Event()
    worker = _BlockingCreateWorker(
        started=worker_started,
        release=worker_release,
        state_path=tmp_path / "store" / "pg_root.bin",
    )
    manager = _DelayedCreateManager(
        worker,
        acquire_started=acquire_started,
        release_acquire=release_acquire,
    )
    store = StateStore(tmp_path / "store")
    lifecycle = ItemLifecycleRegistry()

    task = asyncio.create_task(
        create_states_endpoint(
            CreateStatesRequest(
                env_profile="env",
                items=[
                    {
                        "item_id": "theorem_42:a0",
                        "code": "theorem t : True := by\n  sorry",
                        "timeout_ms": 1000,
                    }
                ],
            ),
            state_store=store,
            pantograph_manager=cast(Any, manager),
            lifecycle=lifecycle,
            limiter=ExecRequestLimiter(),
            metrics=ExecMetrics(),
            settings=Settings(_env_file=None),
            _api_key=None,
        )
    )
    await acquire_started.wait()
    lifecycle.cancel("theorem_42:a0")
    release_acquire.set()
    response = await task

    assert response.items[0].status == "cancelled"
    assert worker.create_calls == 0
    assert not worker_started.is_set()
    assert store.count_by_item_id("theorem_42:a0") == 0
    assert manager.released


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
                    "status": "deleted",
                    "in_flight": 0,
                    "pinned_states": 0,
                    "deleted_states": 1,
                    "deleted_bytes": len(b"root"),
                }
            ]
        }
        assert not state_path.exists()
        assert store.stats().state_count == 0


def test_exec_cleanup_route_defers_when_state_is_pinned(tmp_path: Path) -> None:
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
        store.resolve_and_pin(token)
        try:
            response = client.post(
                "/exec/cleanup",
                json={"item_ids": ["theorem_42:a0"]},
            )
        finally:
            store.unpin(token)

        assert response.status_code == 200
        assert response.json() == {
            "deleted_items": [
                {
                    "item_id": "theorem_42:a0",
                    "status": "deferred",
                    "reason": "pinned",
                    "in_flight": 0,
                    "pinned_states": 1,
                    "deleted_states": 0,
                    "deleted_bytes": 0,
                }
            ]
        }
        assert store.count_by_item_id("theorem_42:a0") == 1


def test_exec_cancel_and_limits_routes_e2e(tmp_path: Path) -> None:
    with _test_client(tmp_path) as client:
        cancel_response = client.post(
            "/exec/cancel",
            json={"item_ids": ["theorem_42:a0"]},
        )
        assert cancel_response.status_code == 200
        assert cancel_response.json()["items"] == [
            {"item_id": "theorem_42:a0", "status": "drained", "in_flight": 0}
        ]

        limits_response = client.get("/exec/limits")
        assert limits_response.status_code == 200
        payload = limits_response.json()
        assert payload["max_pantograph_workers"] == 1
        assert payload["max_lean_processes_per_env_profile"] == 1
        assert payload["max_in_flight_exec_requests"] == 1
        assert payload["max_queued_exec_requests"] == 4
        assert payload["max_state_store_bytes"] == 16 * 2**30
        assert payload["recommended_items_per_step_batch"] == 1
        assert payload["recommended_in_flight_step_batches"] == 8
        assert payload["single_process"] is True
        assert payload["same_item_id_pipelining"] is False

        stats_response = client.get("/exec/stats")
        assert stats_response.status_code == 200
        stats_payload = stats_response.json()
        assert stats_payload["state_store"]["state_count"] == 0
        assert stats_payload["worker_pool"]["max_workers"] == 1
        assert stats_payload["lifecycle"]["drained_items"] == 1
        assert stats_payload["metrics"]["endpoint_requests"]["cancel"] == 1
        assert stats_payload["metrics"]["cancel_status_counts"]["drained"] == 1


def test_exec_app_refuses_unbounded_caps_without_opt_in(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=None,
        environment=Environment.prod,
        init_repls={},
        state_store_dir=tmp_path / "state-store",
        max_in_flight_exec_requests=-1,
    )

    with pytest.raises(ValueError, match="allow_unbounded_exec=True"):
        with TestClient(create_app(settings)):
            pass


def test_exec_single_process_lock_blocks_second_app(tmp_path: Path) -> None:
    first = Settings(
        _env_file=None,
        database_url=None,
        environment=Environment.prod,
        init_repls={},
        state_store_dir=tmp_path / "state-store",
    )
    second = Settings(
        _env_file=None,
        database_url=None,
        environment=Environment.prod,
        init_repls={},
        state_store_dir=tmp_path / "state-store",
    )

    with TestClient(create_app(first)):
        with pytest.raises(RuntimeError, match="already using state_store_dir"):
            with TestClient(create_app(second)):
                pass

    with TestClient(create_app(second)):
        pass


def test_exec_routes_return_503_when_request_limiter_rejects(
    tmp_path: Path,
) -> None:
    with _test_client(tmp_path) as client:
        app = cast(Any, client.app)
        app.state.exec_request_limiter = _RejectingLimiter()

        create_response = client.post(
            "/exec/create_states",
            json={
                "env_profile": "lean_init_test",
                "items": [
                    {
                        "item_id": "theorem_42:a0",
                        "code": "theorem t : True := by\n  sorry",
                    }
                ],
            },
        )
        assert create_response.status_code == 503
        assert "exec request queue is full" in create_response.text

        step_response = client.post(
            "/exec/step_batch",
            json={
                "items": [
                    {
                        "node_id": "theorem_42:a0:n0",
                        "state_token": "st_missing",
                        "tactics": ["simp"],
                    }
                ]
            },
        )
        assert step_response.status_code == 503
        assert "exec request queue is full" in step_response.text


def test_exec_routes_reject_oversized_create_before_worker_leasing(
    tmp_path: Path,
) -> None:
    with _test_client(tmp_path, max_create_items_per_request=1) as client:
        response = client.post(
            "/exec/create_states",
            json={
                "env_profile": "lean_init_test",
                "items": [
                    {"item_id": "a", "code": "theorem a : True := by trivial"},
                    {"item_id": "b", "code": "theorem b : True := by trivial"},
                ],
            },
        )

        assert response.status_code == 422
        assert "max_create_items_per_request=1" in response.text


def test_exec_routes_reject_oversized_step_before_token_resolution(
    tmp_path: Path,
) -> None:
    with _test_client(tmp_path, max_items_per_step_batch=1) as client:
        response = client.post(
            "/exec/step_batch",
            json={
                "items": [
                    {"node_id": "n0", "state_token": "st_missing_0", "tactics": ["simp"]},
                    {"node_id": "n1", "state_token": "st_missing_1", "tactics": ["simp"]},
                ]
            },
        )

        assert response.status_code == 422
        assert "max_items_per_step_batch=1" in response.text


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
                        "debug": True,
                    }
                ],
            },
        )
        assert create_response.status_code == 200
        create_payload = create_response.json()
        create_item = create_payload["items"][0]
        assert create_item["status"] == "open"
        assert create_item["diagnostics"]["acquire_ms"] >= 0
        assert create_item["diagnostics"]["lean_ms"] >= 0
        assert create_item["diagnostics"]["debug"]["memory_max"] > 0
        state = create_item["states"][0]
        assert state["state_token"].startswith("st_")
        assert len(state["goals"]) == 1
        goal = state["goals"][0]
        assert goal["target"] == "n + 0 = n"
        assert goal["pretty"] == "n : Nat\n⊢ n + 0 = n"
        assert goal["hypotheses"] == [{"type": "Nat", "name": "n"}]
        assert goal["sibling_dep"] == []

        step_response = client.post(
            "/exec/step_batch",
            json={
                "items": [
                    {
                        "node_id": "theorem_42:a0:n0",
                        "state_token": state["state_token"],
                        "tactics": ["simp", "rw [Nat.add_comm]", "bad_tactic"],
                        "timeout_ms": 30000,
                        "debug": True,
                    }
                ]
            },
        )
        assert step_response.status_code == 200
        step_item = step_response.json()["items"][0]
        assert step_item["node_id"] == "theorem_42:a0:n0"
        assert step_item["diagnostics"]["acquire_ms"] >= 0
        assert step_item["diagnostics"]["lean_ms"] >= 0
        assert step_item["diagnostics"]["debug"]["memory_max"] > 0
        assert [result["status"] for result in step_item["results"]] == [
            "complete",
            "open",
            "error",
        ]
        assert step_item["results"][1]["state_token"].startswith("st_")
        open_goals = step_item["results"][1]["goals"]
        assert len(open_goals) == 1
        assert open_goals[0]["target"] == "0 + n = n"
        assert open_goals[0]["pretty"] == "n : Nat\n⊢ 0 + n = n"
        assert open_goals[0]["sibling_dep"] == []
        bad_tactic_message = step_item["results"][2]["messages"][0]
        assert bad_tactic_message["severity"] == "error"
        assert "unknown tactic" in bad_tactic_message["data"]
        assert bad_tactic_message["pos"] == {"line": 1, "col": 1}

        cleanup_response = client.post(
            "/exec/cleanup",
            json={"item_ids": ["theorem_42:a0"]},
        )
        assert cleanup_response.status_code == 200
        assert cleanup_response.json()["deleted_items"][0]["status"] == "deleted"
        assert cleanup_response.json()["deleted_items"][0]["deleted_states"] == 2


def test_exec_create_bad_snippet_returns_positioned_message(tmp_path: Path) -> None:
    with _test_client(tmp_path) as client:
        response = client.post(
            "/exec/create_states",
            json={
                "env_profile": "lean_init_test",
                "items": [
                    {
                        "item_id": "bad:a0",
                        "code": "import Init\n\ntheorem t : True := by\n  exact False",
                        "timeout_ms": 30000,
                    }
                ],
            },
        )

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["status"] == "error"
        assert item["diagnostics"]["acquire_ms"] >= 0
        assert item["diagnostics"]["lean_ms"] >= 0
        message = item["messages"][0]
        assert message["severity"] == "error"
        assert "Type mismatch" in message["data"]
        assert message["pos"] == {"line": 4, "col": 2}


def test_exec_verify_accepts_clean_rejects_sorry_and_axioms(
    tmp_path: Path,
) -> None:
    with _test_client(tmp_path) as client:
        response = client.post(
            "/exec/verify",
            json={
                "env_profile": "lean_init_test",
                "items": [
                    {
                        "item_id": "good:a0",
                        "code": "theorem clean : True := by\n  trivial",
                        "theorem_name": "clean",
                        "timeout_ms": 30000,
                        "debug": True,
                    },
                    {
                        "item_id": "sorry:a0",
                        "code": "theorem withSorry : True := by\n  sorry",
                        "theorem_name": "withSorry",
                        "timeout_ms": 30000,
                    },
                    {
                        "item_id": "axiom:a0",
                        "code": (
                            "axiom bad : False\n"
                            "theorem usesBad : False := by\n"
                            "  exact bad"
                        ),
                        "theorem_name": "usesBad",
                        "timeout_ms": 30000,
                    },
                ],
            },
        )

        assert response.status_code == 200
        items = {item["item_id"]: item for item in response.json()["items"]}

        assert items["good:a0"]["status"] == "accepted"
        assert items["good:a0"]["axioms"] == []
        assert items["good:a0"]["diagnostics"]["debug"]["memory_max"] > 0

        assert items["sorry:a0"]["status"] == "rejected"
        assert "sorryAx" in items["sorry:a0"]["axioms"]

        assert items["axiom:a0"]["status"] == "rejected"
        assert items["axiom:a0"]["axioms"] == ["bad"]
        assert any(
            message["data"] == "disallowed axioms: bad"
            for message in items["axiom:a0"]["messages"]
        )


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
