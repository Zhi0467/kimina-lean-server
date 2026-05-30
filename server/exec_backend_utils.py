"""Pure helper functions for the exec step-batch backends.

These functions contain no I/O and no Lean/Pantograph interaction, so they can
be unit-tested in isolation with plain data. The async orchestration that drives
the Pantograph worker pool lives in :mod:`server.exec_backends`.

They are shared by the bounded ``pantograph_process_pool`` backend, which groups
items that can reuse the same loaded Lean process and then spreads each group
across a fixed number of sequential worker *lanes*.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

ItemType = TypeVar("ItemType")

# Grouping key for a single step-batch item: items that share this key may be
# processed by the same Pantograph worker process because they were created
# against the same environment profile and Lean header.
CompatibilityKey = tuple[str, str]


def compatibility_key(env_profile: str, header_hash_value: str) -> CompatibilityKey:
    """Return the worker-compatibility key for an item.

    Two items can safely reuse the same Pantograph process only when both their
    environment profile and header hash agree; otherwise the worker would have a
    different set of imports loaded. The header hash stands in for the full
    header text, which the caller still carries on the resolved record for the
    actual lease.
    """
    return (env_profile, header_hash_value)


@dataclass(frozen=True)
class CompatibilityGroup(Generic[ItemType]):
    """A set of step-batch items that share a worker-compatibility key."""

    key: CompatibilityKey
    items: list[ItemType]

    @property
    def env_profile(self) -> str:
        return self.key[0]

    @property
    def header_hash(self) -> str:
        return self.key[1]


def group_items_by_compatibility(
    items: Iterable[ItemType],
    key_of_item: Callable[[ItemType], CompatibilityKey],
) -> list[CompatibilityGroup[ItemType]]:
    """Group items by their compatibility key, preserving first-seen order.

    The first-seen ordering keeps results deterministic and makes the eventual
    response order easy to reason about in tests.
    """
    grouped: OrderedDict[CompatibilityKey, list[ItemType]] = OrderedDict()
    for item in items:
        grouped.setdefault(key_of_item(item), []).append(item)
    return [
        CompatibilityGroup(key=group_key, items=group_items)
        for group_key, group_items in grouped.items()
    ]


def lane_count_for_group(item_count: int, max_lanes: int) -> int:
    """Return how many worker lanes a group of ``item_count`` items should use.

    A lane corresponds to one leased Pantograph worker process held for the
    lane's whole sequence of items. We never lease more workers than there are
    items, and never more than ``max_lanes``. ``max_lanes <= 0`` means "no
    per-env-profile bound", which degenerates to one lane per item (i.e. the
    item-at-a-time behaviour).
    """
    if item_count <= 0:
        return 0
    if max_lanes <= 0:
        return item_count
    return min(max_lanes, item_count)


def distribute_items_across_lanes(
    items: Sequence[ItemType],
    max_lanes: int,
) -> list[list[ItemType]]:
    """Split ``items`` into at most ``max_lanes`` sequential lanes.

    Each returned lane is a list of items that a single Pantograph worker will
    process strictly sequentially. Items are dealt round-robin so the lanes stay
    balanced even when later items are cheaper than earlier ones. Empty lanes are
    never returned, so the caller leases exactly
    ``lane_count_for_group(len(items), max_lanes)`` workers.
    """
    effective_lane_count = lane_count_for_group(len(items), max_lanes)
    if effective_lane_count == 0:
        return []

    lanes: list[list[ItemType]] = [[] for _ in range(effective_lane_count)]
    for index, item in enumerate(items):
        lanes[index % effective_lane_count].append(item)
    return lanes


def task_chunk_timeout_ms(
    item_timeouts_ms: Sequence[int],
    max_parallel_items: int,
) -> int:
    """Return a command timeout for one Lean-side task chunk.

    ``goal.step_batch`` is one Pantograph command, so Python has only one
    subprocess timeout for the whole chunk. Lean runs at most
    ``max_parallel_items`` item tasks at a time; if that cap is 1, the timeout
    must cover every item in sequence. For larger caps, the command timeout is
    the sum of the slowest item in each wave.
    """
    if not item_timeouts_ms:
        return 1
    parallel = max(max_parallel_items, 1)
    total = 0
    for start in range(0, len(item_timeouts_ms), parallel):
        total += max(item_timeouts_ms[start : start + parallel])
    return max(total, 1)
