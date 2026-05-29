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
import os
import signal
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import AsyncIterator, IO, Iterator

# Allow `from examples.pantograph_benchmark...` when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from examples.pantograph_benchmark.metrics import (  # noqa: E402
    BackendStatsSampler,
    MetricsCollector,
    RssSampler,
    build_report,
    fetch_backend_stats,
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
    parser.add_argument("--items-per-request", type=int, default=16)
    parser.add_argument("--tactics-per-item", type=int, default=8)
    parser.add_argument("--max-replay-depth", type=int, default=3)
    parser.add_argument("--timeout-ms", type=int, default=180000)
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
        "--launch-server",
        action="store_true",
        help="Launch a configured benchmark server subprocess for this run.",
    )
    parser.add_argument("--server-host", default="127.0.0.1")
    parser.add_argument("--server-port", type=int, default=8000)
    parser.add_argument("--server-start-timeout", type=float, default=180.0)
    parser.add_argument(
        "--server-log-dir",
        type=Path,
        default=Path(".cache/pantograph_benchmark/server_logs"),
        help="Directory for logs from benchmark-launched server subprocesses.",
    )
    parser.add_argument("--exec-backend", default="pantograph_task")
    parser.add_argument("--max-pantograph-workers", type=int, default=2)
    parser.add_argument("--max-lean-processes-per-env-profile", type=int, default=1)
    parser.add_argument("--pantograph-worker-startup-timeout-seconds", type=int, default=600)
    parser.add_argument("--max-items-per-worker-batch", type=int, default=16)
    parser.add_argument("--max-parallel-items-per-lean-process", type=int, default=16)
    parser.add_argument("--prewarm-proofs", type=int, default=1)
    parser.add_argument(
        "--no-assert-worker-cap",
        action="store_true",
        help="Do not fail if sampled `/exec/stats` exceeds configured worker caps.",
    )
    parser.add_argument(
        "--no-assert-cleanup",
        action="store_true",
        help="Do not fail when this run's scoped state-store files remain after cleanup.",
    )
    parser.add_argument(
        "--no-assert-backend",
        action="store_true",
        help="Do not fail when `/exec/stats` does not report the requested backend mode.",
    )
    parser.add_argument(
        "--max-rows-scanned",
        type=int,
        default=None,
        help="Cap dataset rows scanned while mining (defaults to n_proofs * 50).",
    )
    return parser.parse_args(argv)


