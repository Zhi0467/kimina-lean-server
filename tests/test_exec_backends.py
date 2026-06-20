from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from server.exec_backend_utils import distribute_items_across_lanes
from server.exec_backends import StepBatchBackendConfig, execute_step_batch_request
from server.exec_lifecycle import ItemLifecycleRegistry
from server.exec_metrics import ExecMetrics
from server.pantograph_goal import PantographGoal
from server.pantograph_manager import PantographManager, header_hash
from server.pantograph_worker import PantographStepResult
from server.routers.exec import cleanup as cleanup_endpoint
from server.schemas_exec import CleanupRequest, StepBatchItem, StepBatchRequest
from server.state_store import StateStore


def _write_state(path: Path, data: bytes = b"state") -> Path:
    path.write_bytes(data)
    return path


@dataclass
class _FakeWorker:
    step_calls: list[tuple[Path, list[str], list[int] | None]] = field(
        default_factory=list
    )
    timeout_seconds: int | None = None
    agc_calls: int = 0

    def set_timeout_seconds(self, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds

    async def step_state_with_tactics(
        self,
        state_path: Path,
        tactics: list[str],
        *,
        state_dir: Path,
        goal_id: int | None = None,
        auto_resume: bool | None = None,
        goal_group: list[int] | None = None,
        debug: bool = False,
    ) -> list[PantographStepResult]:
        _ = debug
        self.step_calls.append((state_path, list(tactics), goal_group))
        return [
            PantographStepResult(tactic=tactic, status="complete")
            for tactic in tactics
        ]

    def is_alive(self) -> bool:
        return True

    async def agc(self) -> None:
        self.agc_calls += 1

    async def aclose(self) -> None:
        return None


class _BlockingWorker(_FakeWorker):
    def __init__(
        self,
        *,
        started: asyncio.Event,
        release: asyncio.Event,
        result_status: str = "complete",
    ) -> None:
        super().__init__()
        self.started = started
        self.release = release
        self.result_status = result_status

    async def step_state_with_tactics(
        self,
        state_path: Path,
        tactics: list[str],
        *,
        state_dir: Path,
        goal_id: int | None = None,
        auto_resume: bool | None = None,
        goal_group: list[int] | None = None,
        debug: bool = False,
    ) -> list[PantographStepResult]:
        _ = debug
        self.step_calls.append((state_path, list(tactics), goal_group))
        self.started.set()
        await self.release.wait()
        if self.result_status == "open":
            return [
                PantographStepResult(
                    tactic=tactic,
                    status="open",
                    state_path=_write_state(state_dir / f"child_{index}.bin", b"child"),
                    goals=[PantographGoal(target="child", pretty="⊢ child")],
                )
                for index, tactic in enumerate(tactics)
            ]
        return [
            PantographStepResult(tactic=tactic, status="complete")
            for tactic in tactics
        ]


class _FakeWorkerFactory:
    def __init__(self) -> None:
        self.workers: list[_FakeWorker] = []

    async def __call__(
        self,
        imports: list[str],
        project_path: str | None,
        timeout_seconds: int,
        buffer_limit: int | None,
    ) -> _FakeWorker:
        worker = _FakeWorker()
        self.workers.append(worker)
        return worker


class _BlockingWorkerFactory:
    def __init__(self, worker: _BlockingWorker) -> None:
        self.worker = worker

    async def __call__(
        self,
        imports: list[str],
        project_path: str | None,
        timeout_seconds: int,
        buffer_limit: int | None,
    ) -> _BlockingWorker:
        return self.worker


class _DelayedWorkerFactory:
    def __init__(
        self,
        worker: _FakeWorker,
        *,
        acquire_started: asyncio.Event,
        release_acquire: asyncio.Event,
    ) -> None:
        self.worker = worker
        self.acquire_started = acquire_started
        self.release_acquire = release_acquire

    async def __call__(
        self,
        imports: list[str],
        project_path: str | None,
        timeout_seconds: int,
        buffer_limit: int | None,
    ) -> _FakeWorker:
        self.acquire_started.set()
        await self.release_acquire.wait()
        return self.worker


def _config(max_lanes: int) -> StepBatchBackendConfig:
    return StepBatchBackendConfig(
        max_items_per_step_batch=16,
        max_tactics_per_step_item=8,
        max_attempts_per_step_batch=128,
        max_lean_processes_per_env_profile=max_lanes,
        max_acquire_timeout_ms=10_000,
        max_step_timeout_ms=10_000,
    )


async def test_process_pool_reuses_one_worker_for_single_lane(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "store")
    header = "import Init"
    tokens = [
        store.put(
            _write_state(tmp_path / f"state_{index}.bin"),
            item_id=f"item_{index}",
            env_profile="env",
            header=header,
            header_hash=header_hash(header),
        )
        for index in range(3)
    ]
    factory = _FakeWorkerFactory()
    lifecycle = ItemLifecycleRegistry()
    manager = PantographManager(
        max_workers=4,
        max_workers_per_env_profile=1,
        worker_factory=factory,
    )

    response = await execute_step_batch_request(
        StepBatchRequest(
            items=[
                StepBatchItem(
                    node_id=f"node_{index}",
                    state_token=token,
                    tactics=["simp", "rfl"],
                    timeout_ms=1000,
                )
                for index, token in enumerate(tokens)
            ]
        ),
        state_store=store,
        pantograph_manager=manager,
        lifecycle=lifecycle,
        config=_config(max_lanes=1),
    )

    assert len(factory.workers) == 1
    assert len(factory.workers[0].step_calls) == 3
    assert [
        [result.status for result in item.results]
        for item in response.items
    ] == [["complete", "complete"]] * 3
    assert [lifecycle.snapshot(f"item_{index}").in_flight for index in range(3)] == [
        0,
        0,
        0,
    ]

    await manager.cleanup()


async def test_step_batch_forwards_goal_group_to_worker(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "store")
    header = "import Init"
    token = store.put(
        _write_state(tmp_path / "state.bin"),
        item_id="item_0",
        env_profile="env",
        header=header,
        header_hash=header_hash(header),
    )
    factory = _FakeWorkerFactory()
    lifecycle = ItemLifecycleRegistry()
    manager = PantographManager(max_workers=1, worker_factory=factory)

    await execute_step_batch_request(
        StepBatchRequest(
            items=[
                StepBatchItem(
                    node_id="item_0:n0",
                    state_token=token,
                    tactics=["rfl"],
                    goal_group=[0, 1],
                    timeout_ms=1000,
                )
            ]
        ),
        state_store=store,
        pantograph_manager=manager,
        lifecycle=lifecycle,
        config=_config(max_lanes=1),
    )

    assert factory.workers[0].step_calls[0][2] == [0, 1]
    await manager.cleanup()


async def test_process_pool_splits_incompatible_headers(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "store")
    tokens = []
    for index, header in enumerate(["import Init", "import Mathlib"]):
        tokens.append(
            store.put(
                _write_state(tmp_path / f"state_{index}.bin"),
                item_id=f"item_{index}",
                env_profile="env",
                header=header,
                header_hash=header_hash(header),
            )
        )
    factory = _FakeWorkerFactory()
    lifecycle = ItemLifecycleRegistry()
    manager = PantographManager(max_workers=4, worker_factory=factory)

    await execute_step_batch_request(
        StepBatchRequest(
            items=[
                StepBatchItem(
                    node_id=f"node_{index}",
                    state_token=token,
                    tactics=["simp"],
                    timeout_ms=1000,
                )
                for index, token in enumerate(tokens)
            ]
        ),
        state_store=store,
        pantograph_manager=manager,
        lifecycle=lifecycle,
        config=_config(max_lanes=1),
    )

    assert len(factory.workers) == 2
    assert [len(worker.step_calls) for worker in factory.workers] == [1, 1]

    await manager.cleanup()


async def test_step_batch_returns_cancelled_for_cancelled_item(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "store")
    header = "import Init"
    token = store.put(
        _write_state(tmp_path / "state.bin"),
        item_id="item_0",
        env_profile="env",
        header=header,
        header_hash=header_hash(header),
    )
    factory = _FakeWorkerFactory()
    lifecycle = ItemLifecycleRegistry()
    lifecycle.cancel("item_0")
    manager = PantographManager(max_workers=1, worker_factory=factory)

    response = await execute_step_batch_request(
        StepBatchRequest(
            items=[
                StepBatchItem(
                    node_id="item_0:n0",
                    state_token=token,
                    tactics=["simp", "rfl"],
                    timeout_ms=1000,
                )
            ]
        ),
        state_store=store,
        pantograph_manager=manager,
        lifecycle=lifecycle,
        config=_config(max_lanes=1),
    )

    assert [result.status for result in response.items[0].results] == [
        "cancelled",
        "cancelled",
    ]
    assert factory.workers == []


async def test_step_batch_cancelled_while_waiting_for_worker_does_not_run_lean(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "store")
    header = "import Init"
    token = store.put(
        _write_state(tmp_path / "state.bin"),
        item_id="item_0",
        env_profile="env",
        header=header,
        header_hash=header_hash(header),
    )
    acquire_started = asyncio.Event()
    release_acquire = asyncio.Event()
    worker = _FakeWorker()
    lifecycle = ItemLifecycleRegistry()
    manager = PantographManager(
        max_workers=1,
        worker_factory=_DelayedWorkerFactory(
            worker,
            acquire_started=acquire_started,
            release_acquire=release_acquire,
        ),
    )

    task = asyncio.create_task(
        execute_step_batch_request(
            StepBatchRequest(
                items=[
                    StepBatchItem(
                        node_id="item_0:n0",
                        state_token=token,
                        tactics=["simp", "rfl"],
                        timeout_ms=1000,
                    )
                ]
            ),
            state_store=store,
            pantograph_manager=manager,
            lifecycle=lifecycle,
            config=_config(max_lanes=1),
        )
    )
    await acquire_started.wait()
    lifecycle.cancel("item_0")
    release_acquire.set()
    response = await task

    assert [result.status for result in response.items[0].results] == [
        "cancelled",
        "cancelled",
    ]
    assert worker.step_calls == []
    await manager.cleanup()


async def test_step_batch_reports_acquire_timeout_as_overloaded(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "store")
    header = "import Init"
    token = store.put(
        _write_state(tmp_path / "state.bin"),
        item_id="item_0",
        env_profile="env",
        header=header,
        header_hash=header_hash(header),
    )
    factory = _FakeWorkerFactory()
    lifecycle = ItemLifecycleRegistry()
    manager = PantographManager(max_workers=1, worker_factory=factory)
    lease = await manager.get_worker(env_profile="env", header=header, timeout=1)

    try:
        response = await execute_step_batch_request(
            StepBatchRequest(
                items=[
                    StepBatchItem(
                        node_id="item_0:n0",
                        state_token=token,
                        tactics=["simp"],
                        acquire_timeout_ms=1,
                        step_timeout_ms=1000,
                    )
                ]
            ),
            state_store=store,
            pantograph_manager=manager,
            lifecycle=lifecycle,
            config=_config(max_lanes=1),
        )
    finally:
        await manager.release_worker(lease)
        await manager.cleanup()

    assert response.items[0].results[0].status == "overloaded"
    assert store.count_by_item_id("item_0") == 1
    stats = await manager.stats()
    assert stats.lease_timeouts == 1
    assert stats.lease_requests == 2


async def test_cancel_skips_later_items_in_same_lane(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "store")
    header = "import Init"
    tokens = [
        store.put(
            _write_state(tmp_path / f"state_{index}.bin"),
            item_id="item_0",
            env_profile="env",
            header=header,
            header_hash=header_hash(header),
        )
        for index in range(2)
    ]
    started = asyncio.Event()
    release = asyncio.Event()
    worker = _BlockingWorker(started=started, release=release)
    lifecycle = ItemLifecycleRegistry()
    manager = PantographManager(
        max_workers=1,
        worker_factory=_BlockingWorkerFactory(worker),
    )

    task = asyncio.create_task(
        execute_step_batch_request(
            StepBatchRequest(
                items=[
                    StepBatchItem(
                        node_id=f"item_0:n{index}",
                        state_token=token,
                        tactics=["simp"],
                        timeout_ms=1000,
                    )
                    for index, token in enumerate(tokens)
                ]
            ),
            state_store=store,
            pantograph_manager=manager,
            lifecycle=lifecycle,
            config=_config(max_lanes=1),
        )
    )
    await started.wait()
    assert lifecycle.cancel("item_0").status == "cancelling"
    release.set()
    response = await task

    assert response.items[0].results[0].status == "cancelled"
    assert response.items[1].results[0].status == "cancelled"
    assert lifecycle.snapshot("item_0").status == "drained"
    await manager.cleanup()


async def test_cancel_during_running_item_discards_open_child(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "store")
    header = "import Init"
    token = store.put(
        _write_state(tmp_path / "state.bin"),
        item_id="item_0",
        env_profile="env",
        header=header,
        header_hash=header_hash(header),
    )
    started = asyncio.Event()
    release = asyncio.Event()
    worker = _BlockingWorker(
        started=started,
        release=release,
        result_status="open",
    )
    lifecycle = ItemLifecycleRegistry()
    manager = PantographManager(
        max_workers=1,
        worker_factory=_BlockingWorkerFactory(worker),
    )

    task = asyncio.create_task(
        execute_step_batch_request(
            StepBatchRequest(
                items=[
                    StepBatchItem(
                        node_id="item_0:n0",
                        state_token=token,
                        tactics=["rw [Nat.add_comm]"],
                        timeout_ms=1000,
                    )
                ]
            ),
            state_store=store,
            pantograph_manager=manager,
            lifecycle=lifecycle,
            config=_config(max_lanes=1),
        )
    )
    await started.wait()
    assert lifecycle.cancel("item_0").status == "cancelling"
    release.set()
    response = await task

    result = response.items[0].results[0]
    assert result.status == "cancelled"
    assert result.state_token is None
    assert store.count_by_item_id("item_0") == 1
    assert not list((tmp_path / "store").glob("child_*.bin"))
    await manager.cleanup()


async def test_cancel_during_running_item_does_not_stop_unrelated_lane_work(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "store")
    header = "import Init"
    tokens = [
        store.put(
            _write_state(tmp_path / f"state_{index}.bin"),
            item_id=item_id,
            env_profile="env",
            header=header,
            header_hash=header_hash(header),
        )
        for index, item_id in enumerate(["item_0", "item_1"])
    ]
    started = asyncio.Event()
    release = asyncio.Event()
    worker = _BlockingWorker(started=started, release=release)
    lifecycle = ItemLifecycleRegistry()
    manager = PantographManager(
        max_workers=1,
        worker_factory=_BlockingWorkerFactory(worker),
    )

    task = asyncio.create_task(
        execute_step_batch_request(
            StepBatchRequest(
                items=[
                    StepBatchItem(
                        node_id="item_0:n0",
                        state_token=tokens[0],
                        tactics=["simp"],
                        timeout_ms=1000,
                    ),
                    StepBatchItem(
                        node_id="item_1:n0",
                        state_token=tokens[1],
                        tactics=["rfl"],
                        timeout_ms=1000,
                    ),
                ]
            ),
            state_store=store,
            pantograph_manager=manager,
            lifecycle=lifecycle,
            config=_config(max_lanes=1),
        )
    )
    await started.wait()
    lifecycle.cancel("item_0")
    release.set()
    response = await task

    assert response.items[0].results[0].status == "cancelled"
    assert response.items[1].results[0].status == "complete"
    assert [call[1] for call in worker.step_calls] == [["simp"], ["rfl"]]
    await manager.cleanup()


async def test_cleanup_defers_while_step_batch_is_creating_child(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "store")
    header = "import Init"
    token = store.put(
        _write_state(tmp_path / "state.bin", b"parent"),
        item_id="item_0",
        env_profile="env",
        header=header,
        header_hash=header_hash(header),
    )
    started = asyncio.Event()
    release = asyncio.Event()
    worker = _BlockingWorker(
        started=started,
        release=release,
        result_status="open",
    )
    lifecycle = ItemLifecycleRegistry()
    manager = PantographManager(
        max_workers=1,
        worker_factory=_BlockingWorkerFactory(worker),
    )

    task = asyncio.create_task(
        execute_step_batch_request(
            StepBatchRequest(
                items=[
                    StepBatchItem(
                        node_id="item_0:n0",
                        state_token=token,
                        tactics=["rw [Nat.add_comm]"],
                        timeout_ms=1000,
                    )
                ]
            ),
            state_store=store,
            pantograph_manager=manager,
            lifecycle=lifecycle,
            config=_config(max_lanes=1),
        )
    )
    await started.wait()

    deferred = await cleanup_endpoint(
        CleanupRequest(item_ids=["item_0"]),
        state_store=store,
        lifecycle=lifecycle,
        metrics=ExecMetrics(),
        _api_key=None,
    )

    assert deferred.deleted_items[0].status == "deferred"
    assert deferred.deleted_items[0].reason == "in_flight"
    assert deferred.deleted_items[0].deleted_states == 0
    assert store.count_by_item_id("item_0") == 1

    release.set()
    response = await task
    child_token = response.items[0].results[0].state_token
    assert response.items[0].results[0].status == "open"
    assert child_token is not None
    assert store.count_by_item_id("item_0") == 2

    deleted = await cleanup_endpoint(
        CleanupRequest(item_ids=["item_0"]),
        state_store=store,
        lifecycle=lifecycle,
        metrics=ExecMetrics(),
        _api_key=None,
    )

    assert deleted.deleted_items[0].status == "deleted"
    assert deleted.deleted_items[0].deleted_states == 2
    assert store.count_by_item_id("item_0") == 0
    await manager.cleanup()


def test_distribute_items_across_lanes_balances_round_robin() -> None:
    assert distribute_items_across_lanes([0, 1, 2, 3, 4], max_lanes=2) == [
        [0, 2, 4],
        [1, 3],
    ]
