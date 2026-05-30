from __future__ import annotations

from dataclasses import dataclass

import pytest

from server.pantograph_manager import (
    NoAvailablePantographWorkerError,
    PantographManager,
    header_hash,
    imports_from_header,
)


@dataclass
class FakeWorker:
    closed: bool = False

    async def aclose(self) -> None:
        self.closed = True


class FakeWorkerFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None, int, int | None]] = []
        self.workers: list[FakeWorker] = []

    async def __call__(
        self,
        imports: list[str],
        project_path: str | None,
        timeout_seconds: int,
        buffer_limit: int | None,
    ) -> FakeWorker:
        self.calls.append((imports, project_path, timeout_seconds, buffer_limit))
        worker = FakeWorker()
        self.workers.append(worker)
        return worker


def test_imports_from_header_parses_lean_import_lines() -> None:
    assert imports_from_header("import Mathlib\nimport Foo Bar\n") == [
        "Mathlib",
        "Foo",
        "Bar",
    ]
    assert imports_from_header("") == ["Init"]


async def test_pantograph_manager_reuses_compatible_idle_worker() -> None:
    factory = FakeWorkerFactory()
    manager = PantographManager(
        max_workers=1,
        buffer_limit=123,
        worker_factory=factory,
    )

    first = await manager.get_worker(
        env_profile="env",
        header="import Init",
        timeout=1,
    )
    await manager.release_worker(first)
    second = await manager.get_worker(
        env_profile="env",
        header="import Init",
        timeout=1,
    )

    assert second is first
    assert factory.calls == [(["Init"], None, 600, 123)]
    assert second.header_hash == header_hash("import Init")

    await manager.release_worker(second)
    await manager.cleanup()
    assert factory.workers[0].closed


async def test_pantograph_manager_evicts_idle_incompatible_worker() -> None:
    factory = FakeWorkerFactory()
    manager = PantographManager(max_workers=1, worker_factory=factory)

    first = await manager.get_worker(env_profile="env_a", header="", timeout=1)
    await manager.release_worker(first)
    second = await manager.get_worker(env_profile="env_b", header="", timeout=1)

    assert second is not first
    assert first.worker.closed
    assert len(factory.calls) == 2

    await manager.release_worker(second)
    await manager.cleanup()


async def test_pantograph_manager_times_out_when_all_workers_are_busy() -> None:
    manager = PantographManager(
        max_workers=1,
        worker_factory=FakeWorkerFactory(),
    )
    await manager.get_worker(env_profile="env", header="", timeout=1)

    with pytest.raises(NoAvailablePantographWorkerError, match="Timed out"):
        await manager.get_worker(env_profile="env", header="", timeout=0.001)

    await manager.cleanup()


async def test_pantograph_manager_enforces_per_env_profile_cap() -> None:
    manager = PantographManager(
        max_workers=2,
        max_workers_per_env_profile=1,
        worker_factory=FakeWorkerFactory(),
    )
    await manager.get_worker(env_profile="env", header="", timeout=1)

    with pytest.raises(NoAvailablePantographWorkerError, match="Timed out"):
        await manager.get_worker(env_profile="env", header="", timeout=0.001)

    other = await manager.get_worker(env_profile="other", header="", timeout=1)
    assert other.env_profile == "other"

    await manager.cleanup()


async def test_pantograph_manager_closes_exhausted_worker_on_release() -> None:
    factory = FakeWorkerFactory()
    manager = PantographManager(
        max_workers=1,
        max_worker_uses=1,
        worker_factory=factory,
    )

    first = await manager.get_worker(env_profile="env", header="", timeout=1)
    await manager.release_worker(first)

    assert first.use_count == 1
    assert first.worker.closed

    second = await manager.get_worker(env_profile="env", header="", timeout=1)

    assert second is not first
    assert len(factory.calls) == 2

    await manager.release_worker(second)
    await manager.cleanup()
