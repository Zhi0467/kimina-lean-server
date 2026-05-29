"""Exact-equivalence oracle: does a parallel step path match the sequential one?

This is the correctness gate for every candidate parallel backend. It runs a
fixed set of proof-state *items* (each = a parent goal state + a list of tactics
applied strictly in sequence) two ways and asserts the per-attempt results are
*byte-identical*:

* ``reference`` — ``maxParallelItems = 1`` (the trustworthy sequential ground
  truth: items processed one after another in a single Lean process), and
* ``candidate`` — ``maxParallelItems = K`` (the parallel path under test).

It deliberately compares the *semantic* fields of each tactic attempt (status,
resulting goals, messages, failure/parse diagnostics, sorry/unsafe flags) while
ignoring the child ``.bin`` path, which legitimately differs between runs
because each run writes to its own output directory.

Designed to run cheaply against ``import Init`` (loads in well under a second,
tiny memory) so a candidate can be iterated on without paying for Mathlib, then
re-run once against the real ``import Mathlib`` workload to confirm the result
holds where the lazy-realization races actually bite.

Usage (no rebuild; reuse the prebuilt repl binary):

    PYTHONPATH="$PWD:$PWD/third_party/PyPantograph" \
        .venv/bin/python examples/pantograph_benchmark/equivalence_oracle.py --imports Init --parallel 4
    PYTHONPATH="$PWD:$PWD/third_party/PyPantograph" \
        .venv/bin/python examples/pantograph_benchmark/equivalence_oracle.py \
            --imports Mathlib --imports Aesop --project mathlib4 --parallel 16
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pantograph  # type: ignore[reportMissingTypeStubs]

# server.* is importable because the repo root is on PYTHONPATH.
from server.pantograph_normalize import (  # noqa: E402
    goal_payloads_to_goal_texts,
    messages_to_texts,
)


@dataclass(frozen=True)
class EquivalenceProblem:
    """One item: a goal expression that becomes the parent goal state (via
    ``goal.start``), plus the tactics to apply to it strictly in sequence."""

    goal_expr: str
    tactics: list[str]


# Trivial problems that load under ``import Init`` only. They exercise intro/
# exact/rfl/simp/omega paths without any Mathlib dependency, so the oracle is
# cheap. They are intentionally *not* a stress test of realization races; the
# Mathlib workload is for that.
DEFAULT_INIT_PROBLEMS: list[EquivalenceProblem] = [
    EquivalenceProblem("forall (p : Prop), p -> p", ["intro p", "intro h", "exact h"]),
    EquivalenceProblem(
        "forall (p q : Prop), p -> q -> p",
        ["intro p", "intro q", "intro hp", "intro hq", "exact hp"],
    ),
    EquivalenceProblem("forall (n : Nat), n = n", ["intro n", "rfl"]),
    EquivalenceProblem(
        "forall (p q : Prop), p -> q -> p /\\ q",
        ["intro p", "intro q", "intro hp", "intro hq", "exact ⟨hp, hq⟩"],
    ),
    EquivalenceProblem("forall (n : Nat), n + 0 = n", ["intro n", "simp"]),
    EquivalenceProblem("forall (a b : Nat), a + b = b + a", ["intro a", "intro b", "omega"]),
]


@dataclass(frozen=True)
class ComparableAttempt:
    """The semantic, run-independent fields of one tactic attempt."""

    tactic: str
    status: str
    goals: tuple[str, ...]
    messages: tuple[str, ...]
    failure: str
    parse_error: str
    has_sorry: bool
    has_unsafe: bool


@dataclass(frozen=True)
class ComparableItem:
    item_index: int
    attempts: tuple[ComparableAttempt, ...]


@dataclass
class EquivalenceReport:
    parallel_cap: int
    item_count: int
    attempt_count: int
    mismatches: list[str] = field(default_factory=list)

    @property
    def equivalent(self) -> bool:
        return not self.mismatches


def _comparable_attempt(payload: dict) -> ComparableAttempt:
    return ComparableAttempt(
        tactic=str(payload.get("tactic", "")),
        status=str(payload.get("status", "")),
        goals=tuple(goal_payloads_to_goal_texts(payload.get("goals", []))),
        messages=tuple(messages_to_texts(payload.get("messages", []))),
        failure=str(payload.get("failure") or ""),
        parse_error=str(payload.get("parseError") or ""),
        has_sorry=bool(payload.get("hasSorry", False)),
        has_unsafe=bool(payload.get("hasUnsafe", False)),
    )


def _comparable_items(response: dict) -> dict[int, ComparableItem]:
    items: dict[int, ComparableItem] = {}
    for item_result in response.get("items", []):
        item_index = int(item_result["itemIdx"])
        attempts = tuple(
            _comparable_attempt(attempt)
            for attempt in item_result.get("results", [])
        )
        items[item_index] = ComparableItem(item_index=item_index, attempts=attempts)
    return items


async def _build_parent_items(
    server: object,
    problems: list[EquivalenceProblem],
    parents_dir: Path,
) -> list[dict]:
    """Create each problem's parent goal state, pickle it, return batch items."""
    parents_dir.mkdir(parents=True, exist_ok=True)
    items: list[dict] = []
    for item_index, problem in enumerate(problems):
        goal_state = await server.goal_start_async(problem.goal_expr)  # type: ignore[attr-defined]
        parent_path = parents_dir / f"parent_{item_index}.bin"
        await server.goal_save_async(goal_state, str(parent_path))  # type: ignore[attr-defined]
        items.append(
            {
                "itemIdx": item_index,
                "parentPath": str(parent_path),
                "tactics": problem.tactics,
            }
        )
    return items


