"""Measure the marginal memory cost of additional Mathlib-loaded Lean processes.

Decisive question: when N independent Pantograph/Lean processes each
``import Mathlib``, how much *unique* system memory does each additional process
actually add? Lean memory-maps ``.olean`` files, so a large fraction of the
"3-4 GB RSS per process" may be shared clean pages served from the OS unified
buffer cache. If the marginal process is cheap, a bounded multi-process worker
pool (which is exactly-equivalent-to-sequential by construction) becomes viable
within a 16 GB budget.

Run (no rebuild, reuse the prebuilt repl binary):

    PYTHONPATH="$PWD:$PWD/third_party/PyPantograph" \
        .venv/bin/python examples/pantograph_benchmark/measure_memory_sharing.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import psutil  # type: ignore[import-untyped]

import pantograph  # type: ignore[reportMissingTypeStubs]

REPO_ROOT = Path(__file__).resolve().parents[2]
MATHLIB_PROJECT = REPO_ROOT / "mathlib4"
IMPORTS = ["Mathlib", "Aesop"]
MAX_WORKERS_TO_TRY = 3
# Stop adding workers if the OS reports less than this much memory available, so
# the measurement never drives the machine into heavy swap.
MIN_AVAILABLE_GB_TO_CONTINUE = 2.5


def gb(num_bytes: float) -> float:
    return round(num_bytes / 1_000_000_000, 3)


def system_used_bytes() -> int:
    virtual_memory = psutil.virtual_memory()
    return int(virtual_memory.total - virtual_memory.available)


def vmmap_summary(pid: int) -> dict[str, str]:
    """Best-effort: capture vmmap's shared/private split for one process."""
    try:
        output = subprocess.run(
            ["vmmap", "--summary", str(pid)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    interesting = {}
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(
            (
                "Physical footprint:",
                "Physical footprint (peak):",
                "ReadOnly portion of Libraries:",
                "Writable regions:",
                "TOTAL",
            )
        ):
            interesting[stripped.split(":")[0]] = stripped
    return interesting


async def main() -> None:
    servers: list[object] = []
    measurements: list[dict[str, object]] = []

    baseline_used = system_used_bytes()
    print(f"baseline system used: {gb(baseline_used)} GB", file=sys.stderr)

    for worker_index in range(MAX_WORKERS_TO_TRY):
        available_gb = gb(psutil.virtual_memory().available)
        if available_gb < MIN_AVAILABLE_GB_TO_CONTINUE:
            print(
                f"stopping early: only {available_gb} GB available before "
                f"worker {worker_index}",
                file=sys.stderr,
            )
            break

        used_before = system_used_bytes()
        server = await pantograph.Server.create(
            imports=IMPORTS,
            project_path=str(MATHLIB_PROJECT),
            timeout=300,
            buffer_limit=2_000_000,
        )
        servers.append(server)
        # Touch the environment so any deferred import work is realised before
        # we measure (best-effort; the import itself already builds the env).
        try:
            await server.run_async("env.inspect", {})
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(3)

        used_after = system_used_bytes()
        pid = server.proc.pid  # type: ignore[attr-defined]
        process = psutil.Process(pid)
        rss = process.memory_info().rss

        measurement = {
            "worker_index": worker_index,
            "pid": pid,
            "process_rss_gb": gb(rss),
            "system_used_delta_gb": gb(used_after - used_before),
            "cumulative_system_used_gb": gb(used_after - baseline_used),
            "available_after_gb": gb(psutil.virtual_memory().available),
            "vmmap": vmmap_summary(pid),
        }
        measurements.append(measurement)
        print(
            f"worker {worker_index}: rss={measurement['process_rss_gb']} GB, "
            f"marginal_system={measurement['system_used_delta_gb']} GB, "
            f"cumulative={measurement['cumulative_system_used_gb']} GB",
            file=sys.stderr,
        )

    summary = {
        "imports": IMPORTS,
        "n_workers": len(measurements),
        "measurements": measurements,
        "interpretation": (
            "If marginal system_used_delta for workers >= 1 is much smaller than "
            "worker 0's process_rss, .olean mmap sharing is real and a bounded "
            "multi-process pool is memory-viable."
        ),
    }
    print(json.dumps(summary, indent=2))

    for server in servers:
        close = getattr(server, "_close", None)
        if close is not None:
            close()


if __name__ == "__main__":
    asyncio.run(main())
