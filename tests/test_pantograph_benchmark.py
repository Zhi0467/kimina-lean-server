from __future__ import annotations

import argparse
import asyncio
import json
import random
from pathlib import Path

import httpx
import pytest

from examples.pantograph_benchmark import replay as replay_module
from examples.pantograph_benchmark.metrics import (
    BackendStatsSampler,
    MetricsCollector,
    build_phase_report,
    build_report,
    fetch_backend_stats,
    percentile,
    state_store_usage,
)
from examples.pantograph_benchmark.frozen_compare import (
    FrozenCompareConfig,
    FrozenStepItem,
    compare_result_sets,
    create_frozen_roots,
    freeze_step_items,
    frozen_header_group_report,
    frozen_workload_signature,
    step_frozen_roots,
)
from examples.pantograph_benchmark.mining import (
    ProofWorkload,
    build_candidate_tactics,
    build_workload,
    distractor_pool,
    is_structured_proof,
    mine_row,
    split_header_body,
    strip_comments,
    tokenize_tactics,
)
from examples.pantograph_benchmark.replay import ReplayConfig, item_id_for, run_replay
from examples.pantograph_step_benchmark import _assert_report, _observed_exec_backend
from examples.pantograph_task_compare_benchmark import _assert_comparison, _server_args

ONE_SHOT = (
    "import Mathlib\n\n"
    "theorem t (a b : ℝ) : a + b = b + a := by\n"
    "  -- swap the summands\n"
    "  cases' le_total a b with h h <;>\n"
    "    nlinarith [sq_nonneg (a - b),\n"
    "      sq_nonneg (a + b)]\n"
)

MULTI_STEP = (
    "import Mathlib\n\n"
    "theorem t (a : ℝ) : a = a := by\n"
    "  /- a long\n     proof sketch -/\n"
    "  have h0 : a ≥ a := le_refl a\n"
    "  have h1 : a ≤ a := le_refl a\n"
    "  nlinarith [h0, h1]\n"
)

STRUCTURED = (
    "import Mathlib\n\n"
    "theorem t : True ∧ True := by\n"
    "  constructor\n"
    "  · trivial\n"
    "  · trivial\n"
)


def test_strip_comments_handles_nested_block_and_line_comments() -> None:
    src = "a /- outer /- inner -/ still -/ b -- trailing\nc"
    assert strip_comments(src).split() == ["a", "b", "c"]


def test_split_header_body_replaces_body_with_sorry() -> None:
    root_code, body = split_header_body(ONE_SHOT)
    assert root_code.rstrip().endswith(":= by sorry")
    assert "nlinarith" in body
    assert "nlinarith" not in root_code


def test_split_header_body_rejects_term_mode_proof() -> None:
    assert split_header_body("import Mathlib\ntheorem t : True := trivial\n") is None


def test_tokenize_joins_combinators_and_brackets_into_one_unit() -> None:
    _, body = split_header_body(ONE_SHOT)
    units = tokenize_tactics(body)
    assert len(units) == 1
    assert units[0].startswith("cases'")
    assert "nlinarith" in units[0]  # multi-line bracket joined


def test_tokenize_splits_sequential_tactics() -> None:
    _, body = split_header_body(MULTI_STEP)
    units = tokenize_tactics(body)
    assert units == [
        "have h0 : a ≥ a := le_refl a",
        "have h1 : a ≤ a := le_refl a",
        "nlinarith [h0, h1]",
    ]


def test_structured_proof_is_detected_and_skipped() -> None:
    _, body = split_header_body(STRUCTURED)
    assert is_structured_proof(body) is True
    assert mine_row("p", STRUCTURED) is None


def test_mine_row_produces_workload() -> None:
    workload = mine_row("lean_workbook_1", MULTI_STEP)
    assert workload is not None
    assert workload.problem_id == "lean_workbook_1"
    assert len(workload.tactic_units) == 3
    assert workload.source_hash


def test_build_candidate_tactics_includes_gold_dedupes_caps_and_is_seed_stable() -> None:
    pool = ["ring", "simp", "omega", "gold", "ring", "norm_num"]
    first = build_candidate_tactics("gold", pool, 3, random.Random(7))
    second = build_candidate_tactics("gold", pool, 3, random.Random(7))
    assert first == second  # seed-stable
    assert "gold" in first
    assert len(first) == 3
    assert len(set(first)) == len(first)  # deduped


