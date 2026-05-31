"""Live HTTP benchmark for the Pantograph `/exec` backend.

Mines real proofs from a Goedel-style dataset, then drives
create_states -> step_batch -> cleanup concurrently and reports throughput,
latency, status mix, memory (process-tree RSS), and state-store usage.

Run against a server started with `uv run python -m server`:

    uv run python examples/pantograph_step_benchmark.py \
        --n-proofs 10 --concurrency 4 --server-pid <pid>
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

# Allow `from examples.pantograph_benchmark...` when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from examples.pantograph_benchmark.metrics import (  # noqa: E402
    MetricsCollector,
    RssSampler,
    build_report,
    state_store_usage,
)
from examples.pantograph_benchmark.mining import (  # noqa: E402
    ProofWorkload,
    build_workload,
    distractor_pool,
)
from examples.pantograph_benchmark.replay import ReplayConfig, run_replay  # noqa: E402
from server.exec_backend_utils import distribute_items_across_lanes  # noqa: E402
from server.pantograph_manager import header_hash  # noqa: E402
from server.split import split_snippet  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-name", default="Goedel-LM/Lean-workbook-proofs")
    parser.add_argument("--split", default="train")
    parser.add_argument("--n-proofs", type=int, default=10)
    parser.add_argument("--items-per-request", type=int, default=8)
    parser.add_argument("--tactics-per-item", type=int, default=8)
    parser.add_argument("--max-replay-depth", type=int, default=3)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--env-profile", default="lean4.29.1_mathlib")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--workload-cache",
        type=Path,
        default=Path(".cache/pantograph_benchmark/goedel_workload.jsonl"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".cache/pantograph_benchmark/results.json"),
    )
    parser.add_argument(
        "--state-store-dir",
        type=Path,
        default=Path(".leanfoundry-state"),
    )
    parser.add_argument(
        "--server-pid",
        type=int,
        default=None,
        help="PID of the server process for process-tree RSS sampling.",
    )
    parser.add_argument(
        "--max-rows-scanned",
        type=int,
        default=None,
        help="Cap dataset rows scanned while mining (defaults to n_proofs * 50).",
    )
    return parser.parse_args(argv)


def iter_dataset_rows(dataset_name: str, split: str, max_rows: int) -> Iterator[tuple[str, str]]:
    from datasets import load_dataset

    dataset = load_dataset(dataset_name, split=split, streaming=True)
    for count, row in enumerate(dataset):
        if count >= max_rows:
            break
        yield row["problem_id"], row["full_proof"]


async def run(args: argparse.Namespace) -> dict[str, object]:
    max_rows = args.max_rows_scanned or args.n_proofs * 50
    workloads = build_workload(
        iter_dataset_rows(args.dataset_name, args.split, max_rows),
        dataset_name=args.dataset_name,
        split=args.split,
        n_proofs=args.n_proofs,
        seed=args.seed,
        max_rows_scanned=max_rows,
        cache_path=args.workload_cache,
    )
    if not workloads:
        raise SystemExit("No suitable proofs mined; widen --max-rows-scanned or check dataset.")

    run_id = str(int(time.time()))
    config = ReplayConfig(
        api_url=args.api_url.rstrip("/"),
        env_profile=args.env_profile,
        run_id=run_id,
        concurrency=args.concurrency,
        items_per_request=args.items_per_request,
        tactics_per_item=args.tactics_per_item,
        max_replay_depth=args.max_replay_depth,
        timeout_ms=args.timeout_ms,
        api_key=args.api_key,
        seed=args.seed,
    )
    collector = MetricsCollector()
    pool = distractor_pool(workloads)
    item_prefix = f"bench:{run_id}:"
    store_before = state_store_usage(args.state_store_dir, item_prefix)

    request_timeout = args.timeout_ms / 1000 + 30
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=request_timeout) as client:
        exec_limits = await _get_json(client, f"{config.api_url}/exec/limits", config.api_key)
        exec_stats_before = await _get_json(
            client, f"{config.api_url}/exec/stats", config.api_key
        )
        async with RssSampler(args.server_pid) as sampler:
            cleanup = await run_replay(client, workloads, config, collector, pool)
        rss = sampler.summary()
        exec_stats_after = await _get_json(
            client, f"{config.api_url}/exec/stats", config.api_key
        )
    wall_seconds = time.perf_counter() - started

    store_after = state_store_usage(args.state_store_dir, item_prefix)
    verdict = _verdict(
        collector,
        state_store_before=store_before,
        state_store_after=store_after,
    )
    report = build_report(
        collector,
        wall_seconds=wall_seconds,
        cleanup_deleted_states=cleanup.deleted_states,
        cleanup_deleted_bytes=cleanup.deleted_bytes,
        rss=rss,
        state_store_before=store_before,
        state_store_after=store_after,
        git_sha=_git_sha(),
        backend_config={
            "api_url": config.api_url,
            "env_profile": config.env_profile,
            "max_pantograph_workers": _nested_get(
                exec_stats_after, ["worker_pool", "max_workers"]
            ),
            "max_lean_processes_per_env_profile": _nested_get(
                exec_stats_after, ["worker_pool", "max_workers_per_env_profile"]
            ),
        },
        workload_shape={
            "n_proofs": len(workloads),
            "items_per_request": args.items_per_request,
            "tactics_per_item": args.tactics_per_item,
            "max_replay_depth": args.max_replay_depth,
            "problem_ids": [workload.problem_id for workload in workloads],
            "analysis": _workload_analysis(
                workloads,
                items_per_request=args.items_per_request,
                max_lanes_per_group=_as_int(
                    _nested_get(exec_stats_after, ["worker_pool", "max_workers_per_env_profile"])
                )
                or args.items_per_request,
            ),
        },
        exec_limits=exec_limits,
        exec_stats_before=exec_stats_before,
        exec_stats_after=exec_stats_after,
        verdict=verdict,
    )
    report["config"] = {
        "dataset_name": args.dataset_name,
        "n_proofs": len(workloads),
        "concurrency": args.concurrency,
        "items_per_request": args.items_per_request,
        "tactics_per_item": args.tactics_per_item,
        "max_replay_depth": args.max_replay_depth,
        "run_id": run_id,
    }
    return report


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    api_key: str | None,
) -> dict[str, object] | None:
    headers = {"Authorization": api_key} if api_key else None
    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    payload = response.json()
    return payload if isinstance(payload, dict) else None


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _nested_get(payload: dict[str, object] | None, path: list[str]) -> object | None:
    current: object = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _as_int(value: object | None) -> int | None:
    return value if isinstance(value, int) else None


def _workload_analysis(
    workloads: list[ProofWorkload],
    *,
    items_per_request: int,
    max_lanes_per_group: int,
) -> dict[str, object]:
    header_counts: Counter[str] = Counter(
        header_hash(split_snippet(workload.root_code).header) for workload in workloads
    )
    header_group_sizes = sorted(header_counts.values(), reverse=True)
    planned_lanes: list[int] = []
    for group_size in header_group_sizes:
        planned_lanes.extend(
            len(lane)
            for lane in distribute_items_across_lanes(
                list(range(group_size)),
                max_lanes_per_group,
            )
        )
    return {
        "header_groups": {
            "count": len(header_counts),
            "sizes": header_group_sizes,
            "top": [
                {"header_hash": key, "items": count}
                for key, count in header_counts.most_common(10)
            ],
        },
        "planned_step_lanes": {
            "max_lanes_per_group": max_lanes_per_group,
            "lane_count": len(planned_lanes),
            "items_per_lane": sorted(planned_lanes, reverse=True),
        },
        "planned_microbatches": {
            "create": _planned_microbatch_count(len(workloads), items_per_request),
            "step_per_depth": _planned_microbatch_count(len(workloads), items_per_request),
            "cleanup": _planned_microbatch_count(len(workloads), items_per_request),
        },
    }


def _planned_microbatch_count(item_count: int, items_per_request: int) -> int:
    if item_count <= 0:
        return 0
    return (item_count + max(items_per_request, 1) - 1) // max(items_per_request, 1)


def _verdict(
    collector: MetricsCollector,
    *,
    state_store_before: dict[str, int],
    state_store_after: dict[str, int],
) -> dict[str, object]:
    bad_statuses = {
        status: count
        for status, count in collector.status_counts.items()
        if status.startswith("http_") or status.startswith("http_error:")
    }
    state_returned_to_baseline = state_store_after == state_store_before
    ran_lean_work = any(
        collector.status_counts.get(status, 0) > 0
        for status in ("open", "complete", "error")
    )
    success = state_returned_to_baseline and not bad_statuses and ran_lean_work
    return {
        "success": success,
        "state_store_returned_to_baseline": state_returned_to_baseline,
        "ran_lean_work": ran_lean_work,
        "bad_status_counts": bad_statuses,
        "overloaded": collector.status_counts.get("overloaded", 0),
    }


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
