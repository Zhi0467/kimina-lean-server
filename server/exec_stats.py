from __future__ import annotations

from .exec_lifecycle import ItemLifecycleRegistry
from .exec_metrics import ExecMetrics
from .exec_request_limiter import ExecRequestLimiter
from .pantograph_manager import PantographManager
from .schemas_exec import (
    ExecLifecycleStats,
    ExecObservedMetrics,
    ExecRequestLimiterStats,
    ExecStateStoreStats,
    ExecStatsResponse,
    ExecWorkerPoolStats,
    ExecWorkerStats,
)
from .state_store import StateStore


async def collect_exec_stats(
    *,
    state_store: StateStore,
    pantograph_manager: PantographManager,
    lifecycle: ItemLifecycleRegistry,
    limiter: ExecRequestLimiter,
    metrics: ExecMetrics,
) -> ExecStatsResponse:
    state_store_stats = state_store.stats()
    lifecycle_stats = lifecycle.stats()
    limiter_stats = limiter.stats()
    manager_stats = await pantograph_manager.stats()
    metrics_stats = metrics.stats()

    return ExecStatsResponse(
        state_store=ExecStateStoreStats(
            state_count=state_store_stats.state_count,
            total_bytes=state_store_stats.total_bytes,
            item_count=state_store_stats.item_count,
            pinned_states=state_store_stats.pinned_states,
            pin_refs=state_store_stats.pin_refs,
        ),
        worker_pool=ExecWorkerPoolStats(
            max_workers=manager_stats.max_workers,
            max_workers_per_env_profile=manager_stats.max_workers_per_env_profile,
            worker_startup_timeout_seconds=(
                manager_stats.worker_startup_timeout_seconds
            ),
            lease_requests=manager_stats.lease_requests,
            lease_timeouts=manager_stats.lease_timeouts,
            lease_wait_ms_total=manager_stats.lease_wait_ms_total,
            lease_wait_ms_max=manager_stats.lease_wait_ms_max,
            free_workers=manager_stats.free_workers,
            busy_workers=manager_stats.busy_workers,
            starting_workers=manager_stats.starting_workers,
            total_workers=manager_stats.total_workers,
            workers_by_env_profile=manager_stats.workers_by_env_profile,
            workers=[
                ExecWorkerStats(
                    env_profile=worker.env_profile,
                    header_hash=worker.header_hash,
                    status=worker.status,
                    use_count=worker.use_count,
                    pid=worker.pid,
                    rss_bytes=worker.rss_bytes,
                )
                for worker in manager_stats.workers
            ],
        ),
        lifecycle=ExecLifecycleStats(
            total_items=lifecycle_stats.total_items,
            active_items=lifecycle_stats.active_items,
            cancelling_items=lifecycle_stats.cancelling_items,
            drained_items=lifecycle_stats.drained_items,
            cleaned_items=lifecycle_stats.cleaned_items,
            in_flight_items=lifecycle_stats.in_flight_items,
            total_in_flight=lifecycle_stats.total_in_flight,
        ),
        request_limiter=ExecRequestLimiterStats(
            max_in_flight=limiter_stats.max_in_flight,
            max_queued=limiter_stats.max_queued,
            in_flight=limiter_stats.in_flight,
            queued=limiter_stats.queued,
        ),
        metrics=ExecObservedMetrics(
            endpoint_requests=metrics_stats.endpoint_requests,
            rejected_requests=metrics_stats.rejected_requests,
            exec_status_counts=metrics_stats.exec_status_counts,
            cleanup_status_counts=metrics_stats.cleanup_status_counts,
            cancel_status_counts=metrics_stats.cancel_status_counts,
        ),
    )
