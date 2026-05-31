from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
import kimina_client.exec_server as exec_server_module

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
    ExecRequestOverloadedError,
    ExecServerConfig,
    ExecStepBatchItem,
    ExecStepBatchRequest,
    ExecStepBatchResponse,
    ExecStepBatchResult,
    ExecStepResult,
    ExecStatsResponse,
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
        self.acquire_timeout_ms = 5000
        self.step_timeout_ms = 5000
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
            max_state_store_bytes=1024,
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


class RequestOverloadedThenCompleteEnv(FakeLeanExecEnv):
    async def step_batch(
        self,
        items: list[ExecStepBatchItem],
    ) -> ExecStepBatchResponse:
        if not self.batches:
            self.batches.append(items)
            raise ExecRequestOverloadedError("http://lean.example/exec/step_batch", 503)
        return await FakeLeanExecEnv.step_batch(self, items)


class UnboundedLimitsEnv(FakeLeanExecEnv):
    async def limits(self) -> ExecLimitsResponse:
        limits = await super().limits()
        return limits.model_copy(update={"max_queued_exec_requests": -1})


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
                "max_state_store_bytes": 1024,
                "max_acquire_timeout_ms": 600000,
                "max_step_timeout_ms": 600000,
                "recommended_items_per_step_batch": 2,
                "recommended_in_flight_step_batches": 2,
            },
            {
                "state_store": {
                    "state_count": 0,
                    "total_bytes": 0,
                    "item_count": 0,
                    "pinned_states": 0,
                    "pin_refs": 0,
                },
                "worker_pool": {
                    "max_workers": 4,
                    "max_workers_per_env_profile": 4,
                    "worker_startup_timeout_seconds": 600,
                    "lease_requests": 0,
                    "lease_timeouts": 0,
                    "lease_wait_ms_total": 0.0,
                    "lease_wait_ms_max": 0.0,
                    "free_workers": 0,
                    "busy_workers": 0,
                    "starting_workers": 0,
                    "total_workers": 0,
                    "workers_by_env_profile": {},
                    "workers": [],
                },
                "lifecycle": {
                    "total_items": 0,
                    "active_items": 0,
                    "cancelling_items": 0,
                    "drained_items": 0,
                    "cleaned_items": 0,
                    "in_flight_items": 0,
                    "total_in_flight": 0,
                },
                "request_limiter": {
                    "max_in_flight": 4,
                    "max_queued": 4,
                    "in_flight": 0,
                    "queued": 0,
                },
                "metrics": {
                    "endpoint_requests": {"stats": 1},
                    "rejected_requests": {},
                    "exec_status_counts": {},
                    "cleanup_status_counts": {},
                    "cancel_status_counts": {},
                },
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
    stats = await client.exec_stats()

    assert isinstance(create, ExecCreateStatesResponse)
    assert create.items[0].states[0].state_token == "st_root"
    assert step.items[0].results[0].status == "complete"
    assert isinstance(cleanup, ExecCleanupResponse)
    assert cleanup.deleted_items[0].deleted_states == 1
    assert isinstance(cancel, ExecCancelResponse)
    assert cancel.items[0].status == "drained"
    assert limits.recommended_items_per_step_batch == 2
    assert isinstance(stats, ExecStatsResponse)
    assert stats.request_limiter.max_in_flight == 4
    assert stats.worker_pool.lease_requests == 0
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
        (
            "http://lean.example/exec/stats",
            None,
            "GET",
        ),
    ]


@pytest.mark.asyncio
async def test_async_client_maps_exec_503_to_request_overloaded() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            503,
            json={"detail": "exec request queue is full"},
        )
    )
    client = AsyncKiminaClient(api_url="http://lean.example", n_retries=1)
    await client.session.aclose()
    client.session = httpx.AsyncClient(transport=transport, headers=client.headers)

    with pytest.raises(ExecRequestOverloadedError) as exc_info:
        await client.exec_step_batch(
            [
                ExecStepBatchItem(
                    node_id="item:n0",
                    state_token="st_root",
                    tactics=["simp"],
                )
            ]
        )

    assert exc_info.value.status_code == 503
    await client.aclose()


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
async def test_exec_batcher_refuses_unbounded_server_limits() -> None:
    env = UnboundedLimitsEnv()

    with pytest.raises(RuntimeError, match="max_queued_exec_requests=-1"):
        await AsyncLeanExecBatcher.from_server_limits(env)


def test_exec_server_config_builds_cli_args() -> None:
    cfg = ExecServerConfig(
        host="127.0.0.1",
        port=8765,
        workers=3,
        state_store_dir="/tmp/lean-state",
    )

    args = cfg.to_cli_args()

    assert args[:6] == [
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
        "--workers",
        "3",
    ]
    assert ["--max-lean-processes-per-env-profile", "3"] == [
        args[6],
        args[7],
    ]
    assert "--state-store-dir" in args
    assert "/tmp/lean-state" in args
    assert "--single-process" in args


def test_exec_server_config_rejects_unbounded_by_default() -> None:
    with pytest.raises(ValueError, match="allow_unbounded_exec=True"):
        ExecServerConfig(max_in_flight_exec_requests=-1)


def test_launch_server_invokes_server_module(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    class FakePopen:
        def __init__(
            self,
            command: list[str],
            *,
            start_new_session: bool,
        ) -> None:
            calls["command"] = command
            calls["start_new_session"] = start_new_session

    monkeypatch.setattr(exec_server_module.subprocess, "Popen", FakePopen)

    process = exec_server_module.launch_server(
        ExecServerConfig(workers=2),
        server_python="/server/.venv/bin/python",
    )

    assert isinstance(process, FakePopen)
    assert calls["command"][:3] == ["/server/.venv/bin/python", "-m", "server"]
    assert "--workers" in calls["command"]
    assert calls["start_new_session"] is True


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
async def test_exec_batcher_retries_request_level_overload() -> None:
    env = RequestOverloadedThenCompleteEnv()
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
async def test_exec_batcher_preserves_split_default_timeouts() -> None:
    env = FakeLeanExecEnv()
    env.acquire_timeout_ms = 1234
    env.step_timeout_ms = 5678
    batcher = AsyncLeanExecBatcher(
        env,
        max_items=1,
        max_wait_ms=0,
        max_in_flight_batches=1,
    )

    await batcher.submit_step("item:n0", "st_a", ["simp"])

    assert env.batches[0][0].acquire_timeout_ms == 1234
    assert env.batches[0][0].step_timeout_ms == 5678


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
