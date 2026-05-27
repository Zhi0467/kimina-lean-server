"""Throughput/latency and memory/state-store metrics for the benchmark."""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import psutil


@dataclass
class RequestRecord:
    endpoint: str
    elapsed_ms: float


@dataclass
class MetricsCollector:
    """Accumulates per-request latencies and per-tactic status counts."""

    requests: list[RequestRecord] = field(default_factory=list)
    status_counts: Counter[str] = field(default_factory=Counter)
    created_states: int = 0
    step_results: int = 0

    def record_request(self, endpoint: str, elapsed_ms: float) -> None:
        self.requests.append(RequestRecord(endpoint=endpoint, elapsed_ms=elapsed_ms))

    def record_status(self, status: str) -> None:
        self.status_counts[status] += 1


class RssSampler:
    """Background sampler of a process tree's RSS, in MB.

    Polls ``psutil`` for the target process plus all descendants every
    ``interval_seconds`` so the reported peak reflects load during the run, not
    just the endpoints. A no-op when ``pid`` is None.
    """

    def __init__(self, pid: int | None, *, interval_seconds: float = 0.5) -> None:
        self._pid = pid
        self._interval = interval_seconds
        self._samples_mb: list[float] = []
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "RssSampler":
        if self._pid is not None:
            self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        assert self._pid is not None
        try:
            process = psutil.Process(self._pid)
        except psutil.Error:
            return
        while True:
            sample = _process_tree_rss_mb(process)
            if sample is not None:
                self._samples_mb.append(sample)
            await asyncio.sleep(self._interval)

    def summary(self) -> dict[str, float] | None:
        if not self._samples_mb:
            return None
        return {
            "peak_mb": round(max(self._samples_mb), 1),
            "mean_mb": round(sum(self._samples_mb) / len(self._samples_mb), 1),
            "final_mb": round(self._samples_mb[-1], 1),
            "samples": len(self._samples_mb),
        }


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (pct in [0, 100]); 0.0 for empty input."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


def state_store_usage(state_store_dir: Path, item_id_prefix: str | None = None) -> dict[str, int]:
    """Count state files and bytes, optionally scoped to a run's item prefix.

    Scoping reads the ``{token}.json`` sidecars (which carry ``item_id``) so a
    shared store is not double-counted across runs.
    """
    if not state_store_dir.is_dir():
        return {"state_count": 0, "total_bytes": 0}

    state_count = 0
    total_bytes = 0
    for bin_path in state_store_dir.glob("*.bin"):
        if item_id_prefix is not None and not _sidecar_matches(bin_path, item_id_prefix):
            continue
        try:
            total_bytes += bin_path.stat().st_size
            state_count += 1
        except FileNotFoundError:
            continue
    return {"state_count": state_count, "total_bytes": total_bytes}


def build_report(
    collector: MetricsCollector,
    *,
    wall_seconds: float,
    cleanup_deleted_states: int,
    cleanup_deleted_bytes: int,
    rss: dict[str, float] | None,
    state_store_before: dict[str, int],
    state_store_after: dict[str, int],
) -> dict[str, object]:
    latencies = [record.elapsed_ms for record in collector.requests]
    request_count = len(latencies)
    return {
        "wall_seconds": round(wall_seconds, 3),
        "request_count": request_count,
        "created_states": collector.created_states,
        "step_results": collector.step_results,
        "items_per_sec": round(collector.created_states / wall_seconds, 2)
        if wall_seconds > 0
        else 0.0,
        "tactics_per_sec": round(collector.step_results / wall_seconds, 2)
        if wall_seconds > 0
        else 0.0,
        "latency_ms": {
            "p50": round(percentile(latencies, 50), 1),
            "p95": round(percentile(latencies, 95), 1),
            "p99": round(percentile(latencies, 99), 1),
            "max": round(max(latencies), 1) if latencies else 0.0,
        },
        "status_counts": dict(collector.status_counts),
        "cleanup": {
            "deleted_states": cleanup_deleted_states,
            "deleted_bytes": cleanup_deleted_bytes,
        },
        "memory": {
            "process_tree_rss": rss,
            "system_memory_percent": psutil.virtual_memory().percent,
        },
        "state_store": {
            "before": state_store_before,
            "after": state_store_after,
        },
    }


def _process_tree_rss_mb(process: psutil.Process) -> float | None:
    try:
        total = process.memory_info().rss
        for child in process.children(recursive=True):
            try:
                total += child.memory_info().rss
            except psutil.Error:
                continue
        return total / (1024 * 1024)
    except psutil.Error:
        return None


def _sidecar_matches(bin_path: Path, item_id_prefix: str) -> bool:
    sidecar = bin_path.with_suffix(".json")
    if not sidecar.is_file():
        return False
    try:
        import json

        return json.loads(sidecar.read_text()).get("item_id", "").startswith(item_id_prefix)
    except (OSError, ValueError):
        return False


def now_ms() -> float:
    return time.perf_counter() * 1000
