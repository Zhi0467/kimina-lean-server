from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

from server.pantograph_normalize import goal_state_to_goal_texts
from server.pantograph_worker import (
    PantographBatchStepInput,
    PantographStepResult,
    PantographWorker,
)
from server.split import split_snippet

PathLike = str | Path
T = TypeVar("T")

DEFAULT_WORKLOAD = Path(".cache/pantograph_benchmark/frozen_goedel_seq16x8.json")
DEFAULT_OUTPUT = Path(".cache/pantograph_benchmark/results_old_vs_task_seq16x8.json")

_LEAN_ERROR_PREFIX_RE = re.compile(r"^\d+:\d+:\s+error:\s+")


@dataclass(frozen=True)
class WorkloadItem:
    item_index: int
    problem_id: str
    source_hash: str
    header: str
    body: str
    tactics: tuple[str, ...]


@dataclass(frozen=True)
class ParentState:
    item_index: int
    problem_id: str
    path: Path
    goals: tuple[str, ...]
    tactics: tuple[str, ...]


@dataclass(frozen=True)
class TimedValue(Generic[T]):
    value: T
    wall_s: float


@dataclass(frozen=True)
class ParentCreationReport:
    startup_wall_s: float
    create_wall_s: float
    created: int


@dataclass(frozen=True)
class AttemptSnapshot:
    item_index: int
    tactic_index: int
    tactic: str
    status: str
    response_goals: tuple[str, ...]
    messages: tuple[str, ...]
    normalized_messages: tuple[str, ...]
    child_path: str | None
    reloaded_goals: tuple[str, ...]
    reload_error: str | None


@dataclass(frozen=True)
class RunReport:
    name: Literal["old", "task"]
    startup_wall_s: float
    step_wall_s: float
    reload_wall_s: float
    worker_alive: bool
    attempts: tuple[AttemptSnapshot, ...]

    @property
    def status_counts(self) -> dict[str, int]:
        return dict(Counter(attempt.status for attempt in self.attempts))


@dataclass(frozen=True)
class Mismatch:
    item_index: int
    tactic_index: int
    tactic: str
    kind: str
    old: object
    task: object


@dataclass(frozen=True)
class ComparisonSummary:
    attempt_count: int
    semantic_mismatch_count: int
    strict_response_mismatch_count: int
    status_mismatch_count: int
    response_goal_mismatch_count: int
    reloaded_goal_mismatch_count: int
    message_mismatch_count: int
    normalized_message_mismatch_count: int
    reload_error_count: int
    mismatches: tuple[Mismatch, ...]

    @property
    def semantically_equivalent(self) -> bool:
        return self.semantic_mismatch_count == 0 and self.reload_error_count == 0

    @property
    def strictly_equivalent(self) -> bool:
        return (
            self.semantically_equivalent
            and self.strict_response_mismatch_count == 0
            and self.message_mismatch_count == 0
        )


@dataclass(frozen=True)
class FairComparisonReport:
    workload: str
    signature: str
    imports: tuple[str, ...]
    project_path: str
    item_count: int
    tactics_per_item: int
    max_parallel_items: int
    parent_creation: ParentCreationReport
    old: RunReport
    task: RunReport
    comparison: ComparisonSummary

    @property
    def attempt_count(self) -> int:
        return self.item_count * self.tactics_per_item

    @property
    def old_transport_roundtrips_estimated(self) -> int:
        old_open = sum(1 for attempt in self.old.attempts if attempt.status == "open")
        return self.item_count + self.attempt_count + old_open

    @property
    def task_transport_roundtrips_estimated(self) -> int:
        return 1


