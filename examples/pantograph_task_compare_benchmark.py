"""Compare Pantograph task backend item parallelism on a frozen workload.

This benchmark freezes exactly N root proof states and exactly K tactic strings
per item, then runs the same workload through different
`max_parallel_items_per_lean_process` settings. It reports create-state timing
separately from step timing so setup cost cannot be mistaken for throughput.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from examples.pantograph_benchmark.frozen_compare import (  # noqa: E402
    FrozenCompareConfig,
    cleanup_items,
    compare_result_sets,
    create_frozen_roots,
    freeze_step_items,
    frozen_metadata,
    frozen_header_group_report,
    frozen_workload_signature,
    read_frozen_workload,
    result_signature,
    select_valid_workloads,
    write_frozen_workload,
)
from examples.pantograph_benchmark.metrics import (  # noqa: E402
    BackendStatsSampler,
    MetricsCollector,
    RssSampler,
    build_phase_report,
    fetch_backend_stats,
    state_store_usage,
)
from examples.pantograph_benchmark.mining import build_workload  # noqa: E402
from examples.pantograph_step_benchmark import (  # noqa: E402
    iter_dataset_rows,
    maybe_launch_server,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-name", default="Goedel-LM/Lean-workbook-proofs")
    parser.add_argument("--split", default="train")
    parser.add_argument("--n-items", type=int, default=200)
    parser.add_argument("--candidate-proofs", type=int, default=320)
    parser.add_argument("--items-per-request", type=int, default=16)
    parser.add_argument("--tactics-per-item", type=int, default=8)
    parser.add_argument("--timeout-ms", type=int, default=180000)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--env-profile", default="lean4.29.1_mathlib")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--workload-cache",
        type=Path,
        default=Path(".cache/pantograph_benchmark/goedel_workload_compare.jsonl"),
    )
    parser.add_argument(
        "--frozen-workload",
        type=Path,
        default=Path(".cache/pantograph_benchmark/frozen_goedel_200x8.json"),
    )
    parser.add_argument(
        "--refresh-frozen",
        action="store_true",
        help="Revalidate roots and rewrite the frozen workload even if it exists.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".cache/pantograph_benchmark/results_task_frozen_compare.json"),
    )
    parser.add_argument(
        "--state-store-root",
        type=Path,
        default=Path(".cache/pantograph_benchmark/frozen_compare_state"),
    )
    parser.add_argument("--server-host", default="127.0.0.1")
    parser.add_argument("--server-port", type=int, default=8020)
    parser.add_argument("--server-start-timeout", type=float, default=180.0)
    parser.add_argument(
        "--server-log-dir",
        type=Path,
        default=Path(".cache/pantograph_benchmark/server_logs"),
        help="Directory for benchmark-launched server logs.",
    )
    parser.add_argument("--exec-backend", default="pantograph_task")
    parser.add_argument("--max-pantograph-workers", type=int, default=1)
    parser.add_argument("--max-lean-processes-per-env-profile", type=int, default=1)
    parser.add_argument("--pantograph-worker-startup-timeout-seconds", type=int, default=600)
    parser.add_argument("--max-items-per-worker-batch", type=int, default=16)
    parser.add_argument(
        "--parallel-modes",
        default="1,16",
        help="Comma-separated max_parallel_items_per_lean_process values.",
    )
    parser.add_argument(
        "--max-rows-scanned",
        type=int,
        default=None,
        help="Cap dataset rows scanned while mining candidates.",
    )
    parser.add_argument(
        "--allow-result-mismatch",
        action="store_true",
        help="Write the report without failing when modes produce different statuses.",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> dict[str, object]:
    modes = [int(part) for part in args.parallel_modes.split(",") if part.strip()]
    if not modes:
        raise SystemExit("--parallel-modes must name at least one mode")
    if args.candidate_proofs < args.n_items:
        raise SystemExit("--candidate-proofs must be >= --n-items")

    metadata = frozen_metadata(
        dataset_name=args.dataset_name,
        split=args.split,
        n_items=args.n_items,
        tactics_per_item=args.tactics_per_item,
        seed=args.seed,
    )
    frozen = None if args.refresh_frozen else read_frozen_workload(
        args.frozen_workload,
        expected_metadata=metadata,
    )
    selection_report: dict[str, object] | None = None
    if frozen is None:
        frozen, selection_report = await _build_frozen_workload(args, metadata)
    signature = frozen_workload_signature(frozen)

    mode_reports: list[dict[str, object]] = []
    for mode_index, mode in enumerate(modes):
        mode_reports.append(
            await _run_mode(
                args,
                frozen,
                mode,
                signature,
                port_offset=mode_index + 1,
            )
        )
        partial_report = _build_final_report(
            args=args,
            frozen=frozen,
            signature=signature,
            selection_report=selection_report,
            mode_reports=mode_reports,
            comparisons=_build_comparisons(mode_reports),
            modes=modes,
            complete=False,
        )
        _write_report(args.output, partial_report)

    final_report = _build_final_report(
        args=args,
        frozen=frozen,
        signature=signature,
        selection_report=selection_report,
        mode_reports=mode_reports,
        comparisons=_build_comparisons(mode_reports),
        modes=modes,
        complete=True,
    )
    _write_report(args.output, final_report)
    _assert_comparison(final_report, allow_mismatch=args.allow_result_mismatch)
    return final_report


def _build_comparisons(mode_reports: list[dict[str, object]]) -> dict[str, object]:
    comparisons: dict[str, object] = {}
    if len(mode_reports) < 2:
        return comparisons
    baseline = mode_reports[0]
    baseline_name = str(baseline["mode_name"])
    baseline_results = baseline.get("step_results_detail", [])
    for report in mode_reports[1:]:
        mode_name = str(report["mode_name"])
        comparisons[f"{baseline_name}_vs_{mode_name}"] = compare_result_sets(
            baseline_results if isinstance(baseline_results, list) else [],
            report.get("step_results_detail", [])
            if isinstance(report.get("step_results_detail"), list)
            else [],
        )
    return comparisons


def _build_final_report(
    *,
    args: argparse.Namespace,
    frozen: list,
    signature: str,
    selection_report: dict[str, object] | None,
    mode_reports: list[dict[str, object]],
    comparisons: dict[str, object],
    modes: list[int],
    complete: bool,
) -> dict[str, object]:
    return {
        "complete": complete,
        "config": {
            "dataset_name": args.dataset_name,
            "split": args.split,
            "n_items": args.n_items,
            "candidate_proofs": args.candidate_proofs,
            "items_per_request": args.items_per_request,
            "tactics_per_item": args.tactics_per_item,
            "timeout_ms": args.timeout_ms,
            "concurrency": args.concurrency,
            "env_profile": args.env_profile,
            "exec_backend": args.exec_backend,
            "server_port_base": args.server_port,
            "max_pantograph_workers": args.max_pantograph_workers,
            "max_lean_processes_per_env_profile": (
                args.max_lean_processes_per_env_profile
            ),
            "pantograph_worker_startup_timeout_seconds": (
                args.pantograph_worker_startup_timeout_seconds
            ),
            "max_items_per_worker_batch": args.max_items_per_worker_batch,
            "parallel_modes": modes,
            "seed": args.seed,
        },
        "frozen_workload": {
            "path": str(args.frozen_workload),
            "signature": signature,
            "item_count": len(frozen),
            "tactics_per_item": args.tactics_per_item,
        },
        "expected_router_grouping": frozen_header_group_report(
            frozen,
            items_per_request=args.items_per_request,
            max_items_per_worker_batch=args.max_items_per_worker_batch,
        ),
        "selection": selection_report,
        "modes": mode_reports,
        "comparisons": comparisons,
    }


def _write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2))


async def _build_frozen_workload(
    args: argparse.Namespace,
    metadata: dict[str, object],
) -> tuple[list, dict[str, object]]:
    max_rows = args.max_rows_scanned or args.candidate_proofs * 50
    workloads = build_workload(
        iter_dataset_rows(args.dataset_name, args.split, max_rows),
        dataset_name=args.dataset_name,
        split=args.split,
        n_proofs=args.candidate_proofs,
        seed=args.seed,
        max_rows_scanned=max_rows,
        cache_path=args.workload_cache,
    )
    if len(workloads) < args.n_items:
        raise SystemExit(
            f"mined only {len(workloads)} candidate workloads; need {args.n_items}"
        )

    launch_args = _server_args(args, mode=1, state_suffix="select")
    request_timeout = args.timeout_ms / 1000 + 60
    async with maybe_launch_server(launch_args) as _server_pid:
        config = FrozenCompareConfig(
            api_url=launch_args.api_url,
            env_profile=args.env_profile,
            run_id=f"select:{int(time.time())}",
            concurrency=args.concurrency,
            items_per_request=args.items_per_request,
            tactics_per_item=args.tactics_per_item,
            timeout_ms=args.timeout_ms,
            api_key=args.api_key,
        )
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            selected, selection_report = await select_valid_workloads(
                client,
                workloads,
                config=config,
                n_items=args.n_items,
            )
    frozen = freeze_step_items(
        selected,
        n_items=args.n_items,
        tactics_per_item=args.tactics_per_item,
        seed=args.seed,
    )
    write_frozen_workload(args.frozen_workload, metadata=metadata, items=frozen)
    selection_report["frozen_path"] = str(args.frozen_workload)
    selection_report["frozen_signature"] = frozen_workload_signature(frozen)
    selection_report["server_log_path"] = getattr(launch_args, "server_log_path", None)
    return frozen, selection_report


async def _run_mode(
    args: argparse.Namespace,
    frozen: list,
    mode: int,
    workload_signature: str,
    *,
    port_offset: int,
) -> dict[str, object]:
    mode_name = f"parallel_{mode}"
    launch_args = _server_args(
        args,
        mode=mode,
        state_suffix=mode_name,
        port_offset=port_offset,
    )
    request_timeout = args.timeout_ms / 1000 + 60
    run_id = f"{int(time.time())}:{mode_name}"
    item_prefix = f"bench:{run_id}:{mode_name}:"

    async with maybe_launch_server(launch_args) as server_pid:
        store_before = state_store_usage(launch_args.state_store_dir, item_prefix)
        create_collector = MetricsCollector()
        step_collector = MetricsCollector()
        cleanup_collector = MetricsCollector()
        backend_stats = None
        final_stats = None
        rss = None
        active = []
        create_failures: list[dict[str, object]] = []
        step_results: list[dict[str, object]] = []
        cleanup_deleted = None
        cleanup_wall = 0.0
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            async with RssSampler(server_pid) as rss_sampler:
                async with BackendStatsSampler(
                    client,
                    api_url=launch_args.api_url,
                    api_key=args.api_key,
                ) as backend_sampler:
                    active, create_failures, create_wall = await create_frozen_roots(
                        client,
                        frozen,
                        config=FrozenCompareConfig(
                            api_url=launch_args.api_url,
                            env_profile=args.env_profile,
                            run_id=run_id,
                            concurrency=args.concurrency,
                            items_per_request=args.items_per_request,
                            tactics_per_item=args.tactics_per_item,
                            timeout_ms=args.timeout_ms,
                            api_key=args.api_key,
                        ),
                        mode_name=mode_name,
                        collector=create_collector,
                    )
                    if len(active) == len(frozen) and not create_failures:
                        step_results, step_wall = await create_then_step(
                            client,
                            active,
                            args=args,
                            api_url=launch_args.api_url,
                            run_id=run_id,
                            collector=step_collector,
                        )
                    else:
                        step_wall = 0.0
                    cleanup_started = time.perf_counter()
                    cleanup_deleted = await cleanup_items(
                        client,
                        [state.item_id for state in active],
                        FrozenCompareConfig(
                            api_url=launch_args.api_url,
                            env_profile=args.env_profile,
                            run_id=run_id,
                            concurrency=args.concurrency,
                            items_per_request=args.items_per_request,
                            tactics_per_item=args.tactics_per_item,
                            timeout_ms=args.timeout_ms,
                            api_key=args.api_key,
                        ),
                        cleanup_collector,
                    )
                    cleanup_wall = time.perf_counter() - cleanup_started
                backend_stats = backend_sampler.summary()
            rss = rss_sampler.summary()
            final_stats = await fetch_backend_stats(
                client,
                api_url=launch_args.api_url,
                api_key=args.api_key,
            )
        store_after = state_store_usage(launch_args.state_store_dir, item_prefix)

    create_phase = build_phase_report(create_collector, wall_seconds=create_wall)
    step_phase = build_phase_report(step_collector, wall_seconds=step_wall)
    return {
        "mode_name": mode_name,
        "max_parallel_items_per_lean_process": mode,
        "api_url": launch_args.api_url,
        "workload_signature": workload_signature,
        "create_phase": create_phase,
        "create_failures": create_failures,
        "step_phase": step_phase,
        "step_result_signature": result_signature(step_results),
        "step_results_detail": step_results,
        "cleanup_phase": build_phase_report(
            cleanup_collector,
            wall_seconds=cleanup_wall,
        ),
        "cleanup": {
            "deleted_states": cleanup_deleted.deleted_states if cleanup_deleted else 0,
            "deleted_bytes": cleanup_deleted.deleted_bytes if cleanup_deleted else 0,
        },
        "memory": {
            "process_tree_rss": rss,
        },
        "backend_stats": backend_stats,
        "final_backend_stats": final_stats,
        "server_log_path": getattr(launch_args, "server_log_path", None),
        "state_store": {
            "before": store_before,
            "after": store_after,
        },
    }


async def create_then_step(
    client: httpx.AsyncClient,
    active: list,
    *,
    args: argparse.Namespace,
    api_url: str,
    run_id: str,
    collector: MetricsCollector,
) -> tuple[list[dict[str, object]], float]:
    from examples.pantograph_benchmark.frozen_compare import step_frozen_roots

    return await step_frozen_roots(
        client,
        active,
        config=FrozenCompareConfig(
            api_url=api_url,
            env_profile=args.env_profile,
            run_id=run_id,
            concurrency=args.concurrency,
            items_per_request=args.items_per_request,
            tactics_per_item=args.tactics_per_item,
            timeout_ms=args.timeout_ms,
            api_key=args.api_key,
        ),
        collector=collector,
    )


def _server_args(
    args: argparse.Namespace,
    *,
    mode: int,
    state_suffix: str,
    port_offset: int = 0,
) -> argparse.Namespace:
    server_port = args.server_port + port_offset
    output_stem = getattr(args, "output", Path("task_compare")).stem
    return argparse.Namespace(
        launch_server=True,
        server_pid=None,
        api_url=f"http://{args.server_host}:{server_port}",
        server_host=args.server_host,
        server_port=server_port,
        server_start_timeout=args.server_start_timeout,
        server_log_dir=args.server_log_dir,
        server_log_name=(
            f"task_compare_{output_stem}_{state_suffix}_port_{server_port}_par_{mode}"
        ),
        exec_backend=args.exec_backend,
        max_pantograph_workers=args.max_pantograph_workers,
        max_lean_processes_per_env_profile=args.max_lean_processes_per_env_profile,
        pantograph_worker_startup_timeout_seconds=(
            args.pantograph_worker_startup_timeout_seconds
        ),
        items_per_request=args.items_per_request,
        tactics_per_item=args.tactics_per_item,
        max_items_per_worker_batch=args.max_items_per_worker_batch,
        max_parallel_items_per_lean_process=mode,
        state_store_dir=args.state_store_root / state_suffix,
    )


def _assert_comparison(
    report: dict[str, object],
    *,
    allow_mismatch: bool,
) -> None:
    modes = report.get("modes", [])
    if not isinstance(modes, list):
        raise SystemExit("invalid report: modes is not a list")
    frozen_workload = report.get("frozen_workload", {})
    if not isinstance(frozen_workload, dict):
        raise SystemExit("invalid report: frozen_workload is not a dict")
    expected_signature = frozen_workload.get("signature")
    for mode in modes:
        if not isinstance(mode, dict):
            continue
        if mode.get("workload_signature") != expected_signature:
            raise SystemExit(
                f"mode {mode.get('mode_name')} used a different frozen workload"
            )
        create_phase = mode.get("create_phase", {})
        if isinstance(create_phase, dict) and create_phase.get("created_states") != report[
            "config"
        ]["n_items"]:
            raise SystemExit(f"mode {mode.get('mode_name')} did not create all roots")
        step_phase = mode.get("step_phase", {})
        expected_results = (
            int(report["config"]["n_items"]) * int(report["config"]["tactics_per_item"])
        )
        if isinstance(step_phase, dict) and step_phase.get("step_results") != expected_results:
            raise SystemExit(f"mode {mode.get('mode_name')} did not step all tactics")
    comparisons = report.get("comparisons", {})
    if allow_mismatch or not isinstance(comparisons, dict):
        return
    for name, comparison in comparisons.items():
        if isinstance(comparison, dict) and not comparison.get("equivalent", False):
            raise SystemExit(f"mode results differ for {name}: {comparison}")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    report = asyncio.run(run(args))
    _write_report(args.output, report)
    print(json.dumps(report, indent=2))
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
