"""Pure helpers for bounded exec step-batch scheduling."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

ItemType = TypeVar("ItemType")

CompatibilityKey = tuple[str, str]


def compatibility_key(env_profile: str, header_hash_value: str) -> CompatibilityKey:
    return (env_profile, header_hash_value)


@dataclass(frozen=True)
class CompatibilityGroup(Generic[ItemType]):
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
    grouped: OrderedDict[CompatibilityKey, list[ItemType]] = OrderedDict()
    for item in items:
        grouped.setdefault(key_of_item(item), []).append(item)
    return [
        CompatibilityGroup(key=group_key, items=group_items)
        for group_key, group_items in grouped.items()
    ]


def lane_count_for_group(item_count: int, max_lanes: int) -> int:
    if item_count <= 0:
        return 0
    if max_lanes <= 0:
        return item_count
    return min(max_lanes, item_count)


def distribute_items_across_lanes(
    items: Sequence[ItemType],
    max_lanes: int,
) -> list[list[ItemType]]:
    effective_lane_count = lane_count_for_group(len(items), max_lanes)
    if effective_lane_count == 0:
        return []

    lanes: list[list[ItemType]] = [[] for _ in range(effective_lane_count)]
    for index, item in enumerate(items):
        lanes[index % effective_lane_count].append(item)
    return lanes
