from __future__ import annotations

import json
import random
from pathlib import Path

import httpx
import pytest

from examples.pantograph_benchmark import replay as replay_module
from examples.pantograph_benchmark.metrics import (
    MetricsCollector,
    build_report,
    percentile,
    state_store_usage,
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
from examples.pantograph_step_benchmark import _verdict, _workload_analysis

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
    collector.record_microbatch("/exec/create_states", item_count=2)
    collector.record_microbatch("/exec/step_batch", item_count=3, tactic_count=12)
    collector.record_microbatch("/exec/cleanup", item_count=2)
    collector.record_status("open")
    collector.record_status("complete")

    report = build_report(
        collector,
        wall_seconds=2.0,
        cleanup_deleted_states=2,
        cleanup_deleted_bytes=512,
        rss={"peak_mb": 1234.0},
        state_store_before={"state_count": 0, "total_bytes": 0},
        state_store_after={"state_count": 0, "total_bytes": 0},
        git_sha="abc123",
        backend_config={"max_pantograph_workers": 4},
        workload_shape={"n_proofs": 2},
        exec_limits={"recommended_items_per_step_batch": 16},
        exec_stats_before={"state_store": {"state_count": 0}},
        exec_stats_after={"state_store": {"state_count": 0}},
        verdict={"success": True},
    )
    assert report["git_sha"] == "abc123"
    assert report["phase"] == "phase6"
    assert report["backend_config"] == {"max_pantograph_workers": 4}
    assert report["workload_shape"] == {"n_proofs": 2}
    assert report["request_count"] == 4
    assert report["create_items"] == 2
    assert report["items_per_sec"] == 1.5
    assert report["tactics_per_sec"] == 2.0
    assert report["status_counts"] == {"open": 1, "complete": 1}
    assert report["cleanup"] == {"deleted_states": 2, "deleted_bytes": 512}
    assert report["exec_limits"] == {"recommended_items_per_step_batch": 16}
    assert report["exec_stats"] == {
        "before": {"state_store": {"state_count": 0}},
        "after": {"state_store": {"state_count": 0}},
    }
    assert report["verdict"] == {"success": True}
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
    assert report["microbatches"] == {
        "/exec/cleanup": {
            "count": 1,
            "total_items": 2,
            "total_tactics": 0,
            "item_count": {"min": 2.0, "p50": 2.0, "max": 2.0},
            "tactic_count": {"min": 0.0, "p50": 0.0, "max": 0.0},
            "ids_sample": ["exec_cleanup:0"],
        },
        "/exec/create_states": {
            "count": 1,
            "total_items": 2,
            "total_tactics": 0,
            "item_count": {"min": 2.0, "p50": 2.0, "max": 2.0},
            "tactic_count": {"min": 0.0, "p50": 0.0, "max": 0.0},
            "ids_sample": ["exec_create_states:0"],
        },
        "/exec/step_batch": {
            "count": 1,
            "total_items": 3,
            "total_tactics": 12,
            "item_count": {"min": 3.0, "p50": 3.0, "max": 3.0},
            "tactic_count": {"min": 12.0, "p50": 12.0, "max": 12.0},
            "ids_sample": ["exec_step_batch:0"],
        },
    }


def test_workload_analysis_reports_header_groups_lanes_and_microbatches() -> None:
    workloads = [
        ProofWorkload(
            problem_id=f"p{index}",
            source_hash=f"h{index}",
            root_code="import Mathlib\n\ntheorem t : True := by sorry",
            tactic_units=["trivial"],
        )
        for index in range(5)
    ]

    analysis = _workload_analysis(
        workloads,
        items_per_request=2,
        max_lanes_per_group=3,
    )

    assert analysis["header_groups"]["count"] == 1
    assert analysis["header_groups"]["sizes"] == [5]
    assert analysis["planned_step_lanes"] == {
        "max_lanes_per_group": 3,
        "lane_count": 3,
        "items_per_lane": [2, 2, 1],
    }
    assert analysis["planned_microbatches"] == {
        "create": 3,
        "step_per_depth": 3,
        "cleanup": 3,
    }


def test_benchmark_verdict_requires_some_lean_work() -> None:
    collector = MetricsCollector()
    collector.record_status("overloaded")

    no_work = _verdict(
        collector,
        state_store_before={"state_count": 0, "total_bytes": 0},
        state_store_after={"state_count": 0, "total_bytes": 0},
    )
    assert no_work["success"] is False
    assert no_work["ran_lean_work"] is False

    collector.record_status("open")
    success = _verdict(
        collector,
        state_store_before={"state_count": 0, "total_bytes": 0},
        state_store_after={"state_count": 0, "total_bytes": 0},
    )
    assert success["success"] is True
    assert success["overloaded"] == 1


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
