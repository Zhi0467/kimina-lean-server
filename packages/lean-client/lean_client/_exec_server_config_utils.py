from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


DEFAULT_MAX_IN_FLIGHT_EXEC_REQUESTS = 8
DEFAULT_MAX_QUEUED_EXEC_REQUESTS = 32
DEFAULT_MAX_STATE_STORE_BYTES = 16 * 2**30
DEFAULT_RECOMMENDED_IN_FLIGHT_STEP_BATCHES = 8
DEFAULT_EXEC_WORKER_MEMORY_BYTES = 6 * 2**30
DEFAULT_EXEC_MEMORY_HEADROOM_BYTES = 16 * 2**30
DEFAULT_EXEC_MEMORY_FRACTION_DENOMINATOR = 2


class ExecSafetyConfig(Protocol):
    @property
    def max_in_flight_exec_requests(self) -> int: ...

    @property
    def max_queued_exec_requests(self) -> int: ...

    @property
    def max_state_store_bytes(self) -> int: ...

    @property
    def allow_unbounded_exec(self) -> bool: ...


def default_exec_worker_count(
    *,
    cpu_count: int | None = None,
    memory_capacity_bytes: int | None = None,
) -> int:
    cpu_bound = max(
        ((cpu_count if cpu_count is not None else os.cpu_count()) or 1) - 1,
        1,
    )
    if memory_capacity_bytes is None:
        memory_capacity_bytes = _memory_capacity_bytes()
    if memory_capacity_bytes is None:
        return cpu_bound

    memory_budget = max(
        memory_capacity_bytes // DEFAULT_EXEC_MEMORY_FRACTION_DENOMINATOR
        - DEFAULT_EXEC_MEMORY_HEADROOM_BYTES,
        0,
    )
    memory_bound = max(memory_budget // DEFAULT_EXEC_WORKER_MEMORY_BYTES, 1)
    return max(min(cpu_bound, memory_bound), 1)


def default_max_in_flight_exec_requests(worker_count: int) -> int:
    return max(min(worker_count, DEFAULT_MAX_IN_FLIGHT_EXEC_REQUESTS), 1)


def default_max_queued_exec_requests(max_in_flight: int) -> int:
    return max(min(max_in_flight * 4, DEFAULT_MAX_QUEUED_EXEC_REQUESTS), 4)


def validate_bounded_exec_caps(config: ExecSafetyConfig) -> None:
    _validate_exec_cap_value(
        "max_in_flight_exec_requests",
        config.max_in_flight_exec_requests,
        allow_zero=False,
    )
    _validate_exec_cap_value(
        "max_queued_exec_requests",
        config.max_queued_exec_requests,
        allow_zero=True,
    )
    _validate_exec_cap_value(
        "max_state_store_bytes",
        config.max_state_store_bytes,
        allow_zero=True,
    )
    if config.allow_unbounded_exec:
        return

    unbounded = [
        name
        for name, value in (
            ("max_in_flight_exec_requests", config.max_in_flight_exec_requests),
            ("max_queued_exec_requests", config.max_queued_exec_requests),
            ("max_state_store_bytes", config.max_state_store_bytes),
        )
        if value == -1
    ]
    if unbounded:
        joined = ", ".join(f"{name}=-1" for name in unbounded)
        raise ValueError(
            "unbounded /exec safety caps require allow_unbounded_exec=True: "
            f"{joined}"
        )


def append_cli_option(args: list[str], flag: str, value: int | str | Path) -> None:
    args.extend([flag, str(value)])


def _validate_exec_cap_value(
    name: str,
    value: int,
    *,
    allow_zero: bool,
) -> None:
    minimum = 0 if allow_zero else 1
    if value == -1 or value >= minimum:
        return
    if allow_zero:
        raise ValueError(f"{name} must be -1 or non-negative")
    raise ValueError(f"{name} must be -1 or positive")


def _memory_capacity_bytes() -> int | None:
    cgroup_capacity = _cgroup_memory_capacity_bytes()
    if cgroup_capacity is not None:
        return cgroup_capacity
    proc_capacity = _proc_mem_total_bytes()
    if proc_capacity is not None:
        return proc_capacity
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None


def _cgroup_memory_capacity_bytes() -> int | None:
    return _read_cgroup_int("/sys/fs/cgroup/memory.max")


def _read_cgroup_int(path: str) -> int | None:
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw or raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _proc_mem_total_bytes() -> int | None:
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, _, rest = line.partition(":")
        if key != "MemTotal":
            continue
        parts = rest.strip().split()
        if not parts:
            return None
        try:
            value = int(parts[0])
        except ValueError:
            return None
        unit = parts[1].lower() if len(parts) > 1 else "kb"
        if unit == "kb":
            return value * 1024
        return value
    return None
