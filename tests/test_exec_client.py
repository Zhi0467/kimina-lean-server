from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kimina_client import (
    AsyncKiminaClient,
    AsyncLeanExecBatcher,
    AsyncLeanExecEnv,
    ExecCleanupResponse,
    ExecCreateStateItem,
    ExecCreateStatesResponse,
    ExecStepBatchItem,
    ExecStepBatchResponse,
    ExecStepBatchResult,
    ExecStepResult,
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
                        "deleted_states": 1,
                        "deleted_bytes": 7,
                    }
                ]
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

    assert isinstance(create, ExecCreateStatesResponse)
    assert create.items[0].states[0].state_token == "st_root"
    assert step.items[0].results[0].status == "complete"
    assert isinstance(cleanup, ExecCleanupResponse)
    assert cleanup.deleted_items[0].deleted_states == 1
    assert client.calls == [
        (
            "http://lean.example/exec/create_states",
            {
                "env_profile": "lean_init",
                "items": [
                    {
                        "item_id": "run_1:thm:attempt_1",
                        "code": "theorem t : True := by\n  sorry",
                        "timeout_ms": 1000,
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
                        "timeout_ms": 1000,
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
                "timeout_ms": 7000,
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
