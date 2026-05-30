from __future__ import annotations

from examples.pantograph_benchmark.old_vs_task_utils import (
    AttemptSnapshot,
    RunReport,
    compare_runs,
    normalize_messages,
)


def _attempt(
    *,
    status: str = "open",
    response_goals: tuple[str, ...] = ("⊢ True",),
    reloaded_goals: tuple[str, ...] = ("⊢ True",),
    messages: tuple[str, ...] = (),
) -> AttemptSnapshot:
    return AttemptSnapshot(
        item_index=0,
        tactic_index=0,
        tactic="trivial",
        status=status,
        response_goals=response_goals,
        messages=messages,
        normalized_messages=normalize_messages(messages),
        child_path="/tmp/child.bin" if status == "open" else None,
        reloaded_goals=reloaded_goals,
        reload_error=None,
    )


def _run(name: str, attempt: AttemptSnapshot) -> RunReport:
    assert name in {"old", "task"}
    return RunReport(
        name=name,  # type: ignore[arg-type]
        startup_wall_s=1.0,
        step_wall_s=2.0,
        reload_wall_s=0.1,
        worker_alive=True,
        attempts=(attempt,),
    )


def test_normalize_messages_strips_old_pantograph_line_prefix() -> None:
    assert normalize_messages(("1:0: error: tactic failed", "plain")) == (
        "tactic failed",
        "plain",
    )


def test_compare_runs_treats_message_prefix_only_as_not_semantic() -> None:
    old = _run("old", _attempt(status="error", messages=("1:0: error: failed",)))
    task = _run("task", _attempt(status="error", messages=("failed",)))

    summary = compare_runs(old, task)

    assert summary.semantically_equivalent is True
    assert summary.strictly_equivalent is False
    assert summary.message_mismatch_count == 1
    assert summary.normalized_message_mismatch_count == 0


def test_compare_runs_flags_reloaded_goal_mismatch_as_semantic() -> None:
    old = _run("old", _attempt(reloaded_goals=("⊢ True",)))
    task = _run("task", _attempt(reloaded_goals=("⊢ False",)))

    summary = compare_runs(old, task)

    assert summary.semantically_equivalent is False
    assert summary.reloaded_goal_mismatch_count == 1
    assert summary.semantic_mismatch_count == 1


def test_compare_runs_reports_normalized_message_mismatch_without_failing_proof_state_gate() -> None:
    old = _run("old", _attempt(status="error", messages=("first failure",)))
    task = _run("task", _attempt(status="error", messages=("different failure",)))

    summary = compare_runs(old, task)

    assert summary.semantically_equivalent is True
    assert summary.normalized_message_mismatch_count == 1
    assert summary.semantic_mismatch_count == 0
