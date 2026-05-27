from __future__ import annotations

import asyncio
import json
import secrets
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger


class StateStoreError(Exception):
    """Base class for state-store errors."""


class StateTokenNotFound(StateStoreError):
    def __init__(self, state_token: str) -> None:
        super().__init__(f"state token not found: {state_token}")
        self.state_token = state_token


@dataclass(frozen=True)
class StateRecord:
    state_token: str
    item_id: str
    env_profile: str
    header: str
    header_hash: str
    path: Path
    created_at: datetime
    last_accessed_at: datetime
    size_bytes: int


@dataclass(frozen=True)
class DeleteStats:
    deleted_states: int
    deleted_bytes: int


@dataclass(frozen=True)
class StateStoreStats:
    state_count: int
    total_bytes: int


class StateStore:
    """Filesystem-backed store for serialized proof states.

    Each state is a ``{token}.bin`` file in ``root_dir`` accompanied by a
    ``{token}.json`` sidecar holding its metadata. The in-memory index is
    rebuilt from those sidecars on construction, so state tokens survive a
    server restart and ``gc_expired`` can reclaim files left behind by a
    previous run.
    """

    def __init__(
        self,
        root_dir: Path,
        *,
        ttl_seconds: int = 3600,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.root_dir = root_dir
        self.ttl = timedelta(seconds=ttl_seconds)
        self._token_factory = token_factory or self._default_token
        self._records: dict[str, StateRecord] = {}
        self._tokens_by_item_id: dict[str, set[str]] = {}
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._rehydrate()

    @staticmethod
    def _default_token() -> str:
        return "st_" + secrets.token_urlsafe(24)

    def put(
        self,
        path: Path,
        *,
        item_id: str,
        env_profile: str,
        header: str = "",
        header_hash: str,
    ) -> str:
        source_path = Path(path)
        if not source_path.is_file():
            raise FileNotFoundError(source_path)

        state_token = self._new_token()
        target_path = self._state_path(state_token)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_path), target_path)

        now = self._now()
        record = StateRecord(
            state_token=state_token,
            item_id=item_id,
            env_profile=env_profile,
            header=header,
            header_hash=header_hash,
            path=target_path,
            created_at=now,
            last_accessed_at=now,
            size_bytes=target_path.stat().st_size,
        )
        self._records[state_token] = record
        self._tokens_by_item_id.setdefault(item_id, set()).add(state_token)
        self._write_metadata(record)
        return state_token

    def resolve(self, state_token: str) -> StateRecord:
        record = self._records.get(state_token)
        if record is None:
            raise StateTokenNotFound(state_token)

        updated = StateRecord(
            state_token=record.state_token,
            item_id=record.item_id,
            env_profile=record.env_profile,
            header=record.header,
            header_hash=record.header_hash,
            path=record.path,
            created_at=record.created_at,
            last_accessed_at=self._now(),
            size_bytes=record.size_bytes,
        )
        self._records[state_token] = updated
        return updated

    def create_child(self, parent_token: str, child_path: Path) -> str:
        parent = self.resolve(parent_token)
        return self.put(
            child_path,
            item_id=parent.item_id,
            env_profile=parent.env_profile,
            header=parent.header,
            header_hash=parent.header_hash,
        )

    def delete_by_item_id(self, item_id: str) -> DeleteStats:
        tokens = list(self._tokens_by_item_id.get(item_id, set()))
        return self._delete_tokens(tokens)

    def gc_expired(self) -> DeleteStats:
        cutoff = self._now() - self.ttl
        expired = [
            token
            for token, record in self._records.items()
            if record.last_accessed_at < cutoff
        ]
        tracked = self._delete_tokens(expired)
        orphans = self._sweep_orphans(cutoff)
        return DeleteStats(
            deleted_states=tracked.deleted_states + orphans.deleted_states,
            deleted_bytes=tracked.deleted_bytes + orphans.deleted_bytes,
        )

    def stats(self) -> StateStoreStats:
        return StateStoreStats(
            state_count=len(self._records),
            total_bytes=sum(record.size_bytes for record in self._records.values()),
        )

    def _new_token(self) -> str:
        while True:
            token = self._token_factory()
            if not token.startswith("st_"):
                token = f"st_{token}"
            if token not in self._records:
                return token

    def _delete_tokens(self, tokens: list[str]) -> DeleteStats:
        deleted_states = 0
        deleted_bytes = 0
        for token in tokens:
            record = self._records.pop(token, None)
            if record is None:
                continue
            item_tokens = self._tokens_by_item_id.get(record.item_id)
            if item_tokens is not None:
                item_tokens.discard(token)
                if not item_tokens:
                    self._tokens_by_item_id.pop(record.item_id, None)
            record.path.unlink(missing_ok=True)
            self._metadata_path(token).unlink(missing_ok=True)
            deleted_states += 1
            deleted_bytes += record.size_bytes
        return DeleteStats(deleted_states=deleted_states, deleted_bytes=deleted_bytes)

    def _sweep_orphans(self, cutoff: datetime) -> DeleteStats:
        """Delete untracked files older than ``cutoff``.

        Catches state files from a crashed run whose sidecar never loaded,
        lone sidecars, and the worker's ``pg_*.bin`` scratch files that were
        never promoted via :meth:`put`.
        """
        deleted_states = 0
        deleted_bytes = 0
        cutoff_ts = cutoff.timestamp()
        for path in self.root_dir.glob("*"):
            if not path.is_file() or path.stem in self._records:
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            if stat.st_mtime >= cutoff_ts:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            if path.suffix == ".bin":
                deleted_states += 1
                deleted_bytes += stat.st_size
        return DeleteStats(deleted_states=deleted_states, deleted_bytes=deleted_bytes)

    def _rehydrate(self) -> None:
        for metadata_path in self.root_dir.glob("*.json"):
            record = self._read_metadata(metadata_path)
            if record is None:
                continue
            self._records[record.state_token] = record
            self._tokens_by_item_id.setdefault(record.item_id, set()).add(
                record.state_token
            )
        if self._records:
            logger.info("State store rehydrated {} state(s)", len(self._records))

    def _read_metadata(self, metadata_path: Path) -> StateRecord | None:
        try:
            raw = json.loads(metadata_path.read_text())
            token = raw["state_token"]
            bin_path = self._state_path(token)
            if not bin_path.is_file():
                metadata_path.unlink(missing_ok=True)
                return None
            return StateRecord(
                state_token=token,
                item_id=raw["item_id"],
                env_profile=raw["env_profile"],
                header=raw.get("header", ""),
                header_hash=raw["header_hash"],
                path=bin_path,
                created_at=datetime.fromisoformat(raw["created_at"]),
                last_accessed_at=datetime.fromisoformat(raw["last_accessed_at"]),
                size_bytes=int(raw["size_bytes"]),
            )
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            logger.warning("Ignoring unreadable state metadata: {}", metadata_path)
            return None

    def _write_metadata(self, record: StateRecord) -> None:
        self._metadata_path(record.state_token).write_text(
            json.dumps(
                {
                    "state_token": record.state_token,
                    "item_id": record.item_id,
                    "env_profile": record.env_profile,
                    "header": record.header,
                    "header_hash": record.header_hash,
                    "created_at": record.created_at.isoformat(),
                    "last_accessed_at": record.last_accessed_at.isoformat(),
                    "size_bytes": record.size_bytes,
                }
            )
        )

    def _state_path(self, state_token: str) -> Path:
        return self.root_dir / f"{state_token}.bin"

    def _metadata_path(self, state_token: str) -> Path:
        return self.root_dir / f"{state_token}.json"

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)


async def run_state_gc(
    store: StateStore,
    *,
    interval_seconds: float,
) -> None:
    """Periodically reclaim expired states until cancelled.

    Runs :meth:`StateStore.gc_expired` off the event loop so its blocking
    filesystem work does not stall request handling.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            stats = await asyncio.to_thread(store.gc_expired)
        except Exception:
            logger.exception("State store GC sweep failed")
            continue
        if stats.deleted_states:
            logger.info(
                "State store GC reclaimed {} state(s), {} byte(s)",
                stats.deleted_states,
                stats.deleted_bytes,
            )
