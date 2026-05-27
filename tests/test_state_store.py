from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest

from server.state_store import (
    DeleteStats,
    StateStore,
    StateTokenNotFound,
    run_state_gc,
)


def _write_state(path: Path, data: bytes = b"state") -> Path:
    path.write_bytes(data)
    return path


def _token_factory(*tokens: str) -> Callable[[], str]:
    remaining = iter(tokens)
    return lambda: next(remaining)


def test_put_moves_file_and_records_metadata(tmp_path: Path) -> None:
    source = _write_state(tmp_path / "source.bin", b"root-state")
    store = StateStore(tmp_path / "store", token_factory=_token_factory("st_root"))

    token = store.put(
        source,
        item_id="theorem_42:a0",
        env_profile="lean4.29.1_mathlib_x",
        header="import Mathlib",
        header_hash="abc123",
    )

    assert token == "st_root"
    assert not source.exists()

    record = store.resolve(token)
    assert record.item_id == "theorem_42:a0"
    assert record.env_profile == "lean4.29.1_mathlib_x"
    assert record.header == "import Mathlib"
    assert record.header_hash == "abc123"
    assert record.path == tmp_path / "store" / "st_root.bin"
    assert record.path.read_bytes() == b"root-state"
    assert record.size_bytes == len(b"root-state")
    assert store.stats().state_count == 1
    assert store.stats().total_bytes == len(b"root-state")


def test_token_factory_prefixes_tokens_and_avoids_collisions(tmp_path: Path) -> None:
    store = StateStore(
        tmp_path / "store",
        token_factory=_token_factory("dup", "dup", "child"),
    )

    first = store.put(
        _write_state(tmp_path / "first.bin"),
        item_id="a",
        env_profile="env",
        header_hash="header",
    )
    second = store.put(
        _write_state(tmp_path / "second.bin"),
        item_id="b",
        env_profile="env",
        header_hash="header",
    )

    assert first == "st_dup"
    assert second == "st_child"


def test_create_child_inherits_parent_metadata(tmp_path: Path) -> None:
    store = StateStore(
        tmp_path / "store",
        token_factory=_token_factory("st_parent", "st_child"),
    )
    parent = store.put(
        _write_state(tmp_path / "parent.bin", b"parent"),
        item_id="theorem_42:a0",
        env_profile="lean4.29.1_mathlib_x",
        header="import Mathlib",
        header_hash="abc123",
    )

    child = store.create_child(parent, _write_state(tmp_path / "child.bin", b"child"))
    child_record = store.resolve(child)

    assert child_record.item_id == "theorem_42:a0"
    assert child_record.env_profile == "lean4.29.1_mathlib_x"
    assert child_record.header == "import Mathlib"
    assert child_record.header_hash == "abc123"
    assert child_record.path.read_bytes() == b"child"
    assert store.stats().state_count == 2


def test_delete_by_item_id_deletes_only_owned_files(tmp_path: Path) -> None:
    store = StateStore(
        tmp_path / "store",
        token_factory=_token_factory("st_a1", "st_a2", "st_b1"),
    )
    a1 = store.put(
        _write_state(tmp_path / "a1.bin", b"a1"),
        item_id="theorem_42:a0",
        env_profile="env",
        header_hash="header",
    )
    a2 = store.put(
        _write_state(tmp_path / "a2.bin", b"a2"),
        item_id="theorem_42:a0",
        env_profile="env",
        header_hash="header",
    )
    b1 = store.put(
        _write_state(tmp_path / "b1.bin", b"b1"),
        item_id="theorem_43",
        env_profile="env",
        header_hash="header",
    )

    deleted = store.delete_by_item_id("theorem_42:a0")

    assert deleted.deleted_states == 2
    assert deleted.deleted_bytes == len(b"a1") + len(b"a2")
    assert store.stats().state_count == 1
    assert not (tmp_path / "store" / f"{a1}.bin").exists()
    assert not (tmp_path / "store" / f"{a2}.bin").exists()
    assert store.resolve(b1).path.exists()


def test_gc_expired_deletes_stale_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = StateStore(tmp_path / "store", ttl_seconds=10, token_factory=_token_factory("st_old"))
    monkeypatch.setattr(store, "_now", lambda: now)
    token = store.put(
        _write_state(tmp_path / "old.bin", b"old"),
        item_id="theorem_42:a0",
        env_profile="env",
        header_hash="header",
    )

    now = now + timedelta(seconds=11)
    deleted = store.gc_expired()

    assert deleted.deleted_states == 1
    assert deleted.deleted_bytes == len(b"old")
    assert store.stats().state_count == 0
    with pytest.raises(StateTokenNotFound):
        store.resolve(token)


