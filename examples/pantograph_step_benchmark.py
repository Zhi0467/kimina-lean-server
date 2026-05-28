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
import json
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
    build_workload,
    distractor_pool,
)
from examples.pantograph_benchmark.replay import ReplayConfig, run_replay  # noqa: E402


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
        async with RssSampler(args.server_pid) as sampler:
            cleanup = await run_replay(client, workloads, config, collector, pool)
        rss = sampler.summary()
    wall_seconds = time.perf_counter() - started

    store_after = state_store_usage(args.state_store_dir, item_prefix)
    report = build_report(
        collector,
        wall_seconds=wall_seconds,
        cleanup_deleted_states=cleanup.deleted_states,
        cleanup_deleted_bytes=cleanup.deleted_bytes,
        rss=rss,
        state_store_before=store_before,
        state_store_after=store_after,
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


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