def test_build_candidate_tactics_handles_small_pool() -> None:
    result = build_candidate_tactics("gold", ["gold"], 8, random.Random(0))
    assert result == ["gold"]


def test_percentile_interpolates() -> None:
    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 50) == pytest.approx(25.0)
    assert percentile([], 95) == 0.0


def test_state_store_usage_scopes_by_item_prefix(tmp_path: Path) -> None:
    (tmp_path / "st_a.bin").write_bytes(b"aaaa")
    (tmp_path / "st_a.json").write_text(json.dumps({"item_id": "bench:run1:p1"}))
    (tmp_path / "st_b.bin").write_bytes(b"bb")
    (tmp_path / "st_b.json").write_text(json.dumps({"item_id": "other:thing"}))

    scoped = state_store_usage(tmp_path, "bench:run1:")
    assert scoped == {"state_count": 1, "total_bytes": 4}
    assert state_store_usage(tmp_path)["state_count"] == 2


def test_build_report_aggregates_throughput_and_status() -> None:
    collector = MetricsCollector()
    collector.create_items = 2
    collector.created_states = 2
    collector.step_items = 3
    collector.step_results = 4
    collector.record_request("/exec/create_states", 1_000.0)
    collector.record_request("/exec/step_batch", 100.0)
    collector.record_request("/exec/step_batch", 200.0)
    collector.record_request("/exec/cleanup", 50.0)
    collector.record_status("open")
    collector.record_status("complete")

    report = build_report(
        collector,
        wall_seconds=2.0,
        cleanup_deleted_states=2,
        cleanup_deleted_bytes=512,
        rss={"peak_mb": 1234.0},
        backend_stats=None,
        state_store_before={"state_count": 0, "total_bytes": 0},
        state_store_after={"state_count": 0, "total_bytes": 0},
    )
    assert report["request_count"] == 4
    assert report["create_items"] == 2
    assert report["items_per_sec"] == 1.5
    assert report["tactics_per_sec"] == 2.0
    assert report["status_counts"] == {"open": 1, "complete": 1}
    assert report["cleanup"] == {"deleted_states": 2, "deleted_bytes": 512}
    assert report["latency_by_endpoint_ms"] == {
        "/exec/cleanup": {
            "count": 1,
            "p50": 50.0,
            "p95": 50.0,
            "p99": 50.0,
            "max": 50.0,
        },
        "/exec/create_states": {
            "count": 1,
            "p50": 1000.0,
            "p95": 1000.0,
            "p99": 1000.0,
            "max": 1000.0,
        },
        "/exec/step_batch": {
            "count": 2,
            "p50": 150.0,
            "p95": 195.0,
            "p99": 199.0,
            "max": 200.0,
        },
    }


def test_build_phase_report_keeps_create_and_step_rates_separate() -> None:
    collector = MetricsCollector()
    collector.create_items = 4
    collector.created_states = 3
    collector.step_items = 2
    collector.step_results = 16
    collector.record_request(
        "/exec/create_states",
        100.0,
        label="create_chunk_000",
        item_count=4,
    )
    collector.record_request(
        "/exec/step_batch",
        200.0,
        label="step_chunk_000",
        item_count=2,
        tactic_count=16,
    )

    report = build_phase_report(collector, wall_seconds=2.0)

    assert report["create_items_per_sec"] == 2.0
    assert report["created_states_per_sec"] == 1.5
    assert report["step_items_per_sec"] == 1.0
    assert report["tactics_per_sec"] == 8.0
    assert report["latency_by_endpoint_ms"]["/exec/create_states"]["count"] == 1
    assert report["request_details"] == [
        {
            "endpoint": "/exec/create_states",
            "label": "create_chunk_000",
            "item_count": 4,
            "tactic_count": None,
            "elapsed_ms": 100.0,
        },
        {
            "endpoint": "/exec/step_batch",
            "label": "step_chunk_000",
            "item_count": 2,
            "tactic_count": 16,
            "elapsed_ms": 200.0,
        },
    ]
    assert report["slowest_requests"][0]["label"] == "step_chunk_000"


