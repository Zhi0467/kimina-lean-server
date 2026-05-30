from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kimina_client import (
    AsyncKiminaClient,
    AsyncLeanExecBatcher,
    AsyncLeanExecEnv,
    ExecCancelResponse,
    ExecCleanupResponse,
    ExecCreateStateItem,
    ExecCreateStatesResponse,
    ExecLimitsResponse,
    ExecMicrobatchJournal,
    ExecStepBatchItem,
    ExecStepBatchRequest,
    ExecStepBatchResponse,
    ExecStepBatchResult,
    ExecStepResult,
    UncertainMicrobatchError,
)


class RecordingAsyncKiminaClient(AsyncKiminaClient):
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.api_url = "http://lean.example"
        self.api_key = None
        self.headers = {}
        self.http_timeout = 60
        self.n_retries = 0
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any] | None, str]] = []

    async def _query(
        self,
        url: str,
        payload: dict[str, Any] | None = None,
        method: str = "POST",
    ) -> Any:
        self.calls.append((url, payload, method))
        return self.responses.pop(0)


class FakeLeanExecEnv:
    def __init__(self) -> None:
        self.timeout_ms = 5000
        self.batches: list[list[ExecStepBatchItem]] = []

    async def step_batch(
        self,
        items: list[ExecStepBatchItem],
    ) -> ExecStepBatchResponse:
        self.batches.append(items)
        return ExecStepBatchResponse(
            items=[
                ExecStepBatchResult(
                    node_id=item.node_id,
                    results=[
                        ExecStepResult(
                            tactic=item.tactics[0],
                            status="complete",
                        )
                    ],
                )
                for item in items
            ]
        )

    async def limits(self) -> ExecLimitsResponse:
        return ExecLimitsResponse(
            max_items_per_step_batch=16,
            max_tactics_per_step_item=8,
            max_attempts_per_step_batch=128,
            max_create_items_per_request=16,
            max_pantograph_workers=4,
            max_lean_processes_per_env_profile=4,
            max_in_flight_exec_requests=4,
            max_queued_exec_requests=4,
            max_acquire_timeout_ms=600_000,
            max_step_timeout_ms=600_000,
            recommended_items_per_step_batch=2,
            recommended_in_flight_step_batches=2,
        )


class BlockingFakeLeanExecEnv(FakeLeanExecEnv):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def step_batch(
        self,
        items: list[ExecStepBatchItem],
    ) -> ExecStepBatchResponse:
        self.started.set()
        await self.release.wait()
        return await super().step_batch(items)


class OverloadedThenCompleteEnv(FakeLeanExecEnv):
    async def step_batch(
        self,
        items: list[ExecStepBatchItem],
    ) -> ExecStepBatchResponse:
        if not self.batches:
            self.batches.append(items)
            return ExecStepBatchResponse(
                items=[
                    ExecStepBatchResult(
                        node_id=item.node_id,
                        results=[
                            ExecStepResult(
                                tactic=tactic,
                                status="overloaded",
                            )
                            for tactic in item.tactics
                        ],
                    )
                    for item in items
                ]
            )
        return await FakeLeanExecEnv.step_batch(self, items)


