from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pantograph

from .pantograph_normalize import (
    exception_to_messages,
    goal_state_to_goal_texts,
    messages_to_texts,
)
from .schemas_exec import ExecStatus


@dataclass(frozen=True)
class PantographSavedState:
    path: Path
    goals: list[str]


@dataclass(frozen=True)
class PantographCreateResult:
    status: ExecStatus
    states: list[PantographSavedState] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PantographStepResult:
    tactic: str
    status: ExecStatus
    state_path: Path | None = None
    goals: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


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

    def close(self) -> None:
        close = getattr(self._server, "_close", None)
        if close is not None:
            close()

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
