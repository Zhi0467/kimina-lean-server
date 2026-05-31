from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


def discard_unpromoted_state_paths(paths: Iterable[Path | None]) -> int:
    """Remove worker scratch state files that must not become public tokens."""
    removed = 0
    for path in paths:
        if path is None:
            continue
        if path.exists():
            removed += 1
        path.unlink(missing_ok=True)
    return removed