async def test_fetch_backend_stats_reads_exec_stats() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/exec/stats"
        return httpx.Response(
            200,
            json={
                "settings": {"exec_backend": "pantograph_task"},
                "pantograph_pool": {"total_workers": 1},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        stats = await fetch_backend_stats(
            client,
            api_url="http://test",
            api_key=None,
        )

    assert stats is not None
    assert stats["settings"] == {"exec_backend": "pantograph_task"}


async def test_backend_stats_sampler_summarizes_worker_caps() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "settings": {"exec_backend": "pantograph_task"},
                "pantograph_pool": {
                    "total_workers": min(calls, 2),
                    "free_workers": 1,
                    "busy_workers": min(calls, 1),
                    "starting_workers": 0,
                    "workers_by_env_profile": {"lean4.29.1_mathlib": min(calls, 2)},
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with BackendStatsSampler(
            client,
            api_url="http://test",
            api_key=None,
            interval_seconds=0.001,
        ) as sampler:
            await asyncio.sleep(0.01)
        summary = sampler.summary()

    assert summary is not None
    assert summary["max_total_workers"] == 2
    assert summary["max_busy_workers"] == 1
    assert summary["max_workers_by_env_profile"] == {"lean4.29.1_mathlib": 2}


def test_benchmark_report_asserts_backend_cleanup_and_caps() -> None:
    args = argparse.Namespace(
        exec_backend="pantograph_task",
        max_pantograph_workers=2,
        max_lean_processes_per_env_profile=1,
        no_assert_backend=False,
        no_assert_cleanup=False,
        no_assert_worker_cap=False,
    )
    report: dict[str, object] = {
        "final_backend_stats": {
            "settings": {"exec_backend": "pantograph_task"},
        },
        "backend_stats": {
            "max_total_workers": 2,
            "max_workers_by_env_profile": {"lean4.29.1_mathlib": 1},
            "final": {"settings": {"exec_backend": "pantograph_task"}},
        },
        "state_store": {"after": {"state_count": 0, "total_bytes": 0}},
    }

    _assert_report(args, report)
    assert _observed_exec_backend(report) == "pantograph_task"

    bad_report = {
        **report,
        "backend_stats": {
            "max_total_workers": 3,
            "max_workers_by_env_profile": {"lean4.29.1_mathlib": 1},
        },
    }
    with pytest.raises(SystemExit, match="worker cap violated"):
        _assert_report(args, bad_report)


def test_workload_cache_reuses_matching_metadata(tmp_path: Path) -> None:
    cache_path = tmp_path / "workload.jsonl"
    first = build_workload(
        [("p1", MULTI_STEP)],
        dataset_name="dataset-a",
        split="train",
        n_proofs=1,
        seed=0,
        max_rows_scanned=10,
        cache_path=cache_path,
    )
    second = build_workload(
        [("p2", ONE_SHOT)],
        dataset_name="dataset-a",
        split="train",
        n_proofs=1,
        seed=0,
        max_rows_scanned=10,
        cache_path=cache_path,
    )

    assert [w.problem_id for w in first] == ["p1"]
    assert [w.problem_id for w in second] == ["p1"]


def test_workload_cache_rejects_mismatched_metadata(tmp_path: Path) -> None:
    cache_path = tmp_path / "workload.jsonl"
    build_workload(
        [("p1", MULTI_STEP)],
        dataset_name="dataset-a",
        split="train",
        n_proofs=1,
        seed=0,
        max_rows_scanned=10,
        cache_path=cache_path,
    )
    refreshed = build_workload(
        [("p2", ONE_SHOT)],
        dataset_name="dataset-b",
        split="train",
        n_proofs=1,
        seed=0,
        max_rows_scanned=10,
        cache_path=cache_path,
    )

    assert [w.problem_id for w in refreshed] == ["p2"]


def _workload() -> ProofWorkload:
    return ProofWorkload(
        problem_id="p1",
        source_hash="h",
        root_code="theorem t : a = a := by sorry",
        tactic_units=["intro x", "rfl"],
    )


def _config() -> ReplayConfig:
    return ReplayConfig(
        api_url="http://test",
        env_profile="bench",
        run_id="run1",
        concurrency=2,
        items_per_request=8,
        tactics_per_item=3,
        max_replay_depth=2,
    )


def test_freeze_step_items_makes_exact_tactic_lists_and_signature() -> None:
    workloads = [
        ProofWorkload(
            problem_id="p1",
            source_hash="h1",
            root_code="theorem p1 : True := by sorry",
            tactic_units=["trivial", "simp"],
        ),
        ProofWorkload(
            problem_id="p2",
            source_hash="h2",
            root_code="theorem p2 : True := by sorry",
            tactic_units=["simp", "trivial"],
        ),
    ]

    frozen = freeze_step_items(workloads, n_items=2, tactics_per_item=2, seed=0)

    assert [item.problem_id for item in frozen] == ["p1", "p2"]
    assert all(len(item.tactics) == 2 for item in frozen)
    assert "trivial" in frozen[0].tactics
    assert frozen_workload_signature(frozen) == frozen_workload_signature(frozen)


def test_frozen_header_group_report_uses_server_import_header() -> None:
    frozen = [
        FrozenStepItem(
            problem_id="p1",
            source_hash="h1",
            root_code="import Mathlib\n\ntheorem p1 : True := by sorry",
            tactics=["trivial"],
        ),
        FrozenStepItem(
            problem_id="p2",
            source_hash="h2",
            root_code="import Mathlib\n\ntheorem p2 : True := by sorry",
            tactics=["trivial"],
        ),
        FrozenStepItem(
            problem_id="p3",
            source_hash="h3",
            root_code="import Aesop\n\ntheorem p3 : True := by sorry",
            tactics=["trivial"],
        ),
    ]

    report = frozen_header_group_report(
        frozen,
        items_per_request=3,
        max_items_per_worker_batch=2,
    )

    assert report["unique_header_count"] == 2
    assert report["worker_group_count"] == 2
    assert report["worker_group_size_histogram"] == {"1": 1, "2": 1}
    assert report["request_groups"] == [
        {
            "request_index": 0,
            "item_count": 3,
            "group_count": 2,
            "group_sizes": [2, 1],
            "groups": [
                {
                    "header_hash": report["headers"][0]["header_hash"],
                    "header": "import Mathlib",
                    "item_count": 2,
                },
                {
                    "header_hash": report["headers"][1]["header_hash"],
                    "header": "import Aesop",
                    "item_count": 1,
                },
            ],
        }
    ]


async def test_frozen_create_and_step_submit_exact_tactics() -> None:
    frozen = [
        FrozenStepItem(
            problem_id="p1",
            source_hash="h1",
            root_code="theorem p1 : True := by sorry",
            tactics=["trivial", "simp"],
        )
    ]
    config = FrozenCompareConfig(
        api_url="http://test",
        env_profile="bench",
        run_id="run1",
        concurrency=1,
        items_per_request=16,
        tactics_per_item=2,
        timeout_ms=1000,
    )
    seen_tactics: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/exec/create_states":
            assert body["items"][0]["code"] == frozen[0].root_code
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "item_id": "bench:run1:parallel_1:p1",
                            "status": "open",
                            "states": [{"state_token": "st_root", "goals": []}],
                        }
                    ]
                },
            )
        if request.url.path == "/exec/step_batch":
            seen_tactics.extend(body["items"][0]["tactics"])
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "node_id": "bench:run1:node:p1",
                            "results": [
                                {"tactic": "trivial", "status": "complete"},
                                {"tactic": "simp", "status": "error"},
                            ],
                        }
                    ]
                },
            )
        raise AssertionError(request.url.path)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        create_collector = MetricsCollector()
        active, failures, _create_wall = await create_frozen_roots(
            client,
            frozen,
            config=config,
            mode_name="parallel_1",
            collector=create_collector,
        )
        step_collector = MetricsCollector()
        results, _step_wall = await step_frozen_roots(
            client,
            active,
            config=config,
            collector=step_collector,
        )

    assert not failures
    assert create_collector.created_states == 1
    assert create_collector.requests[0].label == "create_chunk_000"
    assert create_collector.requests[0].item_count == 1
    assert seen_tactics == frozen[0].tactics
    assert step_collector.step_results == 2
    assert step_collector.requests[0].label == "step_chunk_000"
    assert step_collector.requests[0].item_count == 1
    assert step_collector.requests[0].tactic_count == 2
    assert results[0]["results"][0]["status"] == "complete"