async def _run_batch(
    server: object,
    items: list[dict],
    output_dir: Path,
    parallel: int,
) -> dict[int, ComparableItem]:
    output_dir.mkdir(parents=True, exist_ok=True)
    response = await server.goal_step_batch_async(  # type: ignore[attr-defined]
        items,
        output_dir=str(output_dir),
        max_parallel_items=parallel,
    )
    return _comparable_items(response)


def _diff_items(
    reference: dict[int, ComparableItem],
    candidate: dict[int, ComparableItem],
) -> list[str]:
    mismatches: list[str] = []
    all_indices = sorted(set(reference) | set(candidate))
    for item_index in all_indices:
        ref_item = reference.get(item_index)
        cand_item = candidate.get(item_index)
        if ref_item is None or cand_item is None:
            mismatches.append(
                f"item {item_index}: present in reference={ref_item is not None}, "
                f"candidate={cand_item is not None}"
            )
            continue
        ref_attempts = ref_item.attempts
        cand_attempts = cand_item.attempts
        if len(ref_attempts) != len(cand_attempts):
            mismatches.append(
                f"item {item_index}: attempt count "
                f"reference={len(ref_attempts)} candidate={len(cand_attempts)}"
            )
            continue
        for tactic_index, (ref_attempt, cand_attempt) in enumerate(
            zip(ref_attempts, cand_attempts)
        ):
            if ref_attempt != cand_attempt:
                mismatches.append(
                    f"item {item_index} tactic {tactic_index}:\n"
                    f"    reference={ref_attempt}\n"
                    f"    candidate={cand_attempt}"
                )
    return mismatches


async def run_equivalence_oracle(
    *,
    imports: list[str],
    project_path: str | None,
    problems: list[EquivalenceProblem],
    parallel_cap: int,
    timeout_seconds: int = 300,
) -> EquivalenceReport:
    """Create one server, run the items sequentially then in parallel, diff."""
    server = await pantograph.Server.create(
        imports=imports,
        project_path=project_path,
        timeout=timeout_seconds,
        buffer_limit=2_000_000,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="equiv_oracle_") as workdir_name:
            workdir = Path(workdir_name)
            items = await _build_parent_items(server, problems, workdir / "parents")
            reference = await _run_batch(
                server, items, workdir / "seq", parallel=1
            )
            candidate = await _run_batch(
                server, items, workdir / "par", parallel=parallel_cap
            )
            attempt_count = sum(len(item.attempts) for item in reference.values())
            return EquivalenceReport(
                parallel_cap=parallel_cap,
                item_count=len(items),
                attempt_count=attempt_count,
                mismatches=_diff_items(reference, candidate),
            )
    finally:
        close = getattr(server, "_close", None)
        if close is not None:
            close()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pantograph step-batch equivalence oracle")
    parser.add_argument("--imports", action="append", default=None, help="Lean imports (repeatable)")
    parser.add_argument("--project", default=None, help="Lean project path (e.g. mathlib4)")
    parser.add_argument("--parallel", type=int, default=4, help="maxParallelItems for candidate run")
    parser.add_argument("--repeat", type=int, default=1, help="Repeat the whole batch N times for stability")
    return parser.parse_args(argv)


async def _main(argv: list[str]) -> int:
    args = _parse_args(argv)
    imports = args.imports or ["Init"]
    problems = DEFAULT_INIT_PROBLEMS * args.repeat
    report = await run_equivalence_oracle(
        imports=imports,
        project_path=args.project,
        problems=problems,
        parallel_cap=args.parallel,
    )
    print(
        json.dumps(
            {
                "imports": imports,
                "parallel_cap": report.parallel_cap,
                "item_count": report.item_count,
                "attempt_count": report.attempt_count,
                "equivalent": report.equivalent,
                "mismatch_count": len(report.mismatches),
                "mismatches": report.mismatches[:20],
            },
            indent=2,
        )
    )
    return 0 if report.equivalent else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
