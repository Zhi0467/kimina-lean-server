from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from server.schemas_exec import (
    CancelRequest,
    CancelResponse,
    CancelResult,
    CleanupRequest,
    CleanupResponse,
    CleanupResult,
    CreateStatesItem,
    CreateStatesRequest,
    CreateStatesResponse,
    CreateStatesResult,
    ExecLimitsResponse,
    StateInfo,
    StepBatchItem,
    StepBatchRequest,
    StepBatchResponse,
    StepBatchResult,
    StepResult,
)


def test_create_states_request_validates_unique_item_ids() -> None:
    with pytest.raises(ValidationError):
        CreateStatesRequest(
            env_profile="lean4.29.1_mathlib_x",
            items=[
                {"item_id": "theorem_42:a0", "code": "theorem t : True := by sorry"},
                {"item_id": "theorem_42:a0", "code": "theorem t : True := by sorry"},
            ],
        )


def test_create_states_request_rejects_empty_items() -> None:
    with pytest.raises(ValidationError):
        CreateStatesRequest(env_profile="lean4.29.1_mathlib_x", items=[])


def test_step_batch_request_validates_items() -> None:
    with pytest.raises(ValidationError):
        StepBatchRequest(items=[])

    with pytest.raises(ValidationError):
        StepBatchRequest(
            items=[
                {"node_id": "n0", "state_token": "st_parent", "tactics": ["simp"]},
                {"node_id": "n0", "state_token": "st_parent", "tactics": ["omega"]},
            ]
        )

    with pytest.raises(ValidationError):
        StepBatchRequest(
            items=[{"node_id": "n0", "state_token": "st_parent", "tactics": []}]
        )


def test_timeout_ms_alias_populates_split_timeouts() -> None:
    item = StepBatchItem(
        node_id="n0",
        state_token="st_parent",
        tactics=["simp"],
        timeout_ms=1234,
    )

    assert item.acquire_timeout_ms == 1234
    assert item.step_timeout_ms == 1234
    assert "timeout_ms" not in item.model_dump()

    create_item = CreateStatesItem(
        item_id="theorem_42:a0",
        code="theorem t : True := by\n  sorry",
        timeout_ms=2345,
        acquire_timeout_ms=3456,
    )

    assert create_item.acquire_timeout_ms == 3456
    assert create_item.step_timeout_ms == 2345
    assert "timeout_ms" not in create_item.model_dump()


def test_cleanup_request_validates_item_ids() -> None:
    with pytest.raises(ValidationError):
        CleanupRequest(item_ids=[])

    with pytest.raises(ValidationError):
        CleanupRequest(item_ids=["theorem_42:a0", "theorem_42:a0"])


def test_cancel_request_validates_item_ids() -> None:
    with pytest.raises(ValidationError):
        CancelRequest(item_ids=[])

    with pytest.raises(ValidationError):
        CancelRequest(item_ids=["theorem_42:a0", "theorem_42:a0"])


def test_response_models_capture_stable_contract() -> None:
    create_response = CreateStatesResponse(
        items=[
            CreateStatesResult(
                item_id="theorem_42:a0",
                status="open",
                states=[
                    StateInfo(
                        state_token="st_root",
                        goals=["n : Nat\n⊢ n + 0 = n"],
                    )
                ],
            )
        ]
    )
    assert create_response.items[0].states[0].state_token == "st_root"

    step_response = StepBatchResponse(
        items=[
            StepBatchResult(
                node_id="theorem_42:a0:n0",
                results=[
                    StepResult(tactic="simp", status="complete"),
                    StepResult(tactic="busy", status="overloaded"),
                    StepResult(tactic="skip", status="cancelled"),
                    StepResult(
                        tactic="rw [Nat.add_comm]",
                        status="open",
                        state_token="st_child",
                        goals=["n : Nat\n⊢ 0 + n = n"],
                    ),
                ],
            )
        ]
    )
    assert step_response.items[0].results[3].state_token == "st_child"

    cancel_response = CancelResponse(
        items=[CancelResult(item_id="theorem_42:a0", status="drained")]
    )
    assert cancel_response.items[0].status == "drained"

    limits_response = ExecLimitsResponse(
        max_items_per_step_batch=1024,
        max_tactics_per_step_item=64,
        max_attempts_per_step_batch=8192,
        max_create_items_per_request=1024,
        max_pantograph_workers=4,
        max_lean_processes_per_env_profile=4,
        max_in_flight_exec_requests=-1,
        max_queued_exec_requests=-1,
        max_acquire_timeout_ms=600_000,
        max_step_timeout_ms=600_000,
        recommended_items_per_step_batch=16,
        recommended_in_flight_step_batches=4,
    )
    assert not limits_response.same_item_id_pipelining