def test_gc_enforces_state_store_byte_budget_by_lru(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = StateStore(
        tmp_path / "store",
        ttl_seconds=3600,
        max_bytes=5,
        token_factory=_token_factory("st_old", "st_mid", "st_new"),
    )
    monkeypatch.setattr(store, "_now", lambda: now)
    old = store.put(
        _write_state(tmp_path / "old.bin", b"aaa"),
        item_id="theorem_42:a0",
        env_profile="env",
        header_hash="header",
    )

    now = now + timedelta(seconds=1)
    mid = store.put(
        _write_state(tmp_path / "mid.bin", b"bbb"),
        item_id="theorem_42:a0",
        env_profile="env",
        header_hash="header",
    )

    now = now + timedelta(seconds=1)
    new = store.put(
        _write_state(tmp_path / "new.bin", b"cc"),
        item_id="theorem_42:a0",
        env_profile="env",
        header_hash="header",
    )

    now = now + timedelta(seconds=1)
    store.resolve(old)

    deleted = store.gc_expired()

    assert deleted.deleted_states == 1
    assert deleted.deleted_bytes == len(b"bbb")
    assert store.stats().total_bytes == len(b"aaa") + len(b"cc")
    assert not (tmp_path / "store" / f"{mid}.bin").exists()
    assert not (tmp_path / "store" / f"{mid}.json").exists()
    assert store.resolve(old).path.exists()
    assert store.resolve(new).path.exists()


def test_records_survive_a_restart(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = StateStore(root, token_factory=_token_factory("st_root"))
    token = store.put(
        _write_state(tmp_path / "root.bin", b"root-state"),
        item_id="theorem_42:a0",
        env_profile="lean4.29.1_mathlib_x",
        header="import Mathlib",
        header_hash="abc123",
    )

    # A fresh store over the same directory mimics a server restart.
    reloaded = StateStore(root)

    record = reloaded.resolve(token)
    assert record.item_id == "theorem_42:a0"
    assert record.env_profile == "lean4.29.1_mathlib_x"
    assert record.header == "import Mathlib"
    assert record.header_hash == "abc123"
    assert record.path.read_bytes() == b"root-state"
    assert reloaded.stats().state_count == 1
    assert reloaded.delete_by_item_id("theorem_42:a0").deleted_states == 1


def test_resolve_persists_access_time_for_restart_gc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_access = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    second_access = first_access + timedelta(seconds=9)
    after_restart = first_access + timedelta(seconds=12)
    root = tmp_path / "store"

    store = StateStore(root, ttl_seconds=10, token_factory=_token_factory("st_root"))
    now = first_access
    monkeypatch.setattr(store, "_now", lambda: now)
    token = store.put(
        _write_state(tmp_path / "root.bin", b"root-state"),
        item_id="theorem_42:a0",
        env_profile="env",
        header_hash="header",
    )

    now = second_access
    store.resolve(token)

    reloaded = StateStore(root, ttl_seconds=10)
    monkeypatch.setattr(reloaded, "_now", lambda: after_restart)
    deleted = reloaded.gc_expired()

    assert deleted.deleted_states == 0
    assert reloaded.resolve(token).path.exists()


def test_gc_sweeps_untracked_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    root = tmp_path / "store"
    store = StateStore(root, ttl_seconds=10, token_factory=_token_factory("st_live"))
    monkeypatch.setattr(store, "_now", lambda: now)
    store.put(
        _write_state(tmp_path / "live.bin", b"live"),
        item_id="theorem_42:a0",
        env_profile="env",
        header_hash="header",
    )

    # An untracked scratch file left behind by a crashed worker.
    orphan = _write_state(root / "pg_orphan.bin", b"orphan")
    stale = (now - timedelta(seconds=60)).timestamp()
    os.utime(orphan, (stale, stale))

    now = now + timedelta(seconds=11)
    deleted = store.gc_expired()

    assert not orphan.exists()
    assert deleted.deleted_states == 2  # tracked "live" + orphan scratch file
    assert deleted.deleted_bytes == len(b"live") + len(b"orphan")
    assert store.stats().state_count == 0


@pytest.mark.asyncio
async def test_run_state_gc_runs_on_event_loop_thread() -> None:
    loop_thread = threading.get_ident()
    gc_thread_ids: list[int] = []
    gc_ran = asyncio.Event()

    class FakeStore:
        def gc_expired(self) -> DeleteStats:
            gc_thread_ids.append(threading.get_ident())
            gc_ran.set()
            return DeleteStats(deleted_states=0, deleted_bytes=0)

    task = asyncio.create_task(
        run_state_gc(cast(StateStore, FakeStore()), interval_seconds=0.001)
    )
    await asyncio.wait_for(gc_ran.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert gc_thread_ids == [loop_thread]


def test_state_store_search_lifecycle_e2e(tmp_path: Path) -> None:
    store = StateStore(
        tmp_path / "state-store",
        token_factory=_token_factory("st_root", "st_child"),
    )

    root_token = store.put(
        _write_state(tmp_path / "root.bin", b"root"),
        item_id="theorem_42:a0",
        env_profile="lean4.29.1_mathlib_x",
        header="import Mathlib",
        header_hash="abc123",
    )
    child_token = store.create_child(
        root_token,
        _write_state(tmp_path / "child.bin", b"child"),
    )

    root_record = store.resolve(root_token)
    child_record = store.resolve(child_token)
    assert root_record.path.exists()
    assert child_record.path.exists()
    assert child_record.item_id == root_record.item_id
    assert child_record.header == root_record.header

    deleted = store.delete_by_item_id("theorem_42:a0")

    assert deleted.deleted_states == 2
    assert store.stats().state_count == 0
    assert not root_record.path.exists()
    assert not child_record.path.exists()
