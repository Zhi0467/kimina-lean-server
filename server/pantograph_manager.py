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


class PantographManager:
    def __init__(
        self,
        *,
        max_workers: int,
        project_path: Path | None = None,
        buffer_limit: int | None = 1_000_000,
        max_worker_uses: int = -1,
        worker_factory: WorkerFactory | None = None,
    ) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if max_worker_uses < -1:
            raise ValueError("max_worker_uses must be -1 or non-negative")
        self.max_workers = max_workers
        self.project_path = project_path
        self.buffer_limit = buffer_limit
        self.max_worker_uses = max_worker_uses
        self._worker_factory = worker_factory or _create_pantograph_worker

        self._lock: asyncio.Lock | None = None
        self._cond: asyncio.Condition | None = None
        self._free: list[PantographWorkerLease] = []
        self._busy: set[PantographWorkerLease] = set()
        self._starting = 0

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
                        return lease

                total = len(self._free) + len(self._busy) + self._starting
                if total < self.max_workers:
                    self._starting += 1
                    break

                if self._free:
                    worker_to_close = min(
                        self._free,
                        key=lambda worker: worker.last_used_at,
                    )
                    self._free.remove(worker_to_close)
                    self._starting += 1
                    break

                remaining = deadline - time()
                if remaining <= 0:
                    raise NoAvailablePantographWorkerError(
                        f"Timed out after {timeout}s"
                    )
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=remaining)
                except TimeoutError:
                    raise NoAvailablePantographWorkerError(
                        f"Timed out after {timeout}s while waiting"
                    ) from None

        if worker_to_close is not None:
            await _close_worker(worker_to_close.worker)

        try:
            worker = await self._worker_factory(
                imports_from_header(header),
                self._project_path_for_worker(),
                max(math.ceil(timeout), 1),
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
                self._starting -= 1
                self._cond.notify(1)
            raise

        async with self._cond:
            self._starting -= 1
            self._busy.add(lease)
            self._cond.notify(1)
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