@asynccontextmanager
async def maybe_launch_server(args: argparse.Namespace) -> AsyncIterator[int | None]:
    if not args.launch_server:
        yield args.server_pid
        return

    env = os.environ.copy()
    env.update(
        {
            "LEAN_SERVER_HOST": args.server_host,
            "LEAN_SERVER_PORT": str(args.server_port),
            "LEAN_SERVER_EXEC_BACKEND": args.exec_backend,
            "LEAN_SERVER_MAX_PANTOGRAPH_WORKERS": str(args.max_pantograph_workers),
            "LEAN_SERVER_MAX_LEAN_PROCESSES_PER_ENV_PROFILE": str(
                args.max_lean_processes_per_env_profile
            ),
            "LEAN_SERVER_PANTOGRAPH_WORKER_STARTUP_TIMEOUT_SECONDS": str(
                args.pantograph_worker_startup_timeout_seconds
            ),
            "LEAN_SERVER_MAX_ITEMS_PER_STEP_BATCH": str(args.items_per_request),
            "LEAN_SERVER_MAX_TACTICS_PER_STEP_ITEM": str(args.tactics_per_item),
            "LEAN_SERVER_MAX_ATTEMPTS_PER_STEP_BATCH": str(
                args.items_per_request * args.tactics_per_item
            ),
            "LEAN_SERVER_MAX_ITEMS_PER_WORKER_BATCH": str(
                args.max_items_per_worker_batch
            ),
            "LEAN_SERVER_MAX_PARALLEL_ITEMS_PER_LEAN_PROCESS": str(
                args.max_parallel_items_per_lean_process
            ),
            "LEAN_SERVER_STATE_STORE_DIR": str(args.state_store_dir.resolve()),
            "LEAN_SERVER_DATABASE_URL": "",
        }
    )
    # Track A: optional toggle so the parallel path can be compared with and
    # without single-threaded pre-realization of lazy Lean Environment state.
    # Absent flag -> rely on the server default (warmup on).
    task_warmup = getattr(args, "task_warmup", None)
    if task_warmup is not None:
        env["LEAN_SERVER_PANTOGRAPH_TASK_WARMUP"] = "true" if task_warmup else "false"
    args.api_url = f"http://{args.server_host}:{args.server_port}"
    log_handle: IO[bytes] | None = None
    stdout: int | IO[bytes] | None = asyncio.subprocess.DEVNULL
    stderr: int | IO[bytes] | None = asyncio.subprocess.DEVNULL
    server_log_dir = getattr(args, "server_log_dir", None)
    if server_log_dir is not None:
        log_dir = Path(server_log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_name = getattr(args, "server_log_name", None)
        if not log_name:
            log_name = f"server_{args.server_port}_{int(time.time() * 1000)}"
        safe_log_name = _safe_log_name(str(log_name))
        log_path = log_dir / f"{safe_log_name}.log"
        log_handle = log_path.open("ab")
        log_handle.write(
            f"\n--- launch {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"port={args.server_port} backend={args.exec_backend} ---\n".encode()
        )
        log_handle.flush()
        args.server_log_path = str(log_path)
        stdout = log_handle
        stderr = asyncio.subprocess.STDOUT
    else:
        args.server_log_path = None

    try:
        process = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "python",
            "-m",
            "server",
            env=env,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    except Exception:
        if log_handle is not None:
            log_handle.close()
        raise
    try:
        await _wait_for_server(
            args.api_url,
            args.server_start_timeout,
            process=process,
            log_path=getattr(args, "server_log_path", None),
        )
        yield process.pid
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
        if log_handle is not None:
            log_handle.close()


async def _wait_for_server(
    api_url: str,
    timeout_seconds: float,
    *,
    process: asyncio.subprocess.Process | None = None,
    log_path: str | None = None,
) -> None:
    deadline = time.perf_counter() + timeout_seconds
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.perf_counter() < deadline:
            if process is not None:
                returncode = await _poll_returncode(process)
                if returncode is not None:
                    message = (
                        "server exited before becoming healthy "
                        f"(returncode={returncode})"
                    )
                    if log_path:
                        message += f"; log={log_path}"
                    raise RuntimeError(message)
            try:
                response = await client.get(f"{api_url.rstrip('/')}/health")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    message = f"server did not become healthy within {timeout_seconds}s"
    if log_path:
        message += f"; log={log_path}"
    raise TimeoutError(message)


async def _poll_returncode(process: asyncio.subprocess.Process) -> int | None:
    if process.returncode is not None:
        return process.returncode
    try:
        await asyncio.wait_for(process.wait(), timeout=0.001)
    except TimeoutError:
        return None
    return process.returncode


def _safe_log_name(name: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in name)


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
    async with maybe_launch_server(args) as server_pid:
        backend_stats: dict[str, object] | None = None
        final_stats: dict[str, object] | None = None
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

        request_timeout = args.timeout_ms / 1000 + 60
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            if args.prewarm_proofs > 0:
                warmup_workloads = workloads[: args.prewarm_proofs]
                warmup_config = replace(
                    config,
                    run_id=f"{run_id}:warmup",
                    concurrency=max(1, min(config.concurrency, args.prewarm_proofs)),
                    items_per_request=max(1, min(config.items_per_request, args.prewarm_proofs)),
                    tactics_per_item=1,
                    max_replay_depth=1,
                )
                await run_replay(
                    client,
                    warmup_workloads,
                    warmup_config,
                    MetricsCollector(),
                    pool,
                )

            started = time.perf_counter()
            async with RssSampler(server_pid) as rss_sampler:
                async with BackendStatsSampler(
                    client,
                    api_url=config.api_url,
                    api_key=config.api_key,
                ) as backend_sampler:
                    cleanup = await run_replay(
                        client,
                        workloads,
                        config,
                        collector,
                        pool,
                    )
                backend_stats = backend_sampler.summary()
            rss = rss_sampler.summary()
            final_stats = await fetch_backend_stats(
                client,
                api_url=args.api_url,
                api_key=args.api_key,
            )
        wall_seconds = time.perf_counter() - started

    store_after = state_store_usage(args.state_store_dir, item_prefix)
    report = build_report(
        collector,
        wall_seconds=wall_seconds,
        cleanup_deleted_states=cleanup.deleted_states,
        cleanup_deleted_bytes=cleanup.deleted_bytes,
        rss=rss,
        backend_stats=backend_stats,
        state_store_before=store_before,
        state_store_after=store_after,
    )
    report["config"] = {
        "dataset_name": args.dataset_name,
        "n_proofs": len(workloads),
        "exec_backend": args.exec_backend,
        "concurrency": args.concurrency,
        "items_per_request": args.items_per_request,
        "tactics_per_item": args.tactics_per_item,
        "max_replay_depth": args.max_replay_depth,
        "max_pantograph_workers": args.max_pantograph_workers,
        "max_lean_processes_per_env_profile": (
            args.max_lean_processes_per_env_profile
        ),
        "max_items_per_worker_batch": args.max_items_per_worker_batch,
        "max_parallel_items_per_lean_process": (
            args.max_parallel_items_per_lean_process
        ),
        "prewarm_proofs": args.prewarm_proofs,
        "launch_server": args.launch_server,
        "server_pid": server_pid,
        "run_id": run_id,
    }
    report["final_backend_stats"] = final_stats
    _assert_report(args, report)
    return report


def _assert_report(args: argparse.Namespace, report: dict[str, object]) -> None:
    if not args.no_assert_backend:
        observed_backend = _observed_exec_backend(report)
        if observed_backend is None:
            raise SystemExit(
                "backend stats unavailable; cannot prove the requested "
                f"exec_backend={args.exec_backend!r} was exercised"
            )
        if observed_backend != args.exec_backend:
            raise SystemExit(
                "backend mismatch: "
                f"observed exec_backend={observed_backend!r}, "
                f"requested {args.exec_backend!r}"
            )

    if not args.no_assert_cleanup:
        state_store = report.get("state_store", {})
        after = state_store.get("after", {}) if isinstance(state_store, dict) else {}
        if isinstance(after, dict) and int(after.get("state_count", 0)) != 0:
            raise SystemExit(f"state cleanup failed: after={after}")

    backend_stats = report.get("backend_stats")
    if args.no_assert_worker_cap:
        return
    if not isinstance(backend_stats, dict):
        raise SystemExit("backend stats unavailable; cannot prove worker caps")

    max_total = int(backend_stats.get("max_total_workers", 0))
    if max_total > args.max_pantograph_workers:
        raise SystemExit(
            "worker cap violated: "
            f"max_total_workers={max_total} > {args.max_pantograph_workers}"
        )

    if args.max_lean_processes_per_env_profile > 0:
        by_env = backend_stats.get("max_workers_by_env_profile", {})
        if isinstance(by_env, dict):
            for env_profile, count in by_env.items():
                count_int = int(count)
                if count_int > args.max_lean_processes_per_env_profile:
                    raise SystemExit(
                        "per-env worker cap violated: "
                        f"{env_profile}={count_int} > "
                        f"{args.max_lean_processes_per_env_profile}"
                    )


def _observed_exec_backend(report: dict[str, object]) -> str | None:
    for key in ("final_backend_stats", "backend_stats"):
        payload = report.get(key)
        if key == "backend_stats" and isinstance(payload, dict):
            payload = payload.get("final")
        if not isinstance(payload, dict):
            continue
        settings = payload.get("settings")
        if not isinstance(settings, dict):
            continue
        backend = settings.get("exec_backend")
        if isinstance(backend, str):
            return backend
    return None


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