class OverlapDetectingEnv(FakeLeanExecEnv):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.max_active = 0

    async def step_batch(
        self,
        items: list[ExecStepBatchItem],
    ) -> ExecStepBatchResponse:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            return await super().step_batch(items)
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_async_client_exec_methods_build_stable_payloads() -> None:
    client = RecordingAsyncKiminaClient(
        [
            {
                "items": [
                    {
                        "item_id": "run_1:thm:attempt_1",
                        "status": "open",
                        "states": [{"state_token": "st_root", "goals": ["⊢ True"]}],
                        "messages": [],
                    }
                ]
            },
            {
                "items": [
                    {
                        "node_id": "run_1:thm:attempt_1:n0",
                        "results": [{"tactic": "trivial", "status": "complete"}],
                    }
                ]
            },
            {
                "deleted_items": [
                    {
                        "item_id": "run_1:thm:attempt_1",
                        "status": "deleted",
                        "deleted_states": 1,
                        "deleted_bytes": 7,
                    }
                ]
            },
            {"items": [{"item_id": "run_1:thm:attempt_1", "status": "drained"}]},
            {
                "max_items_per_step_batch": 16,
                "max_tactics_per_step_item": 8,
                "max_attempts_per_step_batch": 128,
                "max_create_items_per_request": 16,
                "max_pantograph_workers": 4,
                "max_lean_processes_per_env_profile": 4,
                "max_in_flight_exec_requests": 4,
                "max_queued_exec_requests": 4,
                "max_acquire_timeout_ms": 600000,
                "max_step_timeout_ms": 600000,
                "recommended_items_per_step_batch": 2,
                "recommended_in_flight_step_batches": 2,
            },
        ]
    )

    create = await client.exec_create_states(
        "lean_init",
        [
            ExecCreateStateItem(
                item_id="run_1:thm:attempt_1",
                code="theorem t : True := by\n  sorry",
                timeout_ms=1000,
            )
        ],
    )
    step = await client.exec_step_batch(
        [
            ExecStepBatchItem(
                node_id="run_1:thm:attempt_1:n0",
                state_token="st_root",
                tactics=["trivial"],
                timeout_ms=1000,
            )
        ]
    )
    cleanup = await client.exec_cleanup(["run_1:thm:attempt_1"])
    cancel = await client.exec_cancel(["run_1:thm:attempt_1"])
    limits = await client.exec_limits()

    assert isinstance(create, ExecCreateStatesResponse)
    assert create.items[0].states[0].state_token == "st_root"
    assert step.items[0].results[0].status == "complete"
    assert isinstance(cleanup, ExecCleanupResponse)
    assert cleanup.deleted_items[0].deleted_states == 1
    assert isinstance(cancel, ExecCancelResponse)
    assert cancel.items[0].status == "drained"
    assert limits.recommended_items_per_step_batch == 2
    assert client.calls == [
        (
            "http://lean.example/exec/create_states",
            {
                "env_profile": "lean_init",
                "items": [
                    {
                        "item_id": "run_1:thm:attempt_1",
                        "code": "theorem t : True := by\n  sorry",
                        "acquire_timeout_ms": 1000,
                        "step_timeout_ms": 1000,
                    }
                ],
            },
            "POST",
        ),
        (
            "http://lean.example/exec/step_batch",
            {
                "items": [
                    {
                        "node_id": "run_1:thm:attempt_1:n0",
                        "state_token": "st_root",
                        "tactics": ["trivial"],
                        "acquire_timeout_ms": 1000,
                        "step_timeout_ms": 1000,
                    }
                ]
            },
            "POST",
        ),
        (
            "http://lean.example/exec/cleanup",
            {"item_ids": ["run_1:thm:attempt_1"]},
            "POST",
        ),
        (
            "http://lean.example/exec/cancel",
            {"item_ids": ["run_1:thm:attempt_1"]},
            "POST",
        ),
        (
            "http://lean.example/exec/limits",
            None,
            "GET",
        ),
    ]


@pytest.mark.asyncio
async def test_exec_env_step_node_wraps_one_item() -> None:
    client = RecordingAsyncKiminaClient(
        [
            {
                "items": [
                    {
                        "node_id": "attempt:n0",
                        "results": [
                            {
                                "tactic": "simp",
                                "status": "open",
                                "state_token": "st_child",
                                "goals": ["⊢ 0 + n = n"],
                            }
                        ],
                    }
                ]
            }
        ]
    )
    env = AsyncLeanExecEnv(client, timeout_ms=7000)

    result = await env.step_node("attempt:n0", "st_root", ["simp"])

    assert result.node_id == "attempt:n0"
    assert result.results[0].state_token == "st_child"
    assert client.calls[0][1] == {
        "items": [
            {
                "node_id": "attempt:n0",
                "state_token": "st_root",
                "tactics": ["simp"],
                "acquire_timeout_ms": 7000,
                "step_timeout_ms": 7000,
            }
        ]
    }


@pytest.mark.asyncio
async def test_exec_batcher_flushes_when_max_items_is_reached() -> None:
    env = FakeLeanExecEnv()
    batcher = AsyncLeanExecBatcher(
        env,
        max_items=2,
        max_wait_ms=1000,
        max_in_flight_batches=1,
    )

    first = asyncio.create_task(batcher.submit_step("n1", "st_a", ["simp"]))
    second = asyncio.create_task(batcher.submit_step("n2", "st_b", ["rfl"]))
    results = await asyncio.gather(first, second)

    assert [result.node_id for result in results] == ["n1", "n2"]
    assert [[item.node_id for item in batch] for batch in env.batches] == [
        ["n1", "n2"]
    ]


@pytest.mark.asyncio
async def test_exec_batcher_flushes_after_wait() -> None:
    env = FakeLeanExecEnv()
    batcher = AsyncLeanExecBatcher(
        env,
        max_items=10,
        max_wait_ms=1,
        max_in_flight_batches=1,
    )

    result = await batcher.submit_step("n1", "st_a", ["simp"])

    assert result.node_id == "n1"
    assert [[item.node_id for item in batch] for batch in env.batches] == [["n1"]]


