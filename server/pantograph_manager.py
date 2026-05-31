from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import time
from typing import Any

from loguru import logger

from .pantograph_worker import PantographWorker


class NoAvailablePantographWorkerError(Exception):
    """Raised when the Pantograph worker pool cannot lease a worker in time."""


WorkerFactory = Callable[
    [list[str], str | None, int, int | None],
    Awaitable[Any],
]


@dataclass(eq=False)
class PantographWorkerLease:
    worker: Any
    env_profile: str
    header: str
    header_hash: str
    last_used_at: datetime
    use_count: int = 0


@dataclass(frozen=True)
class PantographWorkerStats:
    env_profile: str
    header_hash: str
    status: str
    use_count: int
    pid: int | None
    rss_bytes: int | None


@dataclass(frozen=True)
class PantographManagerStats:
    max_workers: int
    max_workers_per_env_profile: int
    worker_startup_timeout_seconds: int
    lease_requests: int
    lease_timeouts: int
    lease_wait_ms_total: float
    lease_wait_ms_max: float
    free_workers: int
    busy_workers: int
    starting_workers: int
    total_workers: int
    workers_by_env_profile: dict[str, int]
    workers: list[PantographWorkerStats]


class PantographManager:
    def __init__(
        self,
        *,
        max_workers: int,
        project_path: Path | None = None,
        buffer_limit: int | None = 1_000_000,
        max_worker_uses: int = -1,
        max_workers_per_env_profile: int = -1,
        worker_startup_timeout_seconds: int = 600,
        worker_factory: WorkerFactory | None = None,
    ) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if max_worker_uses < -1:
            raise ValueError("max_worker_uses must be -1 or non-negative")
        if max_workers_per_env_profile == 0 or max_workers_per_env_profile < -1:
            raise ValueError("max_workers_per_env_profile must be -1 or positive")
        if worker_startup_timeout_seconds <= 0:
            raise ValueError("worker_startup_timeout_seconds must be positive")
        self.max_workers = max_workers
        self.project_path = project_path
        self.buffer_limit = buffer_limit
        self.max_worker_uses = max_worker_uses
        self.max_workers_per_env_profile = max_workers_per_env_profile
        self.worker_startup_timeout_seconds = worker_startup_timeout_seconds
        self._worker_factory = worker_factory or _create_pantograph_worker

        self._lock: asyncio.Lock | None = None
        self._cond: asyncio.Condition | None = None
        self._free: list[PantographWorkerLease] = []
        self._busy: set[PantographWorkerLease] = set()
        self._starting = 0
        self._starting_by_env_profile: dict[str, int] = {}
        self._lease_requests = 0
        self._lease_timeouts = 0
        self._lease_wait_ms_total = 0.0
        self._lease_wait_ms_max = 0.0

    async def get_worker(
        self,
        *,
        env_profile: str,
        header: str,
        timeout: float,
    ) -> PantographWorkerLease:
        self._ensure_lock()
        assert self._cond is not None
        header_hash_value = header_hash(header)
        started_at = time()
        self._lease_requests += 1
        deadline = time() + timeout
        worker_to_close: PantographWorkerLease | None = None

        while True:
            async with self._cond:
                for i, lease in enumerate(self._free):
                    if (
                        lease.env_profile == env_profile
                        and lease.header_hash == header_hash_value
                    ):
                        self._free.pop(i)
                        self._busy.add(lease)
                        self._record_lease_wait(started_at)
                        return lease

                total = len(self._free) + len(self._busy) + self._starting
                can_start_for_profile = self._can_start_for_env_profile(env_profile)
                if total < self.max_workers and can_start_for_profile:
                    self._mark_starting(env_profile)
                    break

                worker_to_close = self._worker_to_close_for_start(
                    env_profile,
                    can_start_for_profile=can_start_for_profile,
                )
                if worker_to_close is not None:
                    self._free.remove(worker_to_close)
                    self._mark_starting(env_profile)
                    break

                remaining = deadline - time()
                if remaining <= 0:
                    self._record_lease_wait(started_at, timed_out=True)
                    raise NoAvailablePantographWorkerError(
                        f"Timed out after {timeout}s"
                    )
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=remaining)
                except TimeoutError:
                    self._record_lease_wait(started_at, timed_out=True)
                    raise NoAvailablePantographWorkerError(
                        f"Timed out after {timeout}s while waiting"
                    ) from None

        if worker_to_close is not None:
            await _close_worker(worker_to_close.worker)

        try:
            worker = await self._worker_factory(
                imports_from_header(header),
                self._project_path_for_worker(),
                max(math.ceil(timeout), self.worker_startup_timeout_seconds, 1),
                self.buffer_limit,
            )
            lease = PantographWorkerLease(
                worker=worker,
                env_profile=env_profile,
                header=header,
                header_hash=header_hash_value,
                last_used_at=datetime.now(),
            )
        except Exception:
            async with self._cond:
                self._unmark_starting(env_profile)
                self._cond.notify(1)
            self._record_lease_wait(started_at)
            raise

        async with self._cond:
            self._unmark_starting(env_profile)
            self._busy.add(lease)
            self._cond.notify(1)
        self._record_lease_wait(started_at)
        return lease

    async def release_worker(self, lease: PantographWorkerLease) -> None:
        self._ensure_lock()
        assert self._cond is not None
        should_close = False
        async with self._cond:
            if lease not in self._busy:
                logger.error("Attempted to release a Pantograph worker that is not busy")
                return
            self._busy.remove(lease)
            lease.use_count += 1
            if self._is_exhausted(lease):
                should_close = True
            else:
                lease.last_used_at = datetime.now()
                self._free.append(lease)
            self._cond.notify(1)

        if should_close:
            logger.info("Pantograph worker exhausted; closing instead of recycling")
            await _close_worker(lease.worker)

    def _is_exhausted(self, lease: PantographWorkerLease) -> bool:
        if self.max_worker_uses < 0:
            return False
        return lease.use_count >= self.max_worker_uses

    def _can_start_for_env_profile(self, env_profile: str) -> bool:
        if self.max_workers_per_env_profile < 0:
            return True
        active = sum(
            1
            for lease in [*self._free, *self._busy]
            if lease.env_profile == env_profile
        )
        active += self._starting_by_env_profile.get(env_profile, 0)
        return active < self.max_workers_per_env_profile

    def _worker_to_close_for_start(
        self,
        env_profile: str,
        *,
        can_start_for_profile: bool,
    ) -> PantographWorkerLease | None:
        if not self._free:
            return None
        if can_start_for_profile:
            return min(self._free, key=lambda worker: worker.last_used_at)
        same_profile = [
            lease for lease in self._free if lease.env_profile == env_profile
        ]
        if not same_profile:
            return None
        return min(same_profile, key=lambda worker: worker.last_used_at)

    def _mark_starting(self, env_profile: str) -> None:
        self._starting += 1
        self._starting_by_env_profile[env_profile] = (
            self._starting_by_env_profile.get(env_profile, 0) + 1
        )

    def _unmark_starting(self, env_profile: str) -> None:
        self._starting -= 1
        count = self._starting_by_env_profile.get(env_profile, 0)
        if count <= 1:
            self._starting_by_env_profile.pop(env_profile, None)
        else:
            self._starting_by_env_profile[env_profile] = count - 1

    def _record_lease_wait(self, started_at: float, *, timed_out: bool = False) -> None:
        wait_ms = max((time() - started_at) * 1000, 0.0)
        self._lease_wait_ms_total += wait_ms
        self._lease_wait_ms_max = max(self._lease_wait_ms_max, wait_ms)
        if timed_out:
            self._lease_timeouts += 1

    async def destroy_worker(self, lease: PantographWorkerLease) -> None:
        self._ensure_lock()
        assert self._cond is not None
        should_close = False
        async with self._cond:
            should_close = lease in self._busy or lease in self._free
            self._busy.discard(lease)
            if lease in self._free:
                self._free.remove(lease)
            self._cond.notify(1)
        if should_close:
            await _close_worker(lease.worker)

    async def cleanup(self) -> None:
        self._ensure_lock()
        assert self._cond is not None
        async with self._cond:
            workers = [lease.worker for lease in [*self._free, *self._busy]]
            self._free.clear()
            self._busy.clear()
            self._cond.notify_all()
        await asyncio.gather(*(_close_worker(worker) for worker in workers))

    async def stats(self) -> PantographManagerStats:
        self._ensure_lock()
        assert self._cond is not None
        async with self._cond:
            free = list(self._free)
            busy = list(self._busy)
            starting_by_env = dict(self._starting_by_env_profile)
            lease_requests = self._lease_requests
            lease_timeouts = self._lease_timeouts
            lease_wait_ms_total = self._lease_wait_ms_total
            lease_wait_ms_max = self._lease_wait_ms_max
            worker_stats = [
                self._lease_stats(lease, status="free") for lease in free
            ] + [self._lease_stats(lease, status="busy") for lease in busy]

        by_env: dict[str, int] = {}
        for lease in [*free, *busy]:
            by_env[lease.env_profile] = by_env.get(lease.env_profile, 0) + 1
        for env_profile, count in starting_by_env.items():
            by_env[env_profile] = by_env.get(env_profile, 0) + count

        return PantographManagerStats(
            max_workers=self.max_workers,
            max_workers_per_env_profile=self.max_workers_per_env_profile,
            worker_startup_timeout_seconds=self.worker_startup_timeout_seconds,
            lease_requests=lease_requests,
            lease_timeouts=lease_timeouts,
            lease_wait_ms_total=lease_wait_ms_total,
            lease_wait_ms_max=lease_wait_ms_max,
            free_workers=len(free),
            busy_workers=len(busy),
            starting_workers=sum(starting_by_env.values()),
            total_workers=len(free) + len(busy) + sum(starting_by_env.values()),
            workers_by_env_profile=by_env,
            workers=worker_stats,
        )

    @staticmethod
    def _lease_stats(
        lease: PantographWorkerLease,
        *,
        status: str,
    ) -> PantographWorkerStats:
        pid_value = getattr(lease.worker, "pid", None)
        pid = pid_value if isinstance(pid_value, int) else None
        rss_getter = getattr(lease.worker, "process_tree_rss_bytes", None)
        rss_value = rss_getter() if callable(rss_getter) else None
        rss_bytes = rss_value if isinstance(rss_value, int) else None
        return PantographWorkerStats(
            env_profile=lease.env_profile,
            header_hash=lease.header_hash,
            status=status,
            use_count=lease.use_count,
            pid=pid,
            rss_bytes=rss_bytes,
        )

    def _ensure_lock(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
            self._cond = asyncio.Condition(self._lock)

    def _project_path_for_worker(self) -> str | None:
        if self.project_path is None or not self.project_path.exists():
            return None
        return str(self.project_path)


def header_hash(header: str) -> str:
    return hashlib.sha256(header.encode("utf-8")).hexdigest()


def imports_from_header(header: str) -> list[str]:
    imports: list[str] = []
    for line in header.splitlines():
        stripped = line.strip()
        if stripped.startswith("import "):
            imports.extend(stripped.removeprefix("import ").split())
    return imports or ["Init"]


async def _create_pantograph_worker(
    imports: list[str],
    project_path: str | None,
    timeout_seconds: int,
    buffer_limit: int | None,
) -> PantographWorker:
    return await PantographWorker.create(
        imports=imports,
        project_path=project_path,
        timeout_seconds=timeout_seconds,
        buffer_limit=buffer_limit,
    )


async def _close_worker(worker: Any) -> None:
    aclose = getattr(worker, "aclose", None)
    if aclose is not None:
        await aclose()
        return
    close = getattr(worker, "close", None)
    if close is not None:
        close()
