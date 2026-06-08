from __future__ import annotations

from .exec_models import ExecLimitsResponse, ExecStepBatchResponse, ExecStepBatchResult


def single_step_result(
    response: ExecStepBatchResponse,
    node_id: str,
) -> ExecStepBatchResult:
    for item in response.items:
        if item.node_id == node_id:
            return item
    raise RuntimeError(f"backend response missing node_id {node_id!r}")


def validate_finite_exec_limits(limits: ExecLimitsResponse) -> None:
    unbounded = [
        name
        for name, value in (
            ("max_in_flight_exec_requests", limits.max_in_flight_exec_requests),
            ("max_queued_exec_requests", limits.max_queued_exec_requests),
            ("max_state_store_bytes", limits.max_state_store_bytes),
        )
        if value == -1
    ]
    if not unbounded:
        return
    joined = ", ".join(f"{name}=-1" for name in unbounded)
    raise RuntimeError(
        "/exec/limits advertises unbounded safety caps; refusing to size "
        f"AsyncLeanExecBatcher from unsafe server: {joined}"
    )
