from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from server.exec_backends import (
    StepBatchBackendConfig,
    StepBatchCapError,
    execute_step_batch_request,
    validate_step_batch_caps,
)
from server.pantograph_worker import (
    PantographBatchStepInput,
    PantographBatchStepItemResult,
    PantographStepResult,
)
from server.schemas_exec import StepBatchRequest
from server.state_store import StateStore


def _write_state(path: Path, data: bytes = b"state") -> Path:
    path.write_bytes(data)
    return path


def _config(
    *,
    exec_backend: str = "pantograph_task",
    max_items_per_step_batch: int = 16,
    max_tactics_per_step_item: int = 8,
    max_attempts_per_step_batch: int = 128,
    max_items_per_worker_batch: int = 16,
    max_parallel_items_per_lean_process: int = 16,
    max_lean_processes_per_env_profile: int = 1,
) -> StepBatchBackendConfig:
    return StepBatchBackendConfig(
        exec_backend=exec_backend,
        max_items_per_step_batch=max_items_per_step_batch,
        max_tactics_per_step_item=max_tactics_per_step_item,
        max_attempts_per_step_batch=max_attempts_per_step_batch,
        max_items_per_worker_batch=max_items_per_worker_batch,
        max_parallel_items_per_lean_process=max_parallel_items_per_lean_process,
        max_lean_processes_per_env_profile=max_lean_processes_per_env_profile,
    )


@dataclass
class _FakeLease:
    worker: "_FakeTaskWorker"


