from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecMetricsStats:
    endpoint_requests: dict[str, int]
    rejected_requests: dict[str, int]
    exec_status_counts: dict[str, int]
    cleanup_status_counts: dict[str, int]
    cancel_status_counts: dict[str, int]


class ExecMetrics:
    """Small in-process counters for the exec service control plane."""

    def __init__(self) -> None:
        self._endpoint_requests: Counter[str] = Counter()
        self._rejected_requests: Counter[str] = Counter()
        self._exec_status_counts: Counter[str] = Counter()
        self._cleanup_status_counts: Counter[str] = Counter()
        self._cancel_status_counts: Counter[str] = Counter()

    def record_endpoint(self, endpoint: str) -> None:
        self._endpoint_requests[endpoint] += 1

    def record_rejection(self, reason: str) -> None:
        self._rejected_requests[reason] += 1

    def record_exec_statuses(self, statuses: Iterable[str]) -> None:
        self._exec_status_counts.update(statuses)

    def record_cleanup_statuses(self, statuses: Iterable[str]) -> None:
        self._cleanup_status_counts.update(statuses)

    def record_cancel_statuses(self, statuses: Iterable[str]) -> None:
        self._cancel_status_counts.update(statuses)

    def stats(self) -> ExecMetricsStats:
        return ExecMetricsStats(
            endpoint_requests=dict(self._endpoint_requests),
            rejected_requests=dict(self._rejected_requests),
            exec_status_counts=dict(self._exec_status_counts),
            cleanup_status_counts=dict(self._cleanup_status_counts),
            cancel_status_counts=dict(self._cancel_status_counts),
        )
