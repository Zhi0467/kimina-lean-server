from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pantograph  # type: ignore[reportMissingTypeStubs]
import psutil

from .pantograph_normalize import (
    exception_to_messages,
    goal_payloads_to_goal_texts,
    goal_state_to_goal_texts,
    messages_to_texts,
)
from .schemas_exec import ExecStatus


@dataclass(frozen=True)
class PantographSavedState:
    path: Path
    goals: list[str]


def _empty_saved_states() -> list[PantographSavedState]:
    return []


def _empty_step_results() -> list[PantographStepResult]:
    return []


def _empty_strings() -> list[str]:
    return []


@dataclass(frozen=True)
class PantographCreateResult:
    status: ExecStatus
    states: list[PantographSavedState] = field(default_factory=_empty_saved_states)
    messages: list[str] = field(default_factory=_empty_strings)


@dataclass(frozen=True)
class PantographStepResult:
    tactic: str
    status: ExecStatus
    state_path: Path | None = None
    goals: list[str] = field(default_factory=_empty_strings)
    messages: list[str] = field(default_factory=_empty_strings)


@dataclass(frozen=True)
class PantographBatchStepInput:
    item_index: int
    state_path: Path
    tactics: list[str]


@dataclass(frozen=True)
class PantographBatchStepItemResult:
    item_index: int
    results: list[PantographStepResult] = field(default_factory=_empty_step_results)