def test_compare_result_sets_detects_status_mismatch() -> None:
    baseline = [
        {
            "problem_id": "p1",
            "results": [{"tactic": "simp", "status": "open", "has_state_token": True}],
        }
    ]
    contender = [
        {
            "problem_id": "p1",
            "results": [{"tactic": "simp", "status": "error", "has_state_token": False}],
        }
    ]

    comparison = compare_result_sets(baseline, contender)

    assert comparison["equivalent"] is False
    assert comparison["mismatch_count"] == 1


def test_task_compare_asserts_mode_workload_signature() -> None:
    report: dict[str, object] = {
        "config": {"n_items": 1, "tactics_per_item": 1},
        "frozen_workload": {"signature": "expected"},
        "modes": [
            {
                "mode_name": "parallel_1",
                "workload_signature": "different",
                "create_phase": {"created_states": 1},
                "step_phase": {"step_results": 1},
            }
        ],
        "comparisons": {},
    }

    with pytest.raises(SystemExit, match="different frozen workload"):
        _assert_comparison(report, allow_mismatch=False)


def test_task_compare_server_log_name_is_run_specific(tmp_path: Path) -> None:
    args = argparse.Namespace(
        server_host="127.0.0.1",
        server_port=8040,
        server_start_timeout=120.0,
        server_log_dir=tmp_path,
        output=Path(".cache/pantograph_benchmark/results_task_frozen_compare_200.json"),
        exec_backend="pantograph_task",
        max_pantograph_workers=1,
        max_lean_processes_per_env_profile=1,
        pantograph_worker_startup_timeout_seconds=600,
        items_per_request=16,
        tactics_per_item=8,
        max_items_per_worker_batch=16,
        state_store_root=tmp_path / "states",
    )

    server = _server_args(args, mode=16, state_suffix="parallel_16", port_offset=2)

    assert server.server_port == 8042
    assert server.server_log_name == (
        "task_compare_results_task_frozen_compare_200_parallel_16_port_8042_par_16"
    )


