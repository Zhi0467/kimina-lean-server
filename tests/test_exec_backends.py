from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from server.exec_backend_utils import distribute_items_across_lanes
from server.exec_backends import StepBatchBackendConfig, execute_step_batch_request
from server.pantograph_manager import PantographManager, header_hash
from server.pantograph_worker import PantographStepResult
from server.schemas_exec import StepBatchItem, StepBatchRequest
from server.state_store import StateStore


def _write_state(path: Path, data: bytes = b"state") -> Path:
    path.write_bytes(data)
    return path


@dataclass
class _FakeWorker:
    step_calls: list[tuple[Path, list[str]]] = field(default_factory=list)
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
    ) -> list[PantographStepResult]:
        self.step_calls.append((state_path, list(tactics)))
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


def _config(max_lanes: int) -> StepBatchBackendConfig:
    return StepBatchBackendConfig(
        max_items_per_step_batch=16,
        max_tactics_per_step_item=8,
        max_attempts_per_step_batch=128,
        max_lean_processes_per_env_profile=max_lanes,
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
        config=_config(max_lanes=1),
    )

    assert len(factory.workers) == 1
    assert len(factory.workers[0].step_calls) == 3
    assert [
        [result.status for result in item.results]
        for item in response.items
    ] == [["complete", "complete"]] * 3

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
        config=_config(max_lanes=1),
    )

    assert len(factory.workers) == 2
    assert [len(worker.step_calls) for worker in factory.workers] == [1, 1]

    await manager.cleanup()


def test_distribute_items_across_lanes_balances_round_robin() -> None:
    assert distribute_items_across_lanes([0, 1, 2, 3, 4], max_lanes=2) == [
        [0, 2, 4],
        [1, 3],
    ]