class _FakeTaskWorker:
    def __init__(self) -> None:
        self.calls: list[list[PantographBatchStepInput]] = []
        self.pool_calls: list[Path] = []
        self.parallel_caps: list[int] = []
        self.gc_calls = 0

    async def step_state_with_tactics(
        self,
        state_path: Path,
        tactics: list[str],
        *,
        state_dir: Path,
    ) -> list[PantographStepResult]:
        self.pool_calls.append(state_path)
        return [
            PantographStepResult(tactic=tactic, status="complete")
            for tactic in tactics
        ]

    async def step_state_batch_with_tactics(
        self,
        items: list[PantographBatchStepInput],
        *,
        state_dir: Path,
        max_parallel_items: int,
    ) -> list[PantographBatchStepItemResult]:
        self.calls.append(items)
        self.parallel_caps.append(max_parallel_items)
        return [
            PantographBatchStepItemResult(
                item_index=item.item_index,
                results=[
                    PantographStepResult(tactic=tactic, status="complete")
                    for tactic in item.tactics
                ],
            )
            for item in items
        ]

    def set_timeout_seconds(self, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds

    def is_alive(self) -> bool:
        return True

    async def agc(self) -> None:
        self.gc_calls += 1


class _FakeManager:
    def __init__(self, worker: _FakeTaskWorker) -> None:
        self.worker = worker
        self.leases: list[tuple[str, str, float]] = []
        self.released = 0

    async def get_worker(
        self,
        *,
        env_profile: str,
        header: str,
        timeout: float,
    ) -> _FakeLease:
        self.leases.append((env_profile, header, timeout))
        return _FakeLease(self.worker)

    async def release_worker(self, lease: _FakeLease) -> None:
        self.released += 1

    async def destroy_worker(self, lease: _FakeLease) -> None:
        raise AssertionError("healthy fake worker should be released")


class _OpenChildWorker(_FakeTaskWorker):
    def __init__(self, barrier_count: int = 1) -> None:
        super().__init__()
        self.state_dirs: list[Path] = []
        self._barrier_count = barrier_count
        self._barrier_seen = 0
        self._barrier = asyncio.Event()

    async def step_state_batch_with_tactics(
        self,
        items: list[PantographBatchStepInput],
        *,
        state_dir: Path,
        max_parallel_items: int,
    ) -> list[PantographBatchStepItemResult]:
        self.calls.append(items)
        self.state_dirs.append(state_dir)
        child_path = state_dir / "item_0_tactic_0.bin"
        child_path.write_bytes(f"child-{len(self.state_dirs)}".encode())
        self._barrier_seen += 1
        if self._barrier_seen >= self._barrier_count:
            self._barrier.set()
        await asyncio.wait_for(self._barrier.wait(), timeout=1)
        return [
            PantographBatchStepItemResult(
                item_index=item.item_index,
                results=[
                    PantographStepResult(
                        tactic=item.tactics[0],
                        status="open",
                        state_path=child_path,
                    )
                ],
            )
            for item in items
        ]


@pytest.mark.asyncio
async def test_task_backend_groups_compatible_items_into_one_worker_batch(
    tmp_path: Path,
) -> None:
    token_iter = iter(["st_0", "st_1"])
    store = StateStore(
        tmp_path / "store",
        token_factory=token_iter.__next__,
    )
    tokens = [
        store.put(
            _write_state(tmp_path / f"root_{idx}.bin"),
            item_id=f"item_{idx}",
            env_profile="env",
            header="import Init",
            header_hash="same",
        )
        for idx in range(2)
    ]
    request = StepBatchRequest(
        items=[
            {"node_id": f"n{idx}", "state_token": token, "tactics": ["simp"]}
            for idx, token in enumerate(tokens)
        ]
    )
    worker = _FakeTaskWorker()

    response = await execute_step_batch_request(
        request,
        state_store=store,
        pantograph_manager=_FakeManager(worker),  # type: ignore[arg-type]
        config=_config(max_parallel_items_per_lean_process=7),
    )

    assert [item.node_id for item in response.items] == ["n0", "n1"]
    assert [result.status for item in response.items for result in item.results] == [
        "complete",
        "complete",
    ]
    assert len(worker.calls) == 1
    assert [item.item_index for item in worker.calls[0]] == [0, 1]
    assert worker.parallel_caps == [7]


@pytest.mark.asyncio
async def test_task_backend_splits_incompatible_headers(
    tmp_path: Path,
) -> None:
    store = StateStore(
        tmp_path / "store",
        token_factory=iter(["st_a", "st_b"]).__next__,
    )
    token_a = store.put(
        _write_state(tmp_path / "a.bin"),
        item_id="a",
        env_profile="env",
        header="import Init",
        header_hash="a",
    )
    token_b = store.put(
        _write_state(tmp_path / "b.bin"),
        item_id="b",
        env_profile="env",
        header="import Mathlib",
        header_hash="b",
    )
    request = StepBatchRequest(
        items=[
            {"node_id": "a", "state_token": token_a, "tactics": ["simp"]},
            {"node_id": "b", "state_token": token_b, "tactics": ["simp"]},
        ]
    )
    worker = _FakeTaskWorker()
    manager = _FakeManager(worker)

    await execute_step_batch_request(
        request,
        state_store=store,
        pantograph_manager=manager,  # type: ignore[arg-type]
        config=_config(),
    )

    assert len(worker.calls) == 2
    assert [[item.item_index for item in call] for call in worker.calls] == [[0], [1]]
    assert [lease[1] for lease in manager.leases] == ["import Init", "import Mathlib"]


@pytest.mark.asyncio
async def test_task_backend_uses_per_command_output_dir_for_child_files(
    tmp_path: Path,
) -> None:
    token_iter = iter(["st_root_a", "st_root_b", "st_child_a", "st_child_b"])
    store = StateStore(
        tmp_path / "store",
        token_factory=token_iter.__next__,
    )
    token_a = store.put(
        _write_state(tmp_path / "root_a.bin"),
        item_id="a",
        env_profile="env_a",
        header="import Init",
        header_hash="same",
    )
    token_b = store.put(
        _write_state(tmp_path / "root_b.bin"),
        item_id="b",
        env_profile="env_b",
        header="import Init",
        header_hash="same",
    )
    worker = _OpenChildWorker(barrier_count=2)
    manager = _FakeManager(worker)

    responses = await asyncio.gather(
        execute_step_batch_request(
            StepBatchRequest(
                items=[
                    {
                        "node_id": "a",
                        "state_token": token_a,
                        "tactics": ["rw [Nat.add_comm]"],
                    }
                ]
            ),
            state_store=store,
            pantograph_manager=manager,  # type: ignore[arg-type]
            config=_config(),
        ),
        execute_step_batch_request(
            StepBatchRequest(
                items=[
                    {
                        "node_id": "b",
                        "state_token": token_b,
                        "tactics": ["rw [Nat.add_comm]"],
                    }
                ]
            ),
            state_store=store,
            pantograph_manager=manager,  # type: ignore[arg-type]
            config=_config(),
        ),
    )

    assert len(worker.state_dirs) == 2
    assert worker.state_dirs[0] != worker.state_dirs[1]
    assert all(state_dir.parent == store.root_dir for state_dir in worker.state_dirs)
    assert all(state_dir != store.root_dir for state_dir in worker.state_dirs)
    assert [
        result.status
        for response in responses
        for item in response.items
        for result in item.results
    ] == ["open", "open"]


@pytest.mark.asyncio
async def test_pool_backend_preserves_item_at_a_time_execution(
    tmp_path: Path,
) -> None:
    token_iter = iter(["st_0", "st_1"])
    store = StateStore(
        tmp_path / "store",
        token_factory=token_iter.__next__,
    )
    tokens = [
        store.put(
            _write_state(tmp_path / f"root_{idx}.bin"),
            item_id=f"item_{idx}",
            env_profile="env",
            header="import Init",
            header_hash="same",
        )
        for idx in range(2)
    ]
    request = StepBatchRequest(
        items=[
            {"node_id": f"n{idx}", "state_token": token, "tactics": ["simp"]}
            for idx, token in enumerate(tokens)
        ]
    )
    worker = _FakeTaskWorker()

    response = await execute_step_batch_request(
        request,
        state_store=store,
        pantograph_manager=_FakeManager(worker),  # type: ignore[arg-type]
        config=_config(exec_backend="pantograph_pool"),
    )

    assert [item.node_id for item in response.items] == ["n0", "n1"]
    assert len(worker.pool_calls) == 2
    assert worker.calls == []


def test_step_batch_caps_reject_oversized_requests() -> None:
    request = StepBatchRequest(
        items=[
            {
                "node_id": "n0",
                "state_token": "st_root",
                "tactics": ["simp", "omega"],
            }
        ]
    )

    with pytest.raises(StepBatchCapError, match="max_tactics_per_step_item"):
        validate_step_batch_caps(request, _config(max_tactics_per_step_item=1))


# --------------------------------------------------------------------------
# pantograph_process_pool backend: bounded, exact-equivalent multi-process pool
# --------------------------------------------------------------------------


class _PoolWorker:
    """Records which parent state paths it stepped, in order, to verify that a
    lane processes its items strictly sequentially on a single process."""

    def __init__(self, worker_id: int) -> None:
        self.worker_id = worker_id
        self.stepped_paths: list[Path] = []
        self.alive = True

    async def step_state_with_tactics(
        self,
        state_path: Path,
        tactics: list[str],
        *,
        state_dir: Path,
    ) -> list[PantographStepResult]:
        self.stepped_paths.append(state_path)
        return [
            PantographStepResult(tactic=tactic, status="complete")
            for tactic in tactics
        ]

    def set_timeout_seconds(self, timeout_seconds: int) -> None:
        pass

    def is_alive(self) -> bool:
        return self.alive

    async def agc(self) -> None:
        pass


class _PoolManager:
    """Hands out a *distinct* worker per ``get_worker`` call so a test can tell
    lanes apart and confirm the lease count equals the lane count."""

    def __init__(self) -> None:
        self.workers: list[_PoolWorker] = []
        self.get_worker_calls: list[tuple[str, str]] = []
        self.released = 0
        self.destroyed = 0

    async def get_worker(
        self,
        *,
        env_profile: str,
        header: str,
        timeout: float,
    ) -> _FakeLease:
        worker = _PoolWorker(len(self.workers))
        self.workers.append(worker)
        self.get_worker_calls.append((env_profile, header))
        return _FakeLease(worker)  # type: ignore[arg-type]

    async def release_worker(self, lease: _FakeLease) -> None:
        self.released += 1

    async def destroy_worker(self, lease: _FakeLease) -> None:
        self.destroyed += 1


def _put_items(store: StateStore, tmp_path: Path, count: int, *, header: str = "import Init") -> list[str]:
    return [
        store.put(
            _write_state(tmp_path / f"root_{index}.bin"),
            item_id=f"item_{index}",
            env_profile="env",
            header=header,
            header_hash="same",
        )
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_process_pool_bounds_lanes_and_reuses_one_lease_per_lane(
    tmp_path: Path,
) -> None:
    store = StateStore(
        tmp_path / "store",
        token_factory=iter([f"st_{i}" for i in range(5)]).__next__,
    )
    tokens = _put_items(store, tmp_path, 5)
    request = StepBatchRequest(
        items=[
            {"node_id": f"n{i}", "state_token": token, "tactics": ["simp"]}
            for i, token in enumerate(tokens)
        ]
    )
    manager = _PoolManager()

    response = await execute_step_batch_request(
        request,
        state_store=store,
        pantograph_manager=manager,  # type: ignore[arg-type]
        config=_config(
            exec_backend="pantograph_process_pool",
            max_lean_processes_per_env_profile=2,
        ),
    )

    # Exactly 2 lanes -> exactly 2 leases (the lease is reused across each
    # lane's items rather than re-acquired per item).
    assert len(manager.get_worker_calls) == 2
    assert manager.released == 2
    # 5 items dealt round-robin across 2 lanes: 3 on lane 0, 2 on lane 1.
    stepped_counts = sorted(len(w.stepped_paths) for w in manager.workers)
    assert stepped_counts == [2, 3]
    # Every item ran exactly once, results in original request order.
    all_stepped = [p for w in manager.workers for p in w.stepped_paths]
    expected = {store.resolve(token).path for token in tokens}
    assert set(all_stepped) == expected
    assert len(all_stepped) == 5
    assert [item.node_id for item in response.items] == ["n0", "n1", "n2", "n3", "n4"]
    assert all(
        result.status == "complete"
        for item in response.items
        for result in item.results
    )


@pytest.mark.asyncio
async def test_process_pool_splits_incompatible_headers_into_separate_lanes(
    tmp_path: Path,
) -> None:
    store = StateStore(
        tmp_path / "store",
        token_factory=iter(["st_a", "st_b"]).__next__,
    )
    token_a = store.put(
        _write_state(tmp_path / "a.bin"),
        item_id="a",
        env_profile="env",
        header="import Init",
        header_hash="a",
    )
    token_b = store.put(
        _write_state(tmp_path / "b.bin"),
        item_id="b",
        env_profile="env",
        header="import Mathlib",
        header_hash="b",
    )
    request = StepBatchRequest(
        items=[
            {"node_id": "a", "state_token": token_a, "tactics": ["simp"]},
            {"node_id": "b", "state_token": token_b, "tactics": ["simp"]},
        ]
    )
    manager = _PoolManager()

    await execute_step_batch_request(
        request,
        state_store=store,
        pantograph_manager=manager,  # type: ignore[arg-type]
        config=_config(
            exec_backend="pantograph_process_pool",
            max_lean_processes_per_env_profile=4,
        ),
    )

    # Different headers cannot share a process, so each is its own group/lane.
    assert sorted(header for _, header in manager.get_worker_calls) == [
        "import Init",
        "import Mathlib",
    ]


@pytest.mark.asyncio
async def test_process_pool_results_match_item_at_a_time(
    tmp_path: Path,
) -> None:
    """The bounded pool must produce results identical to the proven
    item-at-a-time path regardless of how items are spread across lanes."""

    def build_store(prefix: str) -> tuple[StateStore, list[str]]:
        store = StateStore(
            tmp_path / prefix,
            token_factory=iter([f"{prefix}_{i}" for i in range(6)]).__next__,
        )
        tokens = [
            store.put(
                _write_state(tmp_path / f"{prefix}_root_{i}.bin"),
                item_id=f"item_{i}",
                env_profile="env",
                header="import Init",
                header_hash="same",
            )
            for i in range(6)
        ]
        return store, tokens

    pool_store, pool_tokens = build_store("pool")
    proc_store, proc_tokens = build_store("proc")
    pool_request = StepBatchRequest(
        items=[
            {"node_id": f"n{i}", "state_token": token, "tactics": ["intro h", "exact h"]}
            for i, token in enumerate(pool_tokens)
        ]
    )
    proc_request = StepBatchRequest(
        items=[
            {"node_id": f"n{i}", "state_token": token, "tactics": ["intro h", "exact h"]}
            for i, token in enumerate(proc_tokens)
        ]
    )

    item_at_a_time = await execute_step_batch_request(
        pool_request,
        state_store=pool_store,
        pantograph_manager=_PoolManager(),  # type: ignore[arg-type]
        config=_config(exec_backend="pantograph_pool"),
    )
    process_pool = await execute_step_batch_request(
        proc_request,
        state_store=proc_store,
        pantograph_manager=_PoolManager(),  # type: ignore[arg-type]
        config=_config(
            exec_backend="pantograph_process_pool",
            max_lean_processes_per_env_profile=3,
        ),
    )

    def comparable(response: object) -> list[tuple[str, tuple[tuple[str, str], ...]]]:
        return [
            (
                item.node_id,
                tuple((r.tactic, r.status) for r in item.results),
            )
            for item in response.items  # type: ignore[attr-defined]
        ]

    assert comparable(process_pool) == comparable(item_at_a_time)
