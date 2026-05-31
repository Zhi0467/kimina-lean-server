from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

MicrobatchStatus = Literal["pending", "running", "complete", "failed", "unknown"]


class UncertainMicrobatchError(RuntimeError):
    def __init__(self, env_call_id: str, microbatch_id: int, status: str) -> None:
        super().__init__(
            "microbatch outcome is uncertain: "
            f"env_call_id={env_call_id!r}, microbatch_id={microbatch_id}, "
            f"status={status!r}"
        )
        self.env_call_id = env_call_id
        self.microbatch_id = microbatch_id
        self.status = status


@dataclass(frozen=True)
class ExecMicrobatchRecord:
    env_call_id: str
    microbatch_id: int
    status: MicrobatchStatus
    request_payload: dict[str, Any]
    response_payload: dict[str, Any] | None = None


class ExecMicrobatchJournal:
    """Small JSON-backed journal for client-side microbatch resume."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def get(self, env_call_id: str, microbatch_id: int) -> ExecMicrobatchRecord | None:
        raw = self._read()
        record = raw.get(env_call_id, {}).get(str(microbatch_id))
        if not isinstance(record, dict):
            return None
        return ExecMicrobatchRecord(
            env_call_id=env_call_id,
            microbatch_id=microbatch_id,
            status=cast(MicrobatchStatus, record["status"]),
            request_payload=record["request_payload"],
            response_payload=record.get("response_payload"),
        )

    def put(
        self,
        *,
        env_call_id: str,
        microbatch_id: int,
        status: MicrobatchStatus,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any] | None = None,
    ) -> None:
        raw = self._read()
        raw.setdefault(env_call_id, {})[str(microbatch_id)] = {
            "status": status,
            "request_payload": request_payload,
            "response_payload": response_payload,
        }
        self._write(raw)

    def _read(self) -> dict[str, dict[str, dict[str, Any]]]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text())
        if not isinstance(raw, dict):
            return {}
        return cast(dict[str, dict[str, dict[str, Any]]], raw)

    def _write(self, raw: dict[str, dict[str, dict[str, Any]]]) -> None:
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(raw, indent=2, sort_keys=True))
        tmp_path.replace(self.path)
