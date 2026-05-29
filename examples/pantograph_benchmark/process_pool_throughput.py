"""End-to-end throughput + memory + equivalence benchmark for the bounded
process-pool backend, driven through the *real* PantographManager and
StateStore (the same code the ``/exec/step_batch`` endpoint runs).

It answers the three questions the design must satisfy, on real Mathlib states:

1. Equivalence  — are the per-attempt results with N worker processes
   byte-identical (status + goals) to running with a single process?
2. Throughput   — how does tactics/sec scale as the pool grows from 1 to N
   processes (the step phase is timed on a warm pool)?
3. Memory       — what is the total *private* footprint of the warm pool
   (phys_footprint per worker, which excludes shared mmap'd .olean pages)?

Run (no rebuild; reuse the prebuilt repl binary). Close other memory-heavy apps
first so N workers fit:

    PYTHONPATH="$PWD:$PWD/third_party/PyPantograph" \
        .venv/bin/python examples/pantograph_benchmark/process_pool_throughput.py \
            --processes 4 --items 32 --tactics 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import psutil  # type: ignore[import-untyped]

import pantograph  # type: ignore[reportMissingTypeStubs]

from server.exec_backends import StepBatchBackendConfig, execute_step_batch_request
from server.pantograph_manager import PantographManager, header_hash
from server.schemas_exec import StepBatchRequest, StepBatchResponse
from server.state_store import StateStore

REPO_ROOT = Path(__file__).resolve().parents[2]
MATHLIB_PROJECT = REPO_ROOT / "mathlib4"
ENV_PROFILE = "bench"
HEADER = "import Mathlib\nimport Aesop"
IMPORTS = ["Mathlib", "Aesop"]

# Mathlib-flavoured goals + tactics that force real elaboration / simp / omega
# work (and the lazy realization that breaks in-process tasking), so the timing
# reflects genuine proof stepping rather than pickle overhead.
GOAL_TACTICS: list[tuple[str, list[str]]] = [
    ("forall (a b : Nat), a + b = b + a", ["intro a", "intro b", "omega"]),
    ("forall (n : Nat), n + 0 = n", ["intro n", "simp"]),
    ("forall (a b c : Nat), a + b + c = c + b + a", ["intro a", "intro b", "intro c", "omega"]),
    ("forall (p q : Prop), p /\\ q -> q /\\ p", ["intro p", "intro q", "intro h", "exact ⟨h.2, h.1⟩"]),
    ("forall (n : Nat), 0 + n = n", ["intro n", "simp"]),
    ("forall (a b : Int), a + b = b + a", ["intro a", "intro b", "omega"]),
]


def _phys_footprint_mb(pid: int) -> float | None:
    try:
        output = subprocess.run(
            ["vmmap", "--summary", str(pid)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        ).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in output.splitlines():
        if line.strip().startswith("Physical footprint:"):
            value = line.split(":")[1].strip()
            number = float(value.rstrip("KMG"))
            if value.endswith("G"):
                return number * 1024
            if value.endswith("K"):
                return number / 1024
            return number
    return None


async def _create_parent_states(
    store: StateStore,
    parents_dir: Path,
    item_count: int,
) -> list[str]:
    """Use a throwaway server to create + pickle ``item_count`` parent goals."""
    parents_dir.mkdir(parents=True, exist_ok=True)
    server = await pantograph.Server.create(
        imports=IMPORTS,
        project_path=str(MATHLIB_PROJECT),
        timeout=300,
        buffer_limit=2_000_000,
    )
    tokens: list[str] = []
    try:
        header_hash_value = header_hash(HEADER)
        for index in range(item_count):
            goal_expr, _ = GOAL_TACTICS[index % len(GOAL_TACTICS)]
            goal_state = await server.goal_start_async(goal_expr)
            parent_path = parents_dir / f"parent_{index}.bin"
            await server.goal_save_async(goal_state, str(parent_path))
            tokens.append(
                store.put(
                    parent_path,
                    item_id=f"item_{index}",
                    env_profile=ENV_PROFILE,
                    header=HEADER,
                    header_hash=header_hash_value,
                )
            )
    finally:
        close = getattr(server, "_close", None)
        if close is not None:
            close()
    return tokens


def _build_request(tokens: list[str], timeout_ms: int) -> StepBatchRequest:
    return StepBatchRequest(
        items=[
            {
                "node_id": f"n{index}",
                "state_token": token,
                "tactics": GOAL_TACTICS[index % len(GOAL_TACTICS)][1],
                "timeout_ms": timeout_ms,
            }
            for index, token in enumerate(tokens)
        ]
    )


def _config(processes: int) -> StepBatchBackendConfig:
    return StepBatchBackendConfig(
        exec_backend="pantograph_process_pool",
        max_items_per_step_batch=10_000,
        max_tactics_per_step_item=64,
        max_attempts_per_step_batch=1_000_000,
        max_items_per_worker_batch=16,
        max_parallel_items_per_lean_process=16,
        max_lean_processes_per_env_profile=processes,
    )


def _comparable(response: StepBatchResponse) -> dict[str, list[tuple[str, str, tuple[str, ...]]]]:
    """Per-node list of (tactic, status, goals) — token-independent."""
    return {
        item.node_id: [
            (result.tactic, result.status, tuple(result.goals))
            for result in item.results
        ]
        for item in response.items
    }


def _attempt_count(request: StepBatchRequest) -> int:
    return sum(len(item.tactics) for item in request.items)


async def main() -> None:
    parser = argparse.ArgumentParser(description="process-pool throughput benchmark")
    parser.add_argument("--processes", type=int, default=4, help="pool size N for the parallel run")
    parser.add_argument("--items", type=int, default=32)
    parser.add_argument("--tactics", type=int, default=0, help="(unused; tactics come from GOAL_TACTICS)")
    parser.add_argument("--timeout-ms", type=int, default=30_000)
    args = parser.parse_args()

    store = StateStore(REPO_ROOT / ".cache" / "bench_state", ttl_seconds=3600, max_bytes=-1)
    manager = PantographManager(
        max_workers=args.processes,
        project_path=MATHLIB_PROJECT,
        buffer_limit=2_000_000,
        max_worker_uses=-1,
        max_workers_per_env_profile=args.processes,
        worker_startup_timeout_seconds=600,
    )

    report: dict[str, object] = {"processes": args.processes, "items": args.items}
    try:
        tokens = await _create_parent_states(store, REPO_ROOT / ".cache" / "bench_parents", args.items)
        request = _build_request(tokens, args.timeout_ms)
        attempts = _attempt_count(request)
        report["attempts"] = attempts

        # Warm the full pool (pays the N x Mathlib startup once).
        warm_start = time.monotonic()
        await execute_step_batch_request(
            request, state_store=store, pantograph_manager=manager, config=_config(args.processes)
        )
        report["warm_wall_s"] = round(time.monotonic() - warm_start, 2)

        # Timed reference: a single process (lane count 1).
        seq_start = time.monotonic()
        reference = await execute_step_batch_request(
            request, state_store=store, pantograph_manager=manager, config=_config(1)
        )
        seq_wall = time.monotonic() - seq_start

        # Timed candidate: the full N-process pool.
        par_start = time.monotonic()
        candidate = await execute_step_batch_request(
            request, state_store=store, pantograph_manager=manager, config=_config(args.processes)
        )
        par_wall = time.monotonic() - par_start

        # Memory of the warm pool (private footprint excludes shared oleans).
        pool_stats = await manager.stats()
        worker_footprints = {
            worker.pid: _phys_footprint_mb(worker.pid)
            for worker in pool_stats.workers
            if worker.pid is not None
        }

        report.update(
            {
                "equivalent": _comparable(reference) == _comparable(candidate),
                "sequential": {
                    "wall_s": round(seq_wall, 2),
                    "tactics_per_s": round(attempts / seq_wall, 2),
                },
                "parallel": {
                    "processes": args.processes,
                    "wall_s": round(par_wall, 2),
                    "tactics_per_s": round(attempts / par_wall, 2),
                },
                "speedup": round(seq_wall / par_wall, 2) if par_wall else None,
                "worker_private_footprint_mb": worker_footprints,
                "total_private_footprint_mb": round(
                    sum(v for v in worker_footprints.values() if v), 1
                ),
            }
        )
        print(json.dumps(report, indent=2))
    finally:
        await manager.cleanup()
        for item_index in range(args.items):
            store.delete_by_item_id(f"item_{item_index}")


if __name__ == "__main__":
    asyncio.run(main())
