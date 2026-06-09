from __future__ import annotations

import asyncio
import re
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar, cast

import pantograph  # pyright: ignore[reportMissingTypeStubs]
import psutil
from pantograph.expr import Site  # pyright: ignore[reportMissingTypeStubs]

from .pantograph_goal import PantographGoal
from .pantograph_normalize import (
    exception_to_exec_messages,
    goal_state_to_goals,
    messages_to_exec_messages,
)
from .schemas_exec import ExecMessage, ExecStatus, ExecVerifyStatus

# Worker startup options handed to ``pantograph.Server``. ``printDependentMVars``
# makes Pantograph populate each goal's ``sibling_dep`` (the metavariable-coupling
# signal the search engine's split rule needs).
DEFAULT_PANTOGRAPH_OPTIONS: dict[str, Any] = {"printDependentMVars": True}
T = TypeVar("T")


def _empty_saved_states() -> list["PantographSavedState"]:
    return []


def _empty_messages() -> list[ExecMessage]:
    return []


def _empty_goals() -> list[PantographGoal]:
    return []


@dataclass(frozen=True)
class PantographSavedState:
    path: Path
    goals: list[PantographGoal]


@dataclass(frozen=True)
class PantographDebugInfo:
    cpu_max: float
    memory_max: int


@dataclass(frozen=True)
class PantographCreateResult:
    status: ExecStatus
    states: list[PantographSavedState] = field(default_factory=_empty_saved_states)
    messages: list[ExecMessage] = field(default_factory=_empty_messages)
    debug: PantographDebugInfo | None = None


@dataclass(frozen=True)
class PantographStepResult:
    tactic: str
    status: ExecStatus
    state_path: Path | None = None
    goals: list[PantographGoal] = field(default_factory=_empty_goals)
    messages: list[ExecMessage] = field(default_factory=_empty_messages)
    debug: PantographDebugInfo | None = None


