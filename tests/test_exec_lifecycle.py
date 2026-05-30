from __future__ import annotations

from server.exec_lifecycle import ItemLifecycleRegistry


def test_lifecycle_begin_finish_and_cleaned_retention() -> None:
    now = 1000.0
    registry = ItemLifecycleRegistry(
        terminal_retention_seconds=10,
        time_source=lambda: now,
    )

    begin = registry.begin("item")
    assert begin.started
    assert begin.snapshot.in_flight == 1

    finished = registry.finish("item")
    assert finished.status == "active"
    assert finished.in_flight == 0

    cleaned = registry.mark_cleaned("item")
    assert cleaned.status == "cleaned"
    assert not registry.begin("item").started

    now = 1011.0
    assert registry.sweep_terminal() == 1
    assert registry.begin("item").started


def test_lifecycle_cancel_drains_at_zero_and_is_idempotent() -> None:
    registry = ItemLifecycleRegistry()
    assert registry.begin("item").started

    cancelling = registry.cancel("item")
    assert cancelling.status == "cancelling"
    assert cancelling.in_flight == 1
    assert not registry.begin("item").started

    drained = registry.finish("item")
    assert drained.status == "drained"
    assert drained.in_flight == 0

    repeated = registry.cancel("item")
    assert repeated.status == "drained"
    assert repeated.in_flight == 0


def test_cleanup_decision_defers_only_while_in_flight() -> None:
    registry = ItemLifecycleRegistry()
    assert registry.begin("item").started

    active = registry.cleanup_decision("item")
    assert not active.should_delete
    assert active.snapshot.in_flight == 1

    registry.finish("item")
    drained = registry.cleanup_decision("item")
    assert drained.should_delete