def load_frozen_workload(
    path: PathLike,
    *,
    n_items: int,
    tactics_per_item: int,
) -> tuple[str, list[WorkloadItem]]:
    payload = json.loads(Path(path).read_text())
    items: list[WorkloadItem] = []
    for index, raw_item in enumerate(payload["items"][:n_items]):
        split = split_snippet(str(raw_item["root_code"]))
        tactics = tuple(str(tactic) for tactic in raw_item["tactics"][:tactics_per_item])
        items.append(
            WorkloadItem(
                item_index=index,
                problem_id=str(raw_item["problem_id"]),
                source_hash=str(raw_item["source_hash"]),
                header=split.header,
                body=split.body,
                tactics=tactics,
            )
        )
    return str(payload.get("signature", "")), items


def extract_imports(header: str) -> tuple[str, ...]:
    imports: list[str] = []
    for line in header.splitlines():
        stripped = line.strip()
        if stripped.startswith("import "):
            imports.append(stripped.removeprefix("import ").strip())
    return tuple(imports)


def require_single_header(items: list[WorkloadItem]) -> str:
    headers = {item.header for item in items}
    if len(headers) != 1:
        raise ValueError(
            "old_vs_task_sequential currently compares one header group at a time; "
            f"got {len(headers)} headers"
        )
    return items[0].header


def normalize_messages(messages: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(_LEAN_ERROR_PREFIX_RE.sub("", message) for message in messages)


def compare_runs(old: RunReport, task: RunReport) -> ComparisonSummary:
    old_by_key = {(attempt.item_index, attempt.tactic_index): attempt for attempt in old.attempts}
    task_by_key = {(attempt.item_index, attempt.tactic_index): attempt for attempt in task.attempts}
    keys = sorted(set(old_by_key) | set(task_by_key))

    mismatches: list[Mismatch] = []
    semantic_mismatch_count = 0
    strict_response_mismatch_count = 0
    status_mismatch_count = 0
    response_goal_mismatch_count = 0
    reloaded_goal_mismatch_count = 0
    message_mismatch_count = 0
    normalized_message_mismatch_count = 0
    reload_error_count = 0

    for item_index, tactic_index in keys:
        old_attempt = old_by_key.get((item_index, tactic_index))
        task_attempt = task_by_key.get((item_index, tactic_index))
        if old_attempt is None or task_attempt is None:
            semantic_mismatch_count += 1
            mismatches.append(
                Mismatch(
                    item_index=item_index,
                    tactic_index=tactic_index,
                    tactic=old_attempt.tactic if old_attempt else task_attempt.tactic,
                    kind="missing_attempt",
                    old=old_attempt is not None,
                    task=task_attempt is not None,
                )
            )
            continue

        if old_attempt.status != task_attempt.status:
            status_mismatch_count += 1
            semantic_mismatch_count += 1
            mismatches.append(
                _mismatch(old_attempt, task_attempt, "status", old_attempt.status, task_attempt.status)
            )

        if old_attempt.response_goals != task_attempt.response_goals:
            response_goal_mismatch_count += 1
            strict_response_mismatch_count += 1
            mismatches.append(
                _mismatch(
                    old_attempt,
                    task_attempt,
                    "response_goals",
                    old_attempt.response_goals,
                    task_attempt.response_goals,
                )
            )

        if old_attempt.reloaded_goals != task_attempt.reloaded_goals:
            reloaded_goal_mismatch_count += 1
            semantic_mismatch_count += 1
            mismatches.append(
                _mismatch(
                    old_attempt,
                    task_attempt,
                    "reloaded_goals",
                    old_attempt.reloaded_goals,
                    task_attempt.reloaded_goals,
                )
            )

        if old_attempt.messages != task_attempt.messages:
            message_mismatch_count += 1
            strict_response_mismatch_count += 1

        if old_attempt.normalized_messages != task_attempt.normalized_messages:
            normalized_message_mismatch_count += 1
            mismatches.append(
                _mismatch(
                    old_attempt,
                    task_attempt,
                    "normalized_messages",
                    old_attempt.normalized_messages,
                    task_attempt.normalized_messages,
                )
            )

        if old_attempt.reload_error or task_attempt.reload_error:
            reload_error_count += 1
            semantic_mismatch_count += 1
            mismatches.append(
                _mismatch(
                    old_attempt,
                    task_attempt,
                    "reload_error",
                    old_attempt.reload_error,
                    task_attempt.reload_error,
                )
            )

    return ComparisonSummary(
        attempt_count=len(keys),
        semantic_mismatch_count=semantic_mismatch_count,
        strict_response_mismatch_count=strict_response_mismatch_count,
        status_mismatch_count=status_mismatch_count,
        response_goal_mismatch_count=response_goal_mismatch_count,
        reloaded_goal_mismatch_count=reloaded_goal_mismatch_count,
        message_mismatch_count=message_mismatch_count,
        normalized_message_mismatch_count=normalized_message_mismatch_count,
        reload_error_count=reload_error_count,
        mismatches=tuple(mismatches),
    )


async def run_fair_comparison(
    *,
    workload: PathLike,
    workdir: PathLike,
    project_path: PathLike,
    n_items: int,
    tactics_per_item: int,
    timeout_seconds: int,
    buffer_limit: int,
    max_parallel_items: int = 1,
) -> FairComparisonReport:
    signature, items = load_frozen_workload(
        workload,
        n_items=n_items,
        tactics_per_item=tactics_per_item,
    )
    if not items:
        raise ValueError("workload has no items")

    header = require_single_header(items)
    imports = extract_imports(header)
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)

    parent_timed = await _timed(
        _create_parents(
            items,
            imports=imports,
            project_path=project_path,
            timeout_seconds=timeout_seconds,
            buffer_limit=buffer_limit,
            state_dir=root / "parents",
        )
    )
    parent_creation, parents = parent_timed.value

    old = await _run_old_path(
        parents,
        imports=imports,
        project_path=project_path,
        timeout_seconds=timeout_seconds,
        buffer_limit=buffer_limit,
        state_dir=root / "old_children",
    )
    task = await _run_task_path(
        parents,
        imports=imports,
        project_path=project_path,
        timeout_seconds=timeout_seconds,
        buffer_limit=buffer_limit,
        state_dir=root / "task_children",
        max_parallel_items=max_parallel_items,
    )

    return FairComparisonReport(
        workload=str(workload),
        signature=signature,
        imports=imports,
        project_path=str(project_path),
        item_count=len(parents),
        tactics_per_item=tactics_per_item,
        max_parallel_items=max_parallel_items,
        parent_creation=parent_creation,
        old=old,
        task=task,
        comparison=compare_runs(old, task),
    )


