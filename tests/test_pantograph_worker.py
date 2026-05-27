from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from server.pantograph_worker import PantographWorker


@pytest.fixture
async def pantograph_worker() -> AsyncGenerator[PantographWorker]:
    worker = await PantographWorker.create(
        imports=["Init"],
        timeout_seconds=30,
        buffer_limit=2_000_000,
    )
    try:
        yield worker
    finally:
        await worker.aclose()


async def test_pantograph_worker_creates_and_steps_real_state(
    pantograph_worker: PantographWorker,
    tmp_path: Path,
) -> None:
    created = await pantograph_worker.create_states_from_code(
        "theorem t (n : Nat) : n + 0 = n := by\n  sorry",
        state_dir=tmp_path,
    )

    assert created.status == "open"
    assert created.messages == []
    assert len(created.states) == 1
    assert created.states[0].path.exists()
    assert created.states[0].goals == ["n : Nat\n⊢ n + 0 = n"]

    results = await pantograph_worker.step_state_with_tactics(
        created.states[0].path,
        ["simp", "rw [Nat.add_comm]", "bad_tactic"],
        state_dir=tmp_path,
    )

    assert [result.status for result in results] == ["complete", "open", "error"]
    assert results[0].tactic == "simp"
    assert results[0].state_path is None
    assert results[1].state_path is not None
    assert results[1].state_path.exists()
    assert results[1].goals == ["n : Nat\n⊢ 0 + n = n"]
    assert results[2].messages


async def test_pantograph_gc_preserves_saved_state_files(
    pantograph_worker: PantographWorker,
    tmp_path: Path,
) -> None:
    created = await pantograph_worker.create_states_from_code(
        "theorem t (n : Nat) : n + 0 = n := by\n  sorry",
        state_dir=tmp_path,
    )
    stepped = await pantograph_worker.step_state_with_tactics(
        created.states[0].path,
        ["rw [Nat.add_comm]"],
        state_dir=tmp_path,
    )
    child_path = stepped[0].state_path
    assert child_path is not None
    assert child_path.exists()

    await pantograph_worker.agc()

    resumed = await pantograph_worker.step_state_with_tactics(
        child_path,
        ["simp"],
        state_dir=tmp_path,
    )
    assert resumed[0].status == "complete"


async def test_pantograph_worker_normalizes_complete_and_error_code(
    pantograph_worker: PantographWorker,
    tmp_path: Path,
) -> None:
    complete = await pantograph_worker.create_states_from_code(
        "theorem t : True := by\n  trivial",
        state_dir=tmp_path,
    )
    assert complete.status == "complete"
    assert complete.states == []

    errored = await pantograph_worker.create_states_from_code(
        "theorem t : True := by\n  does_not_exist",
        state_dir=tmp_path,
    )
    assert errored.status == "error"
    assert errored.messages


async def test_is_alive_tracks_subprocess_state(
    pantograph_worker: PantographWorker,
) -> None:
    assert pantograph_worker.is_alive() is True
    await pantograph_worker.aclose()
    assert pantograph_worker.is_alive() is False
