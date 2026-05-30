"""Fair old-vs-task comparison for sequential Pantograph stepping.

This compares two real paths on the exact same saved parent states:

* old path: ``goal_load`` once per item, then ``goal_tactic`` once per tactic;
* task path: one ``goal.step_batch`` command with ``maxParallelItems = 1``.

The parent creation phase is shared and reported separately. The comparison is
therefore about the step path only: outputs, saved child-state reloadability,
wall time, and Python-to-Lean transport command count.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from examples.pantograph_benchmark.old_vs_task_utils import (
    DEFAULT_OUTPUT,
    DEFAULT_WORKLOAD,
    report_to_jsonable,
    run_fair_comparison,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="compare old sequential path against pantograph_task")
    parser.add_argument("--workload", type=Path, default=DEFAULT_WORKLOAD)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workdir", type=Path, default=Path(".cache/pantograph_benchmark/old_vs_task_seq"))
    parser.add_argument("--project", type=Path, default=Path("mathlib4"))
    parser.add_argument("--items", type=int, default=16)
    parser.add_argument("--tactics-per-item", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--buffer-limit", type=int, default=5_000_000)
    parser.add_argument("--max-parallel-items", type=int, default=1)
    parser.add_argument("--mismatch-examples", type=int, default=20)
    return parser.parse_args(argv)


async def _main(argv: list[str]) -> int:
    args = _parse_args(argv)
    workload = _repo_relative(args.workload)
    output = _repo_relative(args.output)
    workdir = _repo_relative(args.workdir)
    project = _repo_relative(args.project)

    report = await run_fair_comparison(
        workload=workload,
        workdir=workdir,
        project_path=project,
        n_items=args.items,
        tactics_per_item=args.tactics_per_item,
        timeout_seconds=args.timeout_seconds,
        buffer_limit=args.buffer_limit,
        max_parallel_items=args.max_parallel_items,
    )
    payload = report_to_jsonable(report, max_mismatch_examples=args.mismatch_examples)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(_summary(payload, output), indent=2, ensure_ascii=False))
    return 0 if report.comparison.semantically_equivalent else 1


def _repo_relative(path: Path) -> Path:
    return path if path.is_absolute() else REPO_ROOT / path


def _summary(payload: dict[str, object], output: Path) -> dict[str, object]:
    comparison = payload["comparison"]
    old = payload["old"]
    task = payload["task"]
    transport = payload["transport_roundtrips_estimated"]
    assert isinstance(comparison, dict)
    assert isinstance(old, dict)
    assert isinstance(task, dict)
    assert isinstance(transport, dict)
    return {
        "output": str(output),
        "item_count": payload["item_count"],
        "attempt_count": payload["attempt_count"],
        "semantically_equivalent": comparison["semantically_equivalent"],
        "strictly_equivalent": comparison["strictly_equivalent"],
        "semantic_mismatch_count": comparison["semantic_mismatch_count"],
        "status_mismatch_count": comparison["status_mismatch_count"],
        "reloaded_goal_mismatch_count": comparison["reloaded_goal_mismatch_count"],
        "normalized_message_mismatch_count": comparison["normalized_message_mismatch_count"],
        "response_goal_mismatch_count": comparison["response_goal_mismatch_count"],
        "message_mismatch_count": comparison["message_mismatch_count"],
        "old_step_wall_s": old["step_wall_s"],
        "task_step_wall_s": task["step_wall_s"],
        "old_status_counts": old["status_counts"],
        "task_status_counts": task["status_counts"],
        "transport_roundtrips_estimated": {
            "old": transport["old"],
            "task": transport["task"],
        },
    }


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