def report_to_jsonable(report: FairComparisonReport, *, max_mismatch_examples: int = 20) -> dict[str, object]:
    return {
        "workload": report.workload,
        "signature": report.signature,
        "imports": list(report.imports),
        "project_path": report.project_path,
        "item_count": report.item_count,
        "tactics_per_item": report.tactics_per_item,
        "attempt_count": report.attempt_count,
        "max_parallel_items": report.max_parallel_items,
        "parent_creation": _dataclass_to_public_dict(report.parent_creation),
        "old": _run_to_jsonable(report.old),
        "task": _run_to_jsonable(report.task),
        "transport_roundtrips_estimated": {
            "old": report.old_transport_roundtrips_estimated,
            "task": report.task_transport_roundtrips_estimated,
            "note": (
                "Counts Python-to-Lean JSON commands for the step phase only. "
                "Parent creation is shared by both paths."
            ),
        },
        "comparison": {
            "semantically_equivalent": report.comparison.semantically_equivalent,
            "strictly_equivalent": report.comparison.strictly_equivalent,
            "attempt_count": report.comparison.attempt_count,
            "semantic_mismatch_count": report.comparison.semantic_mismatch_count,
            "strict_response_mismatch_count": report.comparison.strict_response_mismatch_count,
            "status_mismatch_count": report.comparison.status_mismatch_count,
            "response_goal_mismatch_count": report.comparison.response_goal_mismatch_count,
            "reloaded_goal_mismatch_count": report.comparison.reloaded_goal_mismatch_count,
            "message_mismatch_count": report.comparison.message_mismatch_count,
            "normalized_message_mismatch_count": report.comparison.normalized_message_mismatch_count,
            "reload_error_count": report.comparison.reload_error_count,
            "mismatch_examples": [
                _dataclass_to_public_dict(mismatch)
                for mismatch in sorted(
                    report.comparison.mismatches,
                    key=_mismatch_priority,
                )[:max_mismatch_examples]
            ],
        },
    }


