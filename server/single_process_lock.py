from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import TextIO


class SingleProcessLockError(RuntimeError):
    """Raised when another server already owns the state-store lock."""


class SingleProcessLock:
    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path.expanduser().resolve()
        self._file: TextIO | None = None

    def acquire(self) -> None:
        if self._file is not None:
            return

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.lock_path.open("a+")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise SingleProcessLockError(
                "another Kimina Lean Server process is already using "
                f"state_store_dir={self.lock_path.parent}"
            ) from exc

        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"{os.getpid()}\n")
        lock_file.flush()
        self._file = lock_file

    def release(self) -> None:
        lock_file = self._file
        if lock_file is None:
            return
        self._file = None
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()
            self.lock_path.unlink(missing_ok=True)