def _schema_test_app() -> FastAPI:
    app = FastAPI()

    @app.post("/exec/create_states", response_model=CreateStatesResponse)
    async def create_states(request: CreateStatesRequest) -> CreateStatesResponse:
        return CreateStatesResponse(
            items=[
                CreateStatesResult(
                    item_id=item.item_id,
                    status="open",
                    states=[StateInfo(state_token=f"st_{item.item_id}", goals=["⊢ P"])],
                    messages=[],
                )
                for item in request.items
            ]
        )

    @app.post("/exec/step_batch", response_model=StepBatchResponse)
    async def step_batch(request: StepBatchRequest) -> StepBatchResponse:
        return StepBatchResponse(
            items=[
                StepBatchResult(
                    node_id=item.node_id,
                    results=[
                        StepResult(tactic=tactic, status="error", messages=["stub"])
                        for tactic in item.tactics
                    ],
                )
                for item in request.items
            ]
        )

    @app.post("/exec/cleanup", response_model=CleanupResponse)
    async def cleanup(request: CleanupRequest) -> CleanupResponse:
        return CleanupResponse(
            deleted_items=[
                CleanupResult(
                    item_id=item_id,
                    status="deleted",
                    deleted_states=0,
                    deleted_bytes=0,
                )
                for item_id in request.item_ids
            ]
        )

    return app


def test_exec_schemas_round_trip_through_fastapi() -> None:
    client = TestClient(_schema_test_app())

    create_response = client.post(
        "/exec/create_states",
        json={
            "env_profile": "lean4.29.1_mathlib_x",
            "items": [
                {
                    "item_id": "theorem_42:a0",
                    "code": "theorem t : True := by\n  sorry",
                }
            ],
        },
    )
    assert create_response.status_code == 200
    assert create_response.json()["items"][0]["states"][0] == {
        "state_token": "st_theorem_42:a0",
        "goals": ["⊢ P"],
    }

    step_response = client.post(
        "/exec/step_batch",
        json={
            "items": [
                {
                    "node_id": "theorem_42:a0:n0",
                    "state_token": "st_theorem_42:a0",
                    "tactics": ["simp", "omega"],
                }
            ]
        },
    )
    assert step_response.status_code == 200
    assert [r["tactic"] for r in step_response.json()["items"][0]["results"]] == [
        "simp",
        "omega",
    ]

    cleanup_response = client.post(
        "/exec/cleanup",
        json={"item_ids": ["theorem_42:a0"]},
    )
    assert cleanup_response.status_code == 200
    assert cleanup_response.json()["deleted_items"] == [
        {
            "item_id": "theorem_42:a0",
            "status": "deleted",
            "reason": None,
            "in_flight": 0,
            "pinned_states": 0,
            "deleted_states": 0,
            "deleted_bytes": 0,
        }
    ]


def test_exec_schemas_reject_bad_requests_through_fastapi() -> None:
    client = TestClient(_schema_test_app())

    duplicate_create_response = client.post(
        "/exec/create_states",
        json={
            "env_profile": "lean4.29.1_mathlib_x",
            "items": [
                {"item_id": "dup", "code": "theorem t : True := by sorry"},
                {"item_id": "dup", "code": "theorem t : True := by sorry"},
            ],
        },
    )
    assert duplicate_create_response.status_code == 422

    empty_tactics_response = client.post(
        "/exec/step_batch",
        json={
            "items": [
                {"node_id": "n0", "state_token": "st_parent", "tactics": []}
            ]
        },
    )
    assert empty_tactics_response.status_code == 422