async def _create_parents(
    items: list[WorkloadItem],
    *,
    imports: tuple[str, ...],
    project_path: PathLike,
    timeout_seconds: int,
    buffer_limit: int,
    state_dir: Path,
) -> tuple[ParentCreationReport, list[ParentState]]:
    startup = await _timed(
        PantographWorker.create(
            imports=list(imports),
            project_path=str(project_path),
            timeout_seconds=timeout_seconds,
            buffer_limit=buffer_limit,
        )
    )
    worker = startup.value
    parents: list[ParentState] = []
    create_start = time.monotonic()
    try:
        for item in items:
            result = await worker.create_states_from_code(
                item.body,
                state_dir=state_dir / f"item_{item.item_index}",
            )
            if result.status != "open" or len(result.states) != 1:
                raise RuntimeError(
                    f"parent creation failed for item {item.item_index} "
                    f"status={result.status} states={len(result.states)} "
                    f"messages={result.messages}"
                )
            state = result.states[0]
            parents.append(
                ParentState(
                    item_index=item.item_index,
                    problem_id=item.problem_id,
                    path=state.path,
                    goals=tuple(state.goals),
                    tactics=item.tactics,
                )
            )
    finally:
        await worker.aclose()

    return (
        ParentCreationReport(
            startup_wall_s=round(startup.wall_s, 3),
            create_wall_s=round(time.monotonic() - create_start, 3),
            created=len(parents),
        ),
        parents,
    )


async def _run_old_path(
    parents: list[ParentState],
    *,
    imports: tuple[str, ...],
    project_path: PathLike,
    timeout_seconds: int,
    buffer_limit: int,
    state_dir: Path,
) -> RunReport:
    startup = await _timed(
        PantographWorker.create(
            imports=list(imports),
            project_path=str(project_path),
            timeout_seconds=timeout_seconds,
            buffer_limit=buffer_limit,
        )
    )
    worker = startup.value
    step_results: list[tuple[ParentState, list[PantographStepResult]]] = []
    step_wall_s = 0.0
    reload_wall_s = 0.0
    worker_alive = False
    try:
        step_start = time.monotonic()
        for parent in parents:
            results = await worker.step_state_with_tactics(
                parent.path,
                list(parent.tactics),
                state_dir=state_dir / f"item_{parent.item_index}",
            )
            step_results.append((parent, results))
        step_wall_s = time.monotonic() - step_start

        reload_start = time.monotonic()
        attempts = await _snapshot_results(worker, step_results)
        reload_wall_s = time.monotonic() - reload_start
        worker_alive = worker.is_alive()
    finally:
        await worker.aclose()

    return RunReport(
        name="old",
        startup_wall_s=round(startup.wall_s, 3),
        step_wall_s=round(step_wall_s, 3),
        reload_wall_s=round(reload_wall_s, 3),
        worker_alive=worker_alive,
        attempts=tuple(attempts),
    )