class PantographWorker:
    def __init__(self, server: Any) -> None:
        self._server = server

    @classmethod
    async def create(
        cls,
        *,
        imports: list[str],
        project_path: str | None = None,
        lean_path: str | None = None,
        timeout_seconds: int = 120,
        buffer_limit: int | None = 1_000_000,
    ) -> "PantographWorker":
        server = await pantograph.Server.create(
            imports=imports,
            project_path=project_path,
            lean_path=lean_path,
            timeout=timeout_seconds,
            buffer_limit=buffer_limit,
        )
        return cls(server)

    async def create_states_from_code(
        self,
        code: str,
        *,
        state_dir: Path,
    ) -> PantographCreateResult:
        state_dir.mkdir(parents=True, exist_ok=True)
        written_paths: list[Path] = []
        try:
            targets = await self._server.load_sorry_async(code)
            if not targets:
                return PantographCreateResult(status="complete")

            states: list[PantographSavedState] = []
            for target in targets:
                state_path = self._allocate_state_path(state_dir)
                written_paths.append(state_path)
                await self._server.goal_save_async(target.goal_state, str(state_path))
                states.append(
                    PantographSavedState(
                        path=state_path,
                        goals=goal_state_to_goal_texts(target.goal_state),
                    )
                )
            return PantographCreateResult(status="open", states=states)
        except Exception as exc:
            self._unlink_paths(written_paths)
            return PantographCreateResult(
                status="error",
                messages=exception_to_messages(exc),
            )

    async def step_state_with_tactics(
        self,
        state_path: Path,
        tactics: list[str],
        *,
        state_dir: Path,
    ) -> list[PantographStepResult]:
        state_dir.mkdir(parents=True, exist_ok=True)
        try:
            parent_state = await self._server.goal_load_async(str(state_path))
        except Exception as exc:
            messages = exception_to_messages(exc)
            return [
                PantographStepResult(
                    tactic=tactic,
                    status="error",
                    messages=messages,
                )
                for tactic in tactics
            ]

        results: list[PantographStepResult] = []
        for tactic in tactics:
            results.append(
                await self._step_one_tactic(parent_state, tactic, state_dir=state_dir)
            )
        return results

    async def step_state_batch_with_tactics(
        self,
        items: list[PantographBatchStepInput],
        *,
        state_dir: Path,
        max_parallel_items: int,
    ) -> list[PantographBatchStepItemResult]:
        state_dir.mkdir(parents=True, exist_ok=True)
        try:
            payload = [
                {
                    "itemIdx": item.item_index,
                    "parentPath": str(item.state_path),
                    "tactics": item.tactics,
                }
                for item in items
            ]
            response = await self._server.goal_step_batch_async(
                payload,
                output_dir=str(state_dir),
                max_parallel_items=max_parallel_items,
            )
        except Exception as exc:
            messages = exception_to_messages(exc)
            return [
                PantographBatchStepItemResult(
                    item_index=item.item_index,
                    results=[
                        PantographStepResult(
                            tactic=tactic,
                            status="error",
                            messages=messages,
                        )
                        for tactic in item.tactics
                    ],
                )
                for item in items
            ]

        input_by_index = {item.item_index: item for item in items}
        returned: dict[int, PantographBatchStepItemResult] = {}
        for item_result in response.get("items", []):
            item_index = int(item_result["itemIdx"])
            attempts = item_result.get("results", [])
            returned[item_index] = PantographBatchStepItemResult(
                item_index=item_index,
                results=[self._parse_batch_attempt(attempt) for attempt in attempts],
            )

        results: list[PantographBatchStepItemResult] = []
        for item in items:
            if item.item_index in returned:
                results.append(returned[item.item_index])
                continue
            results.append(
                PantographBatchStepItemResult(
                    item_index=item.item_index,
                    results=[
                        PantographStepResult(
                            tactic=tactic,
                            status="error",
                            messages=["Pantograph batch did not return item result"],
                        )
                        for tactic in input_by_index[item.item_index].tactics
                    ],
                )
            )
        return results

    def is_alive(self) -> bool:
        """Whether the underlying Pantograph subprocess is still usable.

        PyPantograph nulls ``server.proc`` (via ``_close``) whenever a command
        times out or its output cannot be decoded, so a missing/exited ``proc``
        means the worker is dead and must not be recycled into the pool.
        """
        proc = getattr(self._server, "proc", None)
        if proc is None:
            return False
        return proc.returncode is None

    @property
    def pid(self) -> int | None:
        proc = getattr(self._server, "proc", None)
        if proc is None:
            return None
        return cast(int, proc.pid)

    def process_tree_rss_bytes(self) -> int | None:
        pid = self.pid
        if pid is None:
            return None
        try:
            process = psutil.Process(pid)
            total = int(process.memory_info().rss)
            for child in process.children(recursive=True):
                try:
                    total += int(child.memory_info().rss)
                except psutil.Error:
                    continue
            return total
        except psutil.Error:
            return None

    def close(self) -> None:
        close = getattr(self._server, "_close", None)
        if close is not None:
            close()

    async def agc(self) -> None:
        # GoalState.__del__ enqueues freed server-side states for deletion as
        # soon as the request's locals are released by refcounting, so an
        # explicit (process-global, blocking) gc.collect() is unnecessary here.
        await self._server.gc_async()

    def set_timeout_seconds(self, timeout_seconds: int) -> None:
        self._server.timeout = timeout_seconds

    async def aclose(self) -> None:
        proc = getattr(self._server, "proc", None)
        if proc is None:
            return
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        self._server.proc = None

    async def _step_one_tactic(
        self,
        parent_state: Any,
        tactic: str,
        *,
        state_dir: Path,
    ) -> PantographStepResult:
        child_path: Path | None = None
        try:
            child_state = await self._server.goal_tactic_async(parent_state, tactic)
            messages = messages_to_texts(child_state.messages)
            if child_state.is_solved:
                return PantographStepResult(
                    tactic=tactic,
                    status="complete",
                    messages=messages,
                )

            child_path = self._allocate_state_path(state_dir)
            await self._server.goal_save_async(child_state, str(child_path))
            return PantographStepResult(
                tactic=tactic,
                status="open",
                state_path=child_path,
                goals=goal_state_to_goal_texts(child_state),
                messages=messages,
            )
        except Exception as exc:
            if child_path is not None:
                child_path.unlink(missing_ok=True)
            return PantographStepResult(
                tactic=tactic,
                status="error",
                messages=exception_to_messages(exc),
            )

    @staticmethod
    def _parse_batch_attempt(payload: dict[str, Any]) -> PantographStepResult:
        status = payload.get("status")
        if status not in {"open", "complete", "error"}:
            status = "error"
        messages = messages_to_texts(payload.get("messages", []))
        for key in ("failure", "parseError"):
            if payload.get(key):
                messages.append(str(payload[key]))
        child_path = None
        if payload.get("childPath"):
            child_path = Path(payload["childPath"])
        return PantographStepResult(
            tactic=str(payload.get("tactic", "")),
            status=cast(ExecStatus, status),
            state_path=child_path,
            goals=goal_payloads_to_goal_texts(payload.get("goals", [])),
            messages=messages,
        )

    @staticmethod
    def _allocate_state_path(state_dir: Path) -> Path:
        with tempfile.NamedTemporaryFile(
            dir=state_dir,
            prefix="pg_",
            suffix=".bin",
            delete=False,
        ) as tmp:
            return Path(tmp.name)

    @staticmethod
    def _unlink_paths(paths: list[Path]) -> None:
        for path in paths:
            path.unlink(missing_ok=True)