@dataclass(frozen=True)
class PantographVerifyResult:
    status: ExecVerifyStatus
    axioms: list[str] = field(default_factory=list[str])
    messages: list[ExecMessage] = field(default_factory=_empty_messages)
    debug: PantographDebugInfo | None = None


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
        options: dict[str, Any] | None = None,
    ) -> "PantographWorker":
        server = await pantograph.Server.create(
            imports=imports,
            project_path=project_path,
            lean_path=lean_path,
            timeout=timeout_seconds,
            buffer_limit=buffer_limit,
            options=DEFAULT_PANTOGRAPH_OPTIONS if options is None else options,
        )
        return cls(server)

    async def create_states_from_code(
        self,
        code: str,
        *,
        state_dir: Path,
        debug: bool = False,
    ) -> PantographCreateResult:
        if debug:
            return await self._with_debug(
                lambda: self._create_states_from_code(code, state_dir=state_dir)
            )
        return await self._create_states_from_code(code, state_dir=state_dir)

    async def _create_states_from_code(
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
                        goals=goal_state_to_goals(target.goal_state),
                    )
                )
            return PantographCreateResult(status="open", states=states)
        except Exception as exc:
            self._unlink_paths(written_paths)
            return PantographCreateResult(
                status="error",
                messages=exception_to_exec_messages(exc),
            )

    async def step_state_with_tactics(
        self,
        state_path: Path,
        tactics: list[str],
        *,
        state_dir: Path,
        goal_id: int | None = None,
        auto_resume: bool | None = None,
        debug: bool = False,
    ) -> list[PantographStepResult]:
        if debug:
            return await self._with_debug(
                lambda: self._step_state_with_tactics(
                    state_path,
                    tactics,
                    state_dir=state_dir,
                    goal_id=goal_id,
                    auto_resume=auto_resume,
                )
            )
        return await self._step_state_with_tactics(
            state_path,
            tactics,
            state_dir=state_dir,
            goal_id=goal_id,
            auto_resume=auto_resume,
        )

    async def _step_state_with_tactics(
        self,
        state_path: Path,
        tactics: list[str],
        *,
        state_dir: Path,
        goal_id: int | None = None,
        auto_resume: bool | None = None,
    ) -> list[PantographStepResult]:
        state_dir.mkdir(parents=True, exist_ok=True)
        try:
            parent_state = await self._server.goal_load_async(str(state_path))
        except Exception as exc:
            messages = exception_to_exec_messages(exc)
            return [
                PantographStepResult(
                    tactic=tactic,
                    status="error",
                    messages=messages,
                )
                for tactic in tactics
            ]

        # ``Site()`` (both fields None) serialises to ``{}`` and is therefore
        # identical to the legacy whole-state step; passing a ``goal_id`` (with
        # ``auto_resume=False``) focuses that goal and suspends its siblings even
        # in automatic mode, yielding an undragged single-goal subtree.
        site = Site(goal_id=goal_id, auto_resume=auto_resume)
        results: list[PantographStepResult] = []
        for tactic in tactics:
            results.append(
                await self._step_one_tactic(
                    parent_state, tactic, site=site, state_dir=state_dir
                )
            )
        return results

    async def verify_complete_proof(
        self,
        code: str,
        *,
        theorem_name: str,
        allowed_axioms: list[str],
        debug: bool = False,
    ) -> PantographVerifyResult:
        if debug:
            return await self._with_debug(
                lambda: self._verify_complete_proof(
                    code,
                    theorem_name=theorem_name,
                    allowed_axioms=allowed_axioms,
                )
            )
        return await self._verify_complete_proof(
            code,
            theorem_name=theorem_name,
            allowed_axioms=allowed_axioms,
        )

    async def _verify_complete_proof(
        self,
        code: str,
        *,
        theorem_name: str,
        allowed_axioms: list[str],
    ) -> PantographVerifyResult:
        try:
            units = await self._server.check_compile_async(
                f"{code.rstrip()}\n#print axioms {theorem_name}",
                new_constants=True,
            )
        except Exception as exc:
            return PantographVerifyResult(
                status="error",
                messages=exception_to_exec_messages(exc),
            )

        messages: list[ExecMessage] = []
        raw_messages: list[Any] = []
        for unit in units:
            unit_messages = getattr(unit, "messages", [])
            raw_messages.extend(unit_messages)
            messages.extend(messages_to_exec_messages(unit_messages))

        axioms = _extract_axioms(messages)
        disallowed_axioms = sorted(set(axioms) - set(allowed_axioms))
        has_errors = any(message.severity == "error" for message in messages)
        has_sorry = any(
            _message_mentions_sorry(message)
            or getattr(raw_message, "kind", None) == "hasSorry"
            for raw_message, message in zip(
                raw_messages,
                messages_to_exec_messages(raw_messages),
            )
        )
        if disallowed_axioms:
            messages.append(
                ExecMessage(
                    severity="error",
                    data="disallowed axioms: " + ", ".join(disallowed_axioms),
                )
            )
        status: ExecVerifyStatus = (
            "rejected" if has_errors or has_sorry or disallowed_axioms else "accepted"
        )
        return PantographVerifyResult(status=status, axioms=axioms, messages=messages)

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
        site: Site,
        state_dir: Path,
    ) -> PantographStepResult:
        child_path: Path | None = None
        try:
            child_state = await self._server.goal_tactic_async(
                parent_state, tactic, site=site
            )
            messages = messages_to_exec_messages(child_state.messages)
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
                goals=goal_state_to_goals(child_state),
                messages=messages,
            )
        except Exception as exc:
            if child_path is not None:
                child_path.unlink(missing_ok=True)
            return PantographStepResult(
                tactic=tactic,
                status="error",
                messages=exception_to_exec_messages(exc),
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

    async def _with_debug(
        self,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        proc = self._ps_process()
        if proc is None:
            return await operation()

        loop = asyncio.get_running_loop()
        start_time = loop.time()
        last_time = start_time
        start_cpu = self._sum_cpu_times(proc)
        last_cpu = start_cpu
        cpu_max = 0.0
        memory_max = self._memory_bytes(proc)
        stop = asyncio.Event()

        async def monitor() -> None:
            nonlocal cpu_max, memory_max, last_cpu, last_time
            while not stop.is_set() and self.is_alive():
                await asyncio.sleep(0.1)
                now = loop.time()
                current_cpu = self._sum_cpu_times(proc)
                delta_t = max(now - last_time, 1e-9)
                cpu_max = max(cpu_max, ((current_cpu - last_cpu) / delta_t) * 100)
                memory_max = max(memory_max, self._memory_bytes(proc))
                last_cpu = current_cpu
                last_time = now

        monitor_task = asyncio.create_task(monitor())
        try:
            result = await operation()
        finally:
            stop.set()
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

        end_time = loop.time()
        end_cpu = self._sum_cpu_times(proc)
        elapsed = max(end_time - start_time, 1e-9)
        debug = PantographDebugInfo(
            cpu_max=max(cpu_max, ((end_cpu - start_cpu) / elapsed) * 100),
            memory_max=max(memory_max, self._memory_bytes(proc)),
        )
        return _attach_debug(result, debug)

    def _ps_process(self) -> psutil.Process | None:
        proc = getattr(self._server, "proc", None)
        if proc is None or proc.returncode is not None:
            return None
        return psutil.Process(proc.pid)

    @staticmethod
    def _sum_cpu_times(proc: psutil.Process) -> float:
        total = proc.cpu_times().user + proc.cpu_times().system
        for child in proc.children(recursive=True):
            try:
                times = child.cpu_times()
            except psutil.Error:
                continue
            total += times.user + times.system
        return float(total)

    @staticmethod
    def _memory_bytes(proc: psutil.Process) -> int:
        try:
            total = proc.memory_info().rss
        except psutil.Error:
            return 0
        for child in proc.children(recursive=True):
            try:
                total += child.memory_info().rss
            except psutil.Error:
                continue
        return int(total)


def _attach_debug(result: T, debug: PantographDebugInfo) -> T:
    if isinstance(result, PantographCreateResult):
        return replace(result, debug=debug)  # type: ignore[return-value]
    if isinstance(result, PantographVerifyResult):
        return replace(result, debug=debug)  # type: ignore[return-value]
    if isinstance(result, list):
        step_results = cast(list[PantographStepResult], result)
        return cast(T, [replace(item, debug=debug) for item in step_results])
    return result


def _extract_axioms(messages: list[ExecMessage]) -> list[str]:
    for message in messages:
        match = re.search(r"depends on axioms: \[(.*)\]", message.data)
        if match is not None:
            raw = match.group(1).strip()
            if not raw:
                return []
            return [axiom.strip() for axiom in raw.split(",") if axiom.strip()]
        if "does not depend on any axioms" in message.data:
            return []
    return []


def _message_mentions_sorry(message: ExecMessage) -> bool:
    return "sorry" in message.data.lower()
