from __future__ import annotations

from collections.abc import Callable

from .async_client import AsyncKiminaClient
from .exec_env import AsyncLeanExecBatcher, AsyncLeanExecEnv
from .exec_models import (
    ExecCancelResponse,
    ExecCleanupResponse,
    ExecCreateStateItem,
    ExecCreateStatesResponse,
    ExecLimitsResponse,
    ExecStatsResponse,
    ExecStepBatchItem,
    ExecStepBatchResponse,
    ExecStepBatchResult,
    ExecVerifyItem,
    ExecVerifyResponse,
    ExecVerifyResult,
)


class AsyncLeanExecBackend:
    """Single entry point for a consumer (search/RL) talking to a Lean ``/exec`` server.

    This is the object an external proof-search or RL consumer constructs and
    manages. It owns the three internal layers so callers never juggle them
    directly:

    - :class:`AsyncKiminaClient` — HTTP transport (one method per endpoint),
    - :class:`AsyncLeanExecEnv` — proof-state vocabulary + default env_profile/timeouts,
    - :class:`AsyncLeanExecBatcher` — coalesces concurrent ``step`` calls into
      server-sized ``/exec/step_batch`` requests.

    The server always runs as a **separate process** (its Docker image or
    ``python -m server`` from a checkout); this backend only connects to it over
    HTTP via ``api_url`` — it never launches the server in-process.

    Construct with :meth:`connect` (reads ``/exec/limits`` to size batching) and
    use as an async context manager so the batcher's background tasks and the
    HTTP session are cleaned up::

        async with await AsyncLeanExecBackend.connect(
            "http://lean-exec:8000", env_profile="lean4.29.1_mathlib"
        ) as backend:
            roots = await backend.create_states("thm:a0", code_with_sorry)
            result = await backend.step(node_id, state_token, tactics)   # coalesced
            verdict = await backend.verify_one("thm:a0", assembled_proof, "thm")

    The composed layers remain reachable via :attr:`client`, :attr:`env`, and
    :attr:`batcher` for escape hatches; routine use should not need them.
    """

    def __init__(
        self,
        *,
        client: AsyncKiminaClient,
        env: AsyncLeanExecEnv,
        batcher: AsyncLeanExecBatcher,
    ) -> None:
        self.client = client
        self.env = env
        self.batcher = batcher

    @classmethod
    async def connect(
        cls,
        api_url: str | None = None,
        *,
        env_profile: str = "default",
        api_key: str | None = None,
        timeout_ms: int = 5000,
        acquire_timeout_ms: int | None = None,
        step_timeout_ms: int | None = None,
        http_timeout: int = 60,
        n_retries: int = 3,
        max_overloaded_retries: int = 3,
        overload_backoff_seconds: float = 0.1,
        item_id_from_node_id: Callable[[str], str] | None = None,
    ) -> "AsyncLeanExecBackend":
        client = AsyncKiminaClient(
            api_url,
            api_key=api_key,
            http_timeout=http_timeout,
            n_retries=n_retries,
        )
        env = AsyncLeanExecEnv(
            client,
            env_profile=env_profile,
            timeout_ms=timeout_ms,
            acquire_timeout_ms=acquire_timeout_ms,
            step_timeout_ms=step_timeout_ms,
        )
        batcher = await AsyncLeanExecBatcher.from_server_limits(
            env,
            max_overloaded_retries=max_overloaded_retries,
            overload_backoff_seconds=overload_backoff_seconds,
            item_id_from_node_id=item_id_from_node_id,
        )
        return cls(client=client, env=env, batcher=batcher)

    # --- roots: initialize states from sorry'd code ---
    async def create_states(
        self,
        item_id: str,
        code: str,
        *,
        env_profile: str | None = None,
        timeout_ms: int | None = None,
        acquire_timeout_ms: int | None = None,
        step_timeout_ms: int | None = None,
    ) -> ExecCreateStatesResponse:
        return await self.env.create_states(
            item_id,
            code,
            env_profile=env_profile,
            timeout_ms=timeout_ms,
            acquire_timeout_ms=acquire_timeout_ms,
            step_timeout_ms=step_timeout_ms,
        )

    async def create_states_batch(
        self,
        items: list[ExecCreateStateItem],
        *,
        env_profile: str | None = None,
    ) -> ExecCreateStatesResponse:
        return await self.env.create_states_batch(items, env_profile=env_profile)

    # --- hot path: per-node steps are coalesced by the batcher ---
    async def step(
        self,
        node_id: str,
        state_token: str,
        tactics: list[str],
        *,
        timeout_ms: int | None = None,
        acquire_timeout_ms: int | None = None,
        step_timeout_ms: int | None = None,
        goal_group: list[int] | None = None,
    ) -> ExecStepBatchResult:
        return await self.batcher.submit_step(
            node_id,
            state_token,
            tactics,
            timeout_ms=timeout_ms,
            acquire_timeout_ms=acquire_timeout_ms,
            step_timeout_ms=step_timeout_ms,
            goal_group=goal_group,
        )

    async def step_batch(
        self,
        items: list[ExecStepBatchItem],
    ) -> ExecStepBatchResponse:
        """Send a pre-built batch directly, bypassing the coalescer."""
        return await self.env.step_batch(items)

    # --- acceptance: warm-pool proof certification ---
    async def verify(
        self,
        items: list[ExecVerifyItem],
        *,
        env_profile: str | None = None,
    ) -> ExecVerifyResponse:
        return await self.env.verify(items, env_profile=env_profile)

    async def verify_one(
        self,
        item_id: str,
        code: str,
        theorem_name: str,
        *,
        allowed_axioms: list[str] | None = None,
        env_profile: str | None = None,
        timeout_ms: int | None = None,
        acquire_timeout_ms: int | None = None,
        step_timeout_ms: int | None = None,
    ) -> ExecVerifyResult:
        return await self.env.verify_one(
            item_id,
            code,
            theorem_name,
            allowed_axioms=allowed_axioms,
            env_profile=env_profile,
            timeout_ms=timeout_ms,
            acquire_timeout_ms=acquire_timeout_ms,
            step_timeout_ms=step_timeout_ms,
        )

    # --- operations ---
    async def cleanup(self, item_ids: list[str]) -> ExecCleanupResponse:
        return await self.env.cleanup(item_ids)

    async def cancel(self, item_ids: list[str]) -> ExecCancelResponse:
        return await self.env.cancel(item_ids)

    async def limits(self) -> ExecLimitsResponse:
        return await self.env.limits()

    async def stats(self) -> ExecStatsResponse:
        return await self.env.stats()

    # --- lifecycle ---
    async def aclose(self) -> None:
        await self.batcher.close()
        await self.client.aclose()

    async def __aenter__(self) -> "AsyncLeanExecBackend":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        await self.aclose()
