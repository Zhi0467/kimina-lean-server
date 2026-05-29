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
    pantograph_task_warmup: bool = True,
) -> StepBatchBackendConfig:
    return StepBatchBackendConfig(
        exec_backend=exec_backend,
        max_items_per_step_batch=max_items_per_step_batch,
        max_tactics_per_step_item=max_tactics_per_step_item,
        max_attempts_per_step_batch=max_attempts_per_step_batch,
        max_items_per_worker_batch=max_items_per_worker_batch,
        max_parallel_items_per_lean_process=max_parallel_items_per_lean_process,
        pantograph_task_warmup=pantograph_task_warmup,
    )


@dataclass
class _FakeLease:
    worker: "_FakeTaskWorker"


class _FakeTaskWorker:
    def __init__(self) -> None:
        self.calls: list[list[PantographBatchStepInput]] = []
        self.pool_calls: list[Path] = []
        self.parallel_caps: list[int] = []
        self.warmups: list[bool] = []
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
        warmup: bool = True,
    ) -> list[PantographBatchStepItemResult]:
        self.calls.append(items)
        self.parallel_caps.append(max_parallel_items)
        self.warmups.append(warmup)
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
        warmup: bool = True,
    ) -> list[PantographBatchStepItemResult]:
        self.calls.append(items)
        self.parallel_caps.append(max_parallel_items)
        self.warmups.append(warmup)
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
    # Track A: the warmup decision flows from config to the worker/REPL.
    assert worker.warmups == [True]


@pytest.mark.asyncio
async def test_task_backend_forwards_warmup_toggle_to_worker(
    tmp_path: Path,
) -> None:
    store = StateStore(
        tmp_path / "store",
        token_factory=iter(["st_0"]).__next__,
    )
    token = store.put(
        _write_state(tmp_path / "root.bin"),
        item_id="item_0",
        env_profile="env",
        header="import Init",
        header_hash="same",
    )
    request = StepBatchRequest(
        items=[{"node_id": "n0", "state_token": token, "tactics": ["simp"]}]
    )
    worker = _FakeTaskWorker()

    await execute_step_batch_request(
        request,
        state_store=store,
        pantograph_manager=_FakeManager(worker),  # type: ignore[arg-type]
        config=_config(pantograph_task_warmup=False),
    )

    assert worker.warmups == [False]


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
