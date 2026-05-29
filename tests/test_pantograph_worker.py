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


async def test_text_tactic_suggestions_use_tactic_file_map(
    pantograph_worker: PantographWorker,
    tmp_path: Path,
) -> None:
    created = await pantograph_worker.create_states_from_code(
        "-- a₀\n"
        "theorem t (n : Nat) : n + 0 = n := by\n"
        "  sorry",
        state_dir=tmp_path,
    )
    assert created.status == "open"

    results = await pantograph_worker.step_state_with_tactics(
        created.states[0].path,
        ["simp?"],
        state_dir=tmp_path,
    )

    assert results[0].status == "complete"
    assert results[0].messages == [
        "1:0-1:5: Try this:\n  [apply] simp only [Nat.add_zero]"
    ]


async def test_goal_step_batch_direct_wrapper_steps_items_in_one_process(
    pantograph_worker: PantographWorker,
    tmp_path: Path,
) -> None:
    created = await pantograph_worker.create_states_from_code(
        "theorem t (n : Nat) : n + 0 = n := by\n  sorry",
        state_dir=tmp_path,
    )
    assert created.status == "open"
    parent_path = created.states[0].path

    result = await pantograph_worker._server.goal_step_batch_async(
        [
            {
                "itemIdx": idx,
                "parentPath": str(parent_path),
                "tactics": ["simp", "rw [Nat.add_comm]", "bad_tactic"],
            }
            for idx in range(2)
        ],
        output_dir=str(tmp_path),
        max_parallel_items=2,
    )

    assert [item["itemIdx"] for item in result["items"]] == [0, 1]
    for item in result["items"]:
        assert [attempt["status"] for attempt in item["results"]] == [
            "complete",
            "open",
            "error",
        ]
        child_path = Path(item["results"][1]["childPath"])
        assert child_path.exists()

        resumed = await pantograph_worker.step_state_with_tactics(
            child_path,
            ["simp"],
            state_dir=tmp_path,
        )
        assert resumed[0].status == "complete"


async def test_goal_step_batch_direct_wrapper_handles_16_by_8_capacity(
    pantograph_worker: PantographWorker,
    tmp_path: Path,
) -> None:
    created = await pantograph_worker.create_states_from_code(
        "theorem t (n : Nat) : n + 0 = n := by\n  sorry",
        state_dir=tmp_path,
    )
    assert created.status == "open"
    tactics = [
        "have h : n = n := rfl",
        "simp",
        "rw [Nat.add_zero]",
        "bad_tactic",
        "have h : n = n := rfl",
        "simp",
        "rw [Nat.add_zero]",
        "bad_tactic",
    ]

    result = await pantograph_worker._server.goal_step_batch_async(
        [
            {
                "itemIdx": idx,
                "parentPath": str(created.states[0].path),
                "tactics": tactics,
            }
            for idx in range(16)
        ],
        output_dir=str(tmp_path),
        max_parallel_items=16,
    )

    assert len(result["items"]) == 16
    for item in result["items"]:
        assert [attempt["status"] for attempt in item["results"]] == [
            "open",
            "complete",
            "complete",
            "error",
            "open",
            "complete",
            "complete",
            "error",
        ]
        assert Path(item["results"][0]["childPath"]).exists()
        assert Path(item["results"][4]["childPath"]).exists()


async def test_goal_step_batch_direct_wrapper_is_equivalent_across_parallel_caps(
    pantograph_worker: PantographWorker,
    tmp_path: Path,
) -> None:
    created = await pantograph_worker.create_states_from_code(
        "theorem t (n : Nat) : n + 0 = n := by\n  sorry",
        state_dir=tmp_path,
    )
    assert created.status == "open"
    tactics = [
        "have h : n = n := rfl",
        "simp",
        "rw [Nat.add_zero]",
        "bad_tactic",
    ]
    items = [
        {
            "itemIdx": idx,
            "parentPath": str(created.states[0].path),
            "tactics": tactics,
        }
        for idx in range(4)
    ]

    sequential = await pantograph_worker._server.goal_step_batch_async(
        items,
        output_dir=str(tmp_path / "seq"),
        max_parallel_items=1,
    )
    parallel = await pantograph_worker._server.goal_step_batch_async(
        items,
        output_dir=str(tmp_path / "par"),
        max_parallel_items=4,
    )

    assert _compact_batch_result(sequential) == _compact_batch_result(parallel)


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


async def test_agc_frees_states_without_explicit_gc_collect(
    pantograph_worker: PantographWorker,
    tmp_path: Path,
) -> None:
    import gc

    # Disable the cyclic collector so only refcounting can free objects. If the
    # deletion queue still fills, agc() needs no explicit gc.collect().
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        created = await pantograph_worker.create_states_from_code(
            "theorem t (n : Nat) : n + 0 = n := by\n  sorry",
            state_dir=tmp_path,
        )
        await pantograph_worker.step_state_with_tactics(
            created.states[0].path,
            ["rw [Nat.add_comm]"],
            state_dir=tmp_path,
        )

        assert pantograph_worker._server.to_remove_goal_states  # populated by refcount

        await pantograph_worker.agc()

        assert pantograph_worker._server.to_remove_goal_states == []
    finally:
        if was_enabled:
            gc.enable()


async def test_state_saved_by_one_worker_loads_in_another(tmp_path: Path) -> None:
    worker_a = await PantographWorker.create(
        imports=["Init"], timeout_seconds=30, buffer_limit=2_000_000
    )
    worker_b = await PantographWorker.create(
        imports=["Init"], timeout_seconds=30, buffer_limit=2_000_000
    )
    try:
        created = await worker_a.create_states_from_code(
            "theorem t (n : Nat) : n + 0 = n := by\n  sorry",
            state_dir=tmp_path,
        )
        assert created.status == "open"

        # A different subprocess loads worker A's saved state and finishes it.
        results = await worker_b.step_state_with_tactics(
            created.states[0].path,
            ["simp"],
            state_dir=tmp_path,
        )
        assert results[0].status == "complete"
    finally:
        await worker_a.aclose()
        await worker_b.aclose()


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


def _compact_batch_result(result: dict) -> list[list[tuple[str, str, bool]]]:
    return [
        [
            (
                str(attempt["tactic"]),
                str(attempt["status"]),
                bool(attempt.get("childPath")),
            )
            for attempt in item["results"]
        ]
        for item in result["items"]
    ]