@pytest.mark.asyncio
async def test_exec_batcher_close_flushes_queued_items() -> None:
    env = FakeLeanExecEnv()
    batcher = AsyncLeanExecBatcher(
        env,
        max_items=10,
        max_wait_ms=1000,
        max_in_flight_batches=1,
    )

    pending = asyncio.create_task(batcher.submit_step("n1", "st_a", ["simp"]))
    await asyncio.sleep(0)
    await batcher.close()

    assert (await pending).node_id == "n1"
    assert [[item.node_id for item in batch] for batch in env.batches] == [["n1"]]


@pytest.mark.asyncio
async def test_exec_batcher_tolerates_cancelled_submitter() -> None:
    env = BlockingFakeLeanExecEnv()
    batcher = AsyncLeanExecBatcher(
        env,
        max_items=1,
        max_wait_ms=1000,
        max_in_flight_batches=1,
    )

    pending = asyncio.create_task(batcher.submit_step("n1", "st_a", ["simp"]))
    await env.started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    env.release.set()
    await batcher.close()


@pytest.mark.asyncio
async def test_exec_batcher_can_be_configured_from_server_limits() -> None:
    env = FakeLeanExecEnv()
    batcher = await AsyncLeanExecBatcher.from_server_limits(env)

    assert batcher.max_items == 2
    assert batcher.max_wait_ms == 0


@pytest.mark.asyncio
async def test_exec_batcher_retries_observed_overloaded_items() -> None:
    env = OverloadedThenCompleteEnv()
    batcher = AsyncLeanExecBatcher(
        env,
        max_items=1,
        max_wait_ms=0,
        max_in_flight_batches=1,
        max_overloaded_retries=1,
        overload_backoff_seconds=0,
    )

    result = await batcher.submit_step("item:n0", "st_a", ["simp"])

    assert result.results[0].status == "complete"
    assert [[item.node_id for item in batch] for batch in env.batches] == [
        ["item:n0"],
        ["item:n0"],
    ]


@pytest.mark.asyncio
async def test_exec_batcher_serializes_same_item_id_requests() -> None:
    env = OverlapDetectingEnv()
    batcher = AsyncLeanExecBatcher(
        env,
        max_items=1,
        max_wait_ms=0,
        max_in_flight_batches=2,
    )

    await asyncio.gather(
        batcher.submit_step("item:n0", "st_a", ["simp"]),
        batcher.submit_step("item:n1", "st_b", ["rfl"]),
    )

    assert env.max_active == 1


@pytest.mark.asyncio
async def test_exec_env_resumable_step_reuses_completed_microbatches(
    tmp_path: Path,
) -> None:
    env = AsyncLeanExecEnv(RecordingAsyncKiminaClient([]))
    fake_env = FakeLeanExecEnv()
    journal = ExecMicrobatchJournal(tmp_path / "journal.json")
    first_items = [
        ExecStepBatchItem(node_id="item:n0", state_token="st_0", tactics=["simp"]),
        ExecStepBatchItem(node_id="item:n1", state_token="st_1", tactics=["simp"]),
    ]
    first_response = ExecStepBatchResponse(
        items=[
            ExecStepBatchResult(
                node_id=item.node_id,
                results=[ExecStepResult(tactic="simp", status="complete")],
            )
            for item in first_items
        ]
    )
    journal.put(
        env_call_id="call",
        microbatch_id=0,
        status="complete",
        request_payload=ExecStepBatchRequest(items=first_items).model_dump(),
        response_payload=first_response.model_dump(),
    )

    env.step_batch = fake_env.step_batch  # type: ignore[method-assign]
    response = await env.step_batch_resumable(
        env_call_id="call",
        items=[
            *first_items,
            ExecStepBatchItem(node_id="item:n2", state_token="st_2", tactics=["simp"]),
            ExecStepBatchItem(node_id="item:n3", state_token="st_3", tactics=["simp"]),
        ],
        journal=journal,
        microbatch_size=2,
    )

    assert [item.node_id for item in response.items] == [
        "item:n0",
        "item:n1",
        "item:n2",
        "item:n3",
    ]
    assert [[item.node_id for item in batch] for batch in fake_env.batches] == [
        ["item:n2", "item:n3"]
    ]


@pytest.mark.asyncio
async def test_exec_env_resumable_step_does_not_replay_uncertain_microbatch(
    tmp_path: Path,
) -> None:
    env = AsyncLeanExecEnv(RecordingAsyncKiminaClient([]))
    journal = ExecMicrobatchJournal(tmp_path / "journal.json")
    items = [
        ExecStepBatchItem(node_id="item:n0", state_token="st_0", tactics=["simp"])
    ]
    journal.put(
        env_call_id="call",
        microbatch_id=0,
        status="running",
        request_payload=ExecStepBatchRequest(items=items).model_dump(),
    )

    with pytest.raises(UncertainMicrobatchError):
        await env.step_batch_resumable(
            env_call_id="call",
            items=items,
            journal=journal,
            microbatch_size=1,
        )
