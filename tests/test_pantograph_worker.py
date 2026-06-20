from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from server.pantograph_goal import PantographHypothesis
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
    assert len(created.states[0].goals) == 1
    created_goal = created.states[0].goals[0]
    assert created_goal.target == "n + 0 = n"
    assert created_goal.pretty == "n : Nat\n⊢ n + 0 = n"
    assert created_goal.hypotheses == [PantographHypothesis(type="Nat", name="n")]
    assert created_goal.sibling_dep == []

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
    assert len(results[1].goals) == 1
    assert results[1].goals[0].target == "0 + n = n"
    assert results[1].goals[0].pretty == "n : Nat\n⊢ 0 + n = n"
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


async def test_sibling_dep_signals_metavariable_coupling(
    pantograph_worker: PantographWorker,
    tmp_path: Path,
) -> None:
    # Independent goals: ``constructor`` on a conjunction yields two goals that
    # share no metavariable, so every goal's ``sibling_dep`` is empty.
    conjunction = await pantograph_worker.create_states_from_code(
        "theorem t : True ∧ True := by\n  sorry",
        state_dir=tmp_path,
    )
    conjunction_split = await pantograph_worker.step_state_with_tactics(
        conjunction.states[0].path,
        ["constructor"],
        state_dir=tmp_path,
    )
    assert conjunction_split[0].status == "open"
    assert len(conjunction_split[0].goals) == 2
    assert all(goal.sibling_dep == [] for goal in conjunction_split[0].goals)

    # Coupled goals: ``apply Exists.intro`` leaves the witness goal and the
    # property goal sharing the witness metavariable, so ``printDependentMVars``
    # populates a non-empty ``sibling_dep`` on at least one of them.
    existential = await pantograph_worker.create_states_from_code(
        "theorem t : ∃ n : Nat, n = n := by\n  sorry",
        state_dir=tmp_path,
    )
    existential_split = await pantograph_worker.step_state_with_tactics(
        existential.states[0].path,
        ["apply Exists.intro"],
        state_dir=tmp_path,
    )
    assert existential_split[0].status == "open"
    assert any(goal.sibling_dep for goal in existential_split[0].goals)


async def test_goal_focusing_suspends_siblings(
    pantograph_worker: PantographWorker,
    tmp_path: Path,
) -> None:
    created = await pantograph_worker.create_states_from_code(
        "theorem t : True ∧ True := by\n  sorry",
        state_dir=tmp_path,
    )
    split = await pantograph_worker.step_state_with_tactics(
        created.states[0].path,
        ["constructor"],
        state_dir=tmp_path,
    )
    assert split[0].status == "open"
    two_goal_path = split[0].state_path
    assert two_goal_path is not None
    assert len(split[0].goals) == 2

    # Default whole-state step (automatic mode): ``trivial`` proves the first
    # goal and the second goal auto-resumes, so one goal remains.
    whole_state = await pantograph_worker.step_state_with_tactics(
        two_goal_path,
        ["trivial"],
        state_dir=tmp_path,
    )
    assert whole_state[0].status == "open"
    assert len(whole_state[0].goals) == 1

    # Focused step: targeting goal 0 with ``auto_resume=False`` suspends the
    # sibling, so proving goal 0 leaves zero in-scope goals (a clean, undragged
    # single-goal subtree) even though the theorem as a whole is not done.
    focused = await pantograph_worker.step_state_with_tactics(
        two_goal_path,
        ["trivial"],
        state_dir=tmp_path,
        goal_id=0,
        auto_resume=False,
    )
    assert focused[0].status == "complete"


async def test_goal_group_resumes_exact_subset(
    pantograph_worker: PantographWorker,
    tmp_path: Path,
) -> None:
    # ``(∃ n, n = n) ∧ True`` after ``constructor; apply Exists.intro`` yields a
    # mixed state: goal 0 (``?w = ?w``) is metavariable-coupled to goal 1 (the
    # ``Nat`` witness); goal 2 (``True``) is independent. ``goal_group`` resumes
    # an exact subset, which neither ``auto_resume`` mode can express.
    created = await pantograph_worker.create_states_from_code(
        "theorem t : (∃ n : Nat, n = n) ∧ True := by\n  sorry",
        state_dir=tmp_path,
    )
    after_constructor = await pantograph_worker.step_state_with_tactics(
        created.states[0].path, ["constructor"], state_dir=tmp_path
    )
    after_intro = await pantograph_worker.step_state_with_tactics(
        after_constructor[0].state_path,
        ["apply Exists.intro"],
        state_dir=tmp_path,
    )
    mixed = after_intro[0]
    assert [goal.sibling_dep for goal in mixed.goals] == [[1], [], []]

    # The coupled cluster [0, 1] resumes exactly those two goals; the
    # independent ``True`` (index 2) is excluded.
    cluster = await pantograph_worker.step_state_with_tactics(
        mixed.state_path, ["skip"], state_dir=tmp_path, goal_group=[0, 1]
    )
    assert cluster[0].status == "open"
    cluster_targets = [goal.target for goal in cluster[0].goals]
    assert len(cluster_targets) == 2
    assert "True" not in cluster_targets
    assert "Nat" in cluster_targets

    # The independent goal [2] resumes only ``True``; ``trivial`` closes it.
    independent = await pantograph_worker.step_state_with_tactics(
        mixed.state_path, ["trivial"], state_dir=tmp_path, goal_group=[2]
    )
    assert independent[0].status == "complete"


async def test_goal_group_rejects_out_of_range_index(
    pantograph_worker: PantographWorker,
    tmp_path: Path,
) -> None:
    created = await pantograph_worker.create_states_from_code(
        "theorem t (n : Nat) : n + 0 = n := by\n  sorry",
        state_dir=tmp_path,
    )
    result = await pantograph_worker.step_state_with_tactics(
        created.states[0].path,
        ["rfl"],
        state_dir=tmp_path,
        goal_group=[5],
    )
    assert result[0].status == "error"
    assert "out of range" in result[0].messages[0].data


async def test_is_alive_tracks_subprocess_state(
    pantograph_worker: PantographWorker,
) -> None:
    assert pantograph_worker.is_alive() is True
    await pantograph_worker.aclose()
    assert pantograph_worker.is_alive() is False