async def test_run_replay_calls_create_step_cleanup_in_order() -> None:
    calls: list[str] = []
    item_id = item_id_for("run1", "p1")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path == "/exec/create_states":
            return httpx.Response(
                200,
                json={"items": [{"item_id": item_id, "status": "open",
                                  "states": [{"state_token": "st_root", "goals": []}]}]},
            )
        if path == "/exec/step_batch":
            body = json.loads(request.content)
            node_id = body["items"][0]["node_id"]
            # gold "intro x" stays open with a child so depth advances, then "rfl".
            return httpx.Response(
                200,
                json={"items": [{"node_id": node_id, "results": [
                    {"tactic": t, "status": "open", "state_token": "st_child"}
                    if t == "intro x" else {"tactic": t, "status": "error"}
                    for t in body["items"][0]["tactics"]
                ]}]},
            )
        if path == "/exec/cleanup":
            return httpx.Response(
                200,
                json={"deleted_items": [{"item_id": item_id, "deleted_states": 3,
                                         "deleted_bytes": 99}]},
            )
        raise AssertionError(path)

    collector = MetricsCollector()
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        totals = await run_replay(client, [_workload()], _config(), collector,
                                  distractor_pool([_workload()]))

    assert calls[0] == "/exec/create_states"
    assert "/exec/step_batch" in calls
    assert calls[-1] == "/exec/cleanup"
    assert totals.deleted_states == 3
    assert collector.created_states == 1
    assert collector.step_items > 0
    assert collector.step_results > 0


async def test_run_replay_cleans_up_even_when_replay_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    item_id = item_id_for("run1", "p1")

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/exec/create_states":
            return httpx.Response(
                200,
                json={"items": [{"item_id": item_id, "status": "open",
                                  "states": [{"state_token": "st_root", "goals": []}]}]},
            )
        return httpx.Response(200, json={"deleted_items": []})

    def boom(*args: object, **kwargs: object) -> list[str]:
        raise RuntimeError("candidate build failed")

    monkeypatch.setattr(replay_module, "build_candidate_tactics", boom)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError, match="candidate build failed"):
            await run_replay(client, [_workload()], _config(), MetricsCollector(),
                             distractor_pool([_workload()]))

    assert "/exec/cleanup" in calls  # cleanup ran despite the failure
