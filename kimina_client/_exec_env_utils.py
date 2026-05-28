from __future__ import annotations

from .exec_models import ExecStepBatchResponse, ExecStepBatchResult


def single_step_result(
    response: ExecStepBatchResponse,
    node_id: str,
) -> ExecStepBatchResult:
    for item in response.items:
        if item.node_id == node_id:
            return item
    raise RuntimeError(f"backend response missing node_id {node_id!r}")
