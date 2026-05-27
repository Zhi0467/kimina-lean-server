from __future__ import annotations

import secrets
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


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

    @staticmethod
    def _default_token() -> str:
        return "st_" + secrets.token_urlsafe(24)

    def put(
        self,
        path: Path,
        *,
        item_id: str,
        env_profile: str,
        header_hash: str,
    ) -> str:
        source_path = Path(path)
        if not source_path.is_file():
            raise FileNotFoundError(source_path)

        state_token = self._new_token()
        target_path = self.root_dir / f"{state_token}.bin"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_path), target_path)

        now = self._now()
        record = StateRecord(
            state_token=state_token,
            item_id=item_id,
            env_profile=env_profile,
            header_hash=header_hash,
            path=target_path,
            created_at=now,
            last_accessed_at=now,
            size_bytes=target_path.stat().st_size,
        )
        self._records[state_token] = record
        self._tokens_by_item_id.setdefault(item_id, set()).add(state_token)
        return state_token

    def resolve(self, state_token: str) -> StateRecord:
        record = self._records.get(state_token)
        if record is None:
            raise StateTokenNotFound(state_token)

        updated = StateRecord(
            state_token=record.state_token,
            item_id=record.item_id,
            env_profile=record.env_profile,
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
        return self._delete_tokens(expired)

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
            try:
                record.path.unlink()
            except FileNotFoundError:
                pass
            deleted_states += 1
            deleted_bytes += record.size_bytes
        return DeleteStats(deleted_states=deleted_states, deleted_bytes=deleted_bytes)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
