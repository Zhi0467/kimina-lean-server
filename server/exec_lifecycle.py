from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

LifecycleStatus = Literal["active", "cancelling", "drained", "cleaned"]
BeginStatus = Literal["started", "cancelled"]
CleanupAction = Literal["delete", "defer"]


@dataclass(frozen=True)
class BeginResult:
    status: BeginStatus
    snapshot: "LifecycleSnapshot"

    @property
    def started(self) -> bool:
        return self.status == "started"


@dataclass(frozen=True)
class CleanupDecision:
    action: CleanupAction
    snapshot: "LifecycleSnapshot"

    @property
    def should_delete(self) -> bool:
        return self.action == "delete"


@dataclass(frozen=True)
class LifecycleSnapshot:
    item_id: str
    status: LifecycleStatus
    in_flight: int


@dataclass(frozen=True)
class ItemLifecycleStats:
    total_items: int
    active_items: int
    cancelling_items: int
    drained_items: int
    cleaned_items: int
    in_flight_items: int
    total_in_flight: int


@dataclass
class _LifecycleRecord:
    status: LifecycleStatus
    in_flight: int = 0
    terminal_expires_at: float | None = None


class ItemLifecycleRegistry:
    """In-memory lifecycle registry for exec item ownership.

    The registry is intentionally synchronous. It is mutated only on the event
    loop thread, around short StateStore decisions, so route code can keep the
    StateStore/lifecycle boundary free of ``await`` interleavings.
    """

    def __init__(
        self,
        *,
        terminal_retention_seconds: float = 660,
        time_source: Callable[[], float] | None = None,
    ) -> None:
        if terminal_retention_seconds <= 0:
            raise ValueError("terminal_retention_seconds must be positive")
        self.terminal_retention_seconds = terminal_retention_seconds
        self._time_source = time_source
        self._records: dict[str, _LifecycleRecord] = {}

    def begin(self, item_id: str) -> BeginResult:
        self.sweep_terminal()
        record = self._records.get(item_id)
        if record is None:
            record = _LifecycleRecord(status="active")
            self._records[item_id] = record

        if record.status in {"cancelling", "drained", "cleaned"}:
            return BeginResult(
                status="cancelled",
                snapshot=self._snapshot(item_id, record),
            )

        record.in_flight += 1
        return BeginResult(status="started", snapshot=self._snapshot(item_id, record))

    def finish(self, item_id: str) -> LifecycleSnapshot:
        record = self._records.get(item_id)
        if record is None:
            record = _LifecycleRecord(status="active")
            self._records[item_id] = record

        if record.in_flight > 0:
            record.in_flight -= 1
        if record.status == "cancelling" and record.in_flight == 0:
            self._set_terminal(record, "drained")
        return self._snapshot(item_id, record)

    def cancel(self, item_id: str) -> LifecycleSnapshot:
        self.sweep_terminal()
        record = self._records.get(item_id)
        if record is None:
            record = _LifecycleRecord(status="drained")
            self._records[item_id] = record
            self._set_terminal(record, "drained")
            return self._snapshot(item_id, record)

        if record.status == "cleaned":
            return self._snapshot(item_id, record)
        if record.in_flight == 0:
            self._set_terminal(record, "drained")
        else:
            record.status = "cancelling"
            record.terminal_expires_at = None
        return self._snapshot(item_id, record)

    def cleanup_decision(self, item_id: str) -> CleanupDecision:
        self.sweep_terminal()
        record = self._records.get(item_id)
        if record is None:
            record = _LifecycleRecord(status="active")
        if record.in_flight > 0:
            return CleanupDecision(
                action="defer",
                snapshot=self._snapshot(item_id, record),
            )
        return CleanupDecision(
            action="delete",
            snapshot=self._snapshot(item_id, record),
        )

    def mark_cleaned(self, item_id: str) -> LifecycleSnapshot:
        record = self._records.get(item_id)
        if record is None:
            record = _LifecycleRecord(status="cleaned")
            self._records[item_id] = record
        self._set_terminal(record, "cleaned")
        return self._snapshot(item_id, record)

    def snapshot(self, item_id: str) -> LifecycleSnapshot:
        record = self._records.get(item_id)
        if record is None:
            return LifecycleSnapshot(item_id=item_id, status="active", in_flight=0)
        return self._snapshot(item_id, record)

    def should_cancel(self, item_id: str) -> bool:
        record = self._records.get(item_id)
        return record is not None and record.status in {
            "cancelling",
            "drained",
            "cleaned",
        }

    def stats(self) -> ItemLifecycleStats:
        self.sweep_terminal()
        status_counts: dict[LifecycleStatus, int] = {
            "active": 0,
            "cancelling": 0,
            "drained": 0,
            "cleaned": 0,
        }
        in_flight_items = 0
        total_in_flight = 0
        for record in self._records.values():
            status_counts[record.status] += 1
            if record.in_flight > 0:
                in_flight_items += 1
                total_in_flight += record.in_flight
        return ItemLifecycleStats(
            total_items=len(self._records),
            active_items=status_counts["active"],
            cancelling_items=status_counts["cancelling"],
            drained_items=status_counts["drained"],
            cleaned_items=status_counts["cleaned"],
            in_flight_items=in_flight_items,
            total_in_flight=total_in_flight,
        )

    def sweep_terminal(self) -> int:
        now = self._now()
        expired = [
            item_id
            for item_id, record in self._records.items()
            if record.terminal_expires_at is not None
            and record.terminal_expires_at <= now
        ]
        for item_id in expired:
            self._records.pop(item_id, None)
        return len(expired)

    def _set_terminal(
        self,
        record: _LifecycleRecord,
        status: Literal["drained", "cleaned"],
    ) -> None:
        record.status = status
        record.in_flight = 0
        record.terminal_expires_at = self._now() + self.terminal_retention_seconds

    def _snapshot(
        self,
        item_id: str,
        record: _LifecycleRecord,
    ) -> LifecycleSnapshot:
        return LifecycleSnapshot(
            item_id=item_id,
            status=record.status,
            in_flight=record.in_flight,
        )

    def _now(self) -> float:
        if self._time_source is not None:
            return self._time_source()
        import time

        return time.monotonic()
