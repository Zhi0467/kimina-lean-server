"""Frozen-workload benchmark helpers for comparing step parallelism modes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import httpx

from server.split import split_snippet

from .metrics import MetricsCollector, build_phase_report, now_ms
from .mining import ProofWorkload, build_candidate_tactics, distractor_pool
from .replay import CleanupTotals


FROZEN_WORKLOAD_VERSION = 1
T = TypeVar("T")


@dataclass(frozen=True)
class FrozenStepItem:
    problem_id: str
    source_hash: str
    root_code: str
    tactics: list[str]


@dataclass(frozen=True)
class FrozenCompareConfig:
    api_url: str
    env_profile: str
    run_id: str
    concurrency: int
    items_per_request: int
    tactics_per_item: int
    timeout_ms: int
    api_key: str | None = None


@dataclass(frozen=True)
class ActiveFrozenState:
    item_id: str
    problem_id: str
    state_token: str
    tactics: list[str]


def freeze_step_items(
    workloads: list[ProofWorkload],
    *,
    n_items: int,
    tactics_per_item: int,
    seed: int,
) -> list[FrozenStepItem]:
    """Build deterministic first-step items after root validity is known."""
    selected = workloads[:n_items]
    pool = distractor_pool(selected)
    items: list[FrozenStepItem] = []
    for workload in selected:
        rng = random.Random(f"{seed}:root:{workload.problem_id}:{workload.source_hash}")
        tactics = build_candidate_tactics(
            workload.tactic_units[0],
            pool,
            tactics_per_item,
            rng,
        )
        if len(tactics) != tactics_per_item:
            raise ValueError(
                f"could only build {len(tactics)} tactics for {workload.problem_id}; "
                f"expected {tactics_per_item}"
            )
        items.append(
            FrozenStepItem(
                problem_id=workload.problem_id,
                source_hash=workload.source_hash,
                root_code=workload.root_code,
                tactics=tactics,
            )
        )
    return items


def frozen_workload_signature(items: list[FrozenStepItem]) -> str:
    payload = [
        {
            "problem_id": item.problem_id,
            "source_hash": item.source_hash,
            "root_sha256": hashlib.sha256(item.root_code.encode("utf-8")).hexdigest(),
            "tactics": item.tactics,
        }
        for item in items
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def frozen_header_group_report(
    items: list[FrozenStepItem],
    *,
    items_per_request: int,
    max_items_per_worker_batch: int,
) -> dict[str, object]:
    """Describe the server-side header grouping expected for a frozen workload."""
    chunk_size = max(max_items_per_worker_batch, 1)
    headers = [split_snippet(item.root_code).header for item in items]
    header_counts = Counter(headers)
    request_groups: list[dict[str, object]] = []
    all_group_sizes: list[int] = []
    for request_index, chunk in enumerate(_chunks(items, items_per_request)):
        grouped = Counter(split_snippet(item.root_code).header for item in chunk)
        groups: list[dict[str, object]] = []
        for header, item_count in grouped.items():
            start = 0
            while start < item_count:
                group_size = min(chunk_size, item_count - start)
                all_group_sizes.append(group_size)
                groups.append(
                    {
                        "header_hash": _header_hash(header),
                        "header": header,
                        "item_count": group_size,
                    }
                )
                start += group_size
        request_groups.append(
            {
                "request_index": request_index,
                "item_count": len(chunk),
                "group_count": len(groups),
                "group_sizes": [group["item_count"] for group in groups],
                "groups": groups,
            }
        )

    return {
        "unique_header_count": len(header_counts),
        "headers": [
            {
                "header_hash": _header_hash(header),
                "header": header,
                "item_count": count,
            }
            for header, count in header_counts.most_common()
        ],
        "http_request_count": len(request_groups),
        "worker_group_count": len(all_group_sizes),
        "worker_group_size_histogram": {
            str(size): count for size, count in sorted(Counter(all_group_sizes).items())
        },
        "request_groups": request_groups,
    }


def _header_hash(header: str) -> str:
    return hashlib.sha256(header.encode("utf-8")).hexdigest()


def frozen_metadata(
    *,
    dataset_name: str,
    split: str,
    n_items: int,
    tactics_per_item: int,
    seed: int,
) -> dict[str, object]:
    return {
        "version": FROZEN_WORKLOAD_VERSION,
        "dataset_name": dataset_name,
        "split": split,
        "n_items": n_items,
        "tactics_per_item": tactics_per_item,
        "seed": seed,
    }


def read_frozen_workload(
    path: Path,
    *,
    expected_metadata: dict[str, object],
) -> list[FrozenStepItem] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if raw.get("metadata") != expected_metadata:
        return None
    items = [
        FrozenStepItem(
            problem_id=item["problem_id"],
            source_hash=item["source_hash"],
            root_code=item["root_code"],
            tactics=list(item["tactics"]),
        )
        for item in raw.get("items", [])
    ]
    if len(items) != int(expected_metadata["n_items"]):
        return None
    if any(len(item.tactics) != int(expected_metadata["tactics_per_item"]) for item in items):
        return None
    return items


def write_frozen_workload(
    path: Path,
    *,
    metadata: dict[str, object],
    items: list[FrozenStepItem],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "metadata": metadata,
        "signature": frozen_workload_signature(items),
        "items": [
            {
                "problem_id": item.problem_id,
                "source_hash": item.source_hash,
                "root_code": item.root_code,
                "tactics": item.tactics,
            }
            for item in items
        ],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


async def select_valid_workloads(
    client: httpx.AsyncClient,
    workloads: list[ProofWorkload],
    *,
    config: FrozenCompareConfig,
    n_items: int,
) -> tuple[list[ProofWorkload], dict[str, object]]:
    """Return the first n workloads whose root state can be created."""
    selected: list[ProofWorkload] = []
    failures: list[dict[str, object]] = []
    item_ids: list[str] = []
    index = {f"validate:{config.run_id}:{w.problem_id}": w for w in workloads}
    collector = MetricsCollector()
    started = time.perf_counter()
    try:
        for chunk_index, chunk in enumerate(
            _chunks(list(index), config.items_per_request)
        ):
            if len(selected) >= n_items:
                break
            payload_items = [
                {
                    "item_id": item_id,
                    "code": index[item_id].root_code,
                    "timeout_ms": config.timeout_ms,
                }
                for item_id in chunk
            ]
            collector.create_items += len(payload_items)
            response = await _post(
                client,
                "/exec/create_states",
                {
                    "env_profile": config.env_profile,
                    "items": payload_items,
                },
                config,
                collector,
                label=f"validate_create_chunk_{chunk_index:03d}",
                item_count=len(payload_items),
            )
            for result in (response or {}).get("items", []):
                status = result["status"]
                collector.record_status(status)
                states = result.get("states", [])
                item_id = result["item_id"]
                item_ids.append(item_id)
                if status == "open" and states:
                    collector.created_states += 1
                    if len(selected) < n_items:
                        selected.append(index[item_id])
                else:
                    failures.append(
                        {
                            "problem_id": index[item_id].problem_id,
                            "status": status,
                            "messages": result.get("messages", []),
                        }
                    )
        if len(selected) < n_items:
            raise RuntimeError(
                f"only selected {len(selected)} valid roots; need {n_items}"
            )
    finally:
        cleanup = await cleanup_items(client, item_ids, config, MetricsCollector())
    wall_seconds = time.perf_counter() - started
    report = build_phase_report(collector, wall_seconds=wall_seconds)
    report["candidate_count"] = len(workloads)
    report["selected_count"] = len(selected)
    report["failure_count"] = len(failures)
    report["failures_sample"] = failures[:20]
    report["cleanup"] = {
        "deleted_states": cleanup.deleted_states,
        "deleted_bytes": cleanup.deleted_bytes,
    }
    return selected, report


async def create_frozen_roots(
    client: httpx.AsyncClient,
    items: list[FrozenStepItem],
    *,
    config: FrozenCompareConfig,
    mode_name: str,
    collector: MetricsCollector,
) -> tuple[list[ActiveFrozenState], list[dict[str, object]], float]:
    payloads = []
    for index, chunk in enumerate(_chunks(items, config.items_per_request)):
        payloads.append(
            {
                "env_profile": config.env_profile,
                "items": [
                    {
                        "item_id": item_id_for(config.run_id, mode_name, item.problem_id),
                        "code": item.root_code,
                        "timeout_ms": config.timeout_ms,
                    }
                    for item in chunk
                ],
                "_chunk_index": index,
                "_label": f"create_chunk_{index:03d}",
                "_item_count": len(chunk),
            }
        )
    collector.create_items += len(items)
    started = time.perf_counter()
    responses = await _fan_out_payloads(
        client,
        "/exec/create_states",
        payloads,
        config,
        collector,
    )
    wall_seconds = time.perf_counter() - started

    by_problem = {item.problem_id: item for item in items}
    active_by_problem: dict[str, ActiveFrozenState] = {}
    failures: list[dict[str, object]] = []
    for response in responses:
        for result in (response or {}).get("items", []):
            status = result["status"]
            collector.record_status(status)
            item_id = result["item_id"]
            problem_id = item_id.rsplit(":", 1)[-1]
            states = result.get("states", [])
            if status == "open" and states:
                collector.created_states += 1
                frozen = by_problem[problem_id]
                active_by_problem[problem_id] = ActiveFrozenState(
                    item_id=item_id,
                    problem_id=problem_id,
                    state_token=states[0]["state_token"],
                    tactics=frozen.tactics,
                )
            else:
                failures.append(
                    {
                        "problem_id": problem_id,
                        "status": status,
                        "messages": result.get("messages", []),
                    }
                )

    active = [
        active_by_problem[item.problem_id]
        for item in items
        if item.problem_id in active_by_problem
    ]
    return active, failures, wall_seconds


async def step_frozen_roots(
    client: httpx.AsyncClient,
    active: list[ActiveFrozenState],
    *,
    config: FrozenCompareConfig,
    collector: MetricsCollector,
) -> tuple[list[dict[str, object]], float]:
    payloads = []
    for index, chunk in enumerate(_chunks(active, config.items_per_request)):
        payloads.append(
            {
                "items": [
                    {
                        "node_id": node_id_for(config.run_id, state.problem_id),
                        "state_token": state.state_token,
                        "tactics": state.tactics,
                        "timeout_ms": config.timeout_ms,
                    }
                    for state in chunk
                ],
                "_label": f"step_chunk_{index:03d}",
                "_item_count": len(chunk),
                "_tactic_count": sum(len(state.tactics) for state in chunk),
            }
        )
    collector.step_items += len(active)
    started = time.perf_counter()
    responses = await _fan_out_payloads(
        client,
        "/exec/step_batch",
        payloads,
        config,
        collector,
    )
    wall_seconds = time.perf_counter() - started

    results_by_problem: dict[str, dict[str, object]] = {}
    for response in responses:
        for batch in (response or {}).get("items", []):
            problem_id = batch["node_id"].rsplit(":", 1)[-1]
            tactic_results = []
            for result in batch.get("results", []):
                collector.record_status(result["status"])
                collector.step_results += 1
                tactic_results.append(
                    {
                        "tactic": result["tactic"],
                        "status": result["status"],
                        "has_state_token": bool(result.get("state_token")),
                        "messages": result.get("messages", []),
                    }
                )
            results_by_problem[problem_id] = {
                "problem_id": problem_id,
                "results": tactic_results,
            }
    ordered = [
        results_by_problem[state.problem_id]
        for state in active
        if state.problem_id in results_by_problem
    ]
    return ordered, wall_seconds


async def cleanup_items(
    client: httpx.AsyncClient,
    item_ids: list[str],
    config: FrozenCompareConfig,
    collector: MetricsCollector,
) -> CleanupTotals:
    if not item_ids:
        return CleanupTotals()
    responses = await _fan_out_payloads(
        client,
        "/exec/cleanup",
        [
            {
                "item_ids": chunk,
                "_label": f"cleanup_chunk_{index:03d}",
                "_item_count": len(chunk),
            }
            for index, chunk in enumerate(_chunks(item_ids, config.items_per_request))
        ],
        config,
        collector,
    )
    totals = CleanupTotals()
    for response in responses:
        for deleted in (response or {}).get("deleted_items", []):
            totals.deleted_states += int(deleted.get("deleted_states", 0))
            totals.deleted_bytes += int(deleted.get("deleted_bytes", 0))
    return totals


def result_signature(results: list[dict[str, object]]) -> str:
    normalized = [
        {
            "problem_id": item["problem_id"],
            "results": [
                {
                    "tactic": result["tactic"],
                    "status": result["status"],
                    "has_state_token": result["has_state_token"],
                }
                for result in item["results"]
            ],
        }
        for item in results
    ]
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def compare_result_sets(
    baseline: list[dict[str, object]],
    contender: list[dict[str, object]],
) -> dict[str, object]:
    baseline_by_problem = {str(item["problem_id"]): item for item in baseline}
    contender_by_problem = {str(item["problem_id"]): item for item in contender}
    missing = sorted(set(baseline_by_problem) - set(contender_by_problem))
    extra = sorted(set(contender_by_problem) - set(baseline_by_problem))
    mismatches: list[dict[str, object]] = []
    for problem_id in sorted(set(baseline_by_problem) & set(contender_by_problem)):
        base_results = baseline_by_problem[problem_id]["results"]
        other_results = contender_by_problem[problem_id]["results"]
        if _compact_results(base_results) != _compact_results(other_results):
            mismatches.append(
                {
                    "problem_id": problem_id,
                    "baseline": _compact_results(base_results),
                    "contender": _compact_results(other_results),
                }
            )
    return {
        "equivalent": not missing and not extra and not mismatches,
        "missing_count": len(missing),
        "extra_count": len(extra),
        "mismatch_count": len(mismatches),
        "missing_sample": missing[:20],
        "extra_sample": extra[:20],
        "mismatch_sample": mismatches[:10],
    }


def item_id_for(run_id: str, mode_name: str, problem_id: str) -> str:
    return f"bench:{run_id}:{mode_name}:{problem_id}"


def node_id_for(run_id: str, problem_id: str) -> str:
    return f"bench:{run_id}:node:{problem_id}"


async def _fan_out_payloads(
    client: httpx.AsyncClient,
    path: str,
    payloads: list[dict[str, object]],
    config: FrozenCompareConfig,
    collector: MetricsCollector,
) -> list[dict[str, object] | None]:
    semaphore = asyncio.Semaphore(config.concurrency)

    async def one(payload: dict[str, object]) -> dict[str, object] | None:
        label = payload.get("_label")
        item_count = payload.get("_item_count")
        tactic_count = payload.get("_tactic_count")
        payload = {
            key: value for key, value in payload.items() if not key.startswith("_")
        }
        async with semaphore:
            return await _post(
                client,
                path,
                payload,
                config,
                collector,
                label=str(label) if label is not None else None,
                item_count=int(item_count) if item_count is not None else None,
                tactic_count=int(tactic_count) if tactic_count is not None else None,
            )

    return await asyncio.gather(*(one(payload) for payload in payloads))


async def _post(
    client: httpx.AsyncClient,
    path: str,
    payload: dict[str, object],
    config: FrozenCompareConfig,
    collector: MetricsCollector,
    *,
    label: str | None = None,
    item_count: int | None = None,
    tactic_count: int | None = None,
) -> dict[str, object] | None:
    headers = {"Authorization": config.api_key} if config.api_key else None
    start = now_ms()
    try:
        response = await client.post(
            f"{config.api_url.rstrip('/')}{path}",
            json=payload,
            headers=headers,
        )
    except httpx.HTTPError as exc:
        collector.record_request(
            path,
            now_ms() - start,
            label=label,
            item_count=item_count,
            tactic_count=tactic_count,
        )
        collector.record_status(f"http_error:{type(exc).__name__}")
        return None
    collector.record_request(
        path,
        now_ms() - start,
        label=label,
        item_count=item_count,
        tactic_count=tactic_count,
    )
    if response.status_code != 200:
        collector.record_status(f"http_{response.status_code}")
        return None
    return dict(response.json())


def _chunks(items: list[T], size: int) -> list[list[T]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _compact_results(results: object) -> list[dict[str, object]]:
    if not isinstance(results, list):
        return []
    return [
        {
            "tactic": str(result.get("tactic")),
            "status": str(result.get("status")),
            "has_state_token": bool(result.get("has_state_token")),
        }
        for result in results
        if isinstance(result, dict)
    ]