async def _run_task_path(
    parents: list[ParentState],
    *,
    imports: tuple[str, ...],
    project_path: PathLike,
    timeout_seconds: int,
    buffer_limit: int,
    state_dir: Path,
    max_parallel_items: int,
) -> RunReport:
    startup = await _timed(
        PantographWorker.create(
            imports=list(imports),
            project_path=str(project_path),
            timeout_seconds=timeout_seconds,
            buffer_limit=buffer_limit,
        )
    )
    worker = startup.value
    step_wall_s = 0.0
    reload_wall_s = 0.0
    worker_alive = False
    try:
        inputs = [
            PantographBatchStepInput(
                item_index=parent.item_index,
                state_path=parent.path,
                tactics=list(parent.tactics),
            )
            for parent in parents
        ]
        parent_by_index = {parent.item_index: parent for parent in parents}
        step_start = time.monotonic()
        batch_results = await worker.step_state_batch_with_tactics(
            inputs,
            state_dir=state_dir,
            max_parallel_items=max_parallel_items,
        )
        step_wall_s = time.monotonic() - step_start
        step_results = [
            (parent_by_index[item_result.item_index], item_result.results)
            for item_result in batch_results
        ]

        reload_start = time.monotonic()
        attempts = await _snapshot_results(worker, step_results)
        reload_wall_s = time.monotonic() - reload_start
        worker_alive = worker.is_alive()
    finally:
        await worker.aclose()

    return RunReport(
        name="task",
        startup_wall_s=round(startup.wall_s, 3),
        step_wall_s=round(step_wall_s, 3),
        reload_wall_s=round(reload_wall_s, 3),
        worker_alive=worker_alive,
        attempts=tuple(attempts),
    )


async def _snapshot_results(
    worker: PantographWorker,
    step_results: list[tuple[ParentState, list[PantographStepResult]]],
) -> list[AttemptSnapshot]:
    attempts: list[AttemptSnapshot] = []
    for parent, results in step_results:
        for tactic_index, result in enumerate(results):
            reloaded_goals: tuple[str, ...] = ()
            reload_error: str | None = None
            if result.status == "open":
                if result.state_path is None:
                    reload_error = "open result has no child path"
                else:
                    try:
                        server = getattr(worker, "_server")
                        loaded = await server.goal_load_async(str(result.state_path))
                        reloaded_goals = tuple(goal_state_to_goal_texts(loaded))
                    except Exception as exc:  # noqa: BLE001
                        reload_error = str(exc)

            messages = tuple(result.messages)
            attempts.append(
                AttemptSnapshot(
                    item_index=parent.item_index,
                    tactic_index=tactic_index,
                    tactic=result.tactic,
                    status=result.status,
                    response_goals=tuple(result.goals),
                    messages=messages,
                    normalized_messages=normalize_messages(messages),
                    child_path=str(result.state_path) if result.state_path else None,
                    reloaded_goals=reloaded_goals,
                    reload_error=reload_error,
                )
            )
    return attempts


async def _timed(awaitable: Any) -> TimedValue[Any]:
    start = time.monotonic()
    value = await awaitable
    return TimedValue(value=value, wall_s=time.monotonic() - start)


def _mismatch(
    old_attempt: AttemptSnapshot,
    task_attempt: AttemptSnapshot,
    kind: str,
    old: object,
    task: object,
) -> Mismatch:
    return Mismatch(
        item_index=old_attempt.item_index,
        tactic_index=old_attempt.tactic_index,
        tactic=old_attempt.tactic,
        kind=kind,
        old=old,
        task=task,
    )


def _mismatch_priority(mismatch: Mismatch) -> tuple[int, int, int]:
    priority_by_kind = {
        "missing_attempt": 0,
        "status": 0,
        "reload_error": 0,
        "reloaded_goals": 1,
        "response_goals": 2,
        "normalized_messages": 3,
    }
    return (
        priority_by_kind.get(mismatch.kind, 4),
        mismatch.item_index,
        mismatch.tactic_index,
    )


def _run_to_jsonable(run: RunReport) -> dict[str, object]:
    return {
        "name": run.name,
        "startup_wall_s": run.startup_wall_s,
        "step_wall_s": run.step_wall_s,
        "reload_wall_s": run.reload_wall_s,
        "worker_alive": run.worker_alive,
        "status_counts": run.status_counts,
    }


def _dataclass_to_public_dict(value: object) -> dict[str, object]:
    return {
        key: _jsonable(raw_value)
        for key, raw_value in vars(value).items()
        if not key.startswith("_")
    }


def _jsonable(value: object) -> object:
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value
