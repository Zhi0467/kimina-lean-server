"""Concurrent replay of mined workloads against the `/exec` HTTP API.

Drives create_states -> step_batch (BFS by replay depth) -> cleanup, keeping up to
``concurrency`` requests in flight so the run measures server throughput under load.
Cleanup always runs, even if the run aborts.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

import httpx

from .metrics import MetricsCollector, now_ms
from .mining import ProofWorkload, build_candidate_tactics


@dataclass
class ReplayConfig:
    api_url: str
    env_profile: str
    run_id: str
    concurrency: int = 4
    items_per_request: int = 8
    tactics_per_item: int = 8
    max_replay_depth: int = 3
    timeout_ms: int = 30000
    api_key: str | None = None
    seed: int = 0


@dataclass
class CleanupTotals:
    deleted_states: int = 0
    deleted_bytes: int = 0


@dataclass
class _ActiveState:
    item_id: str
    problem_id: str
    state_token: str
    gold_index: int


@dataclass
class _Workloads:
    by_item_id: dict[str, ProofWorkload] = field(default_factory=dict)


def item_id_for(run_id: str, problem_id: str) -> str:
    return f"bench:{run_id}:{problem_id}"


async def run_replay(
    client: httpx.AsyncClient,
    workloads: list[ProofWorkload],
    config: ReplayConfig,
    collector: MetricsCollector,
    distractor_pool: list[str],
) -> CleanupTotals:
    """Run the replay and always clean up the benchmark's item_ids afterward."""
    index = _Workloads(
        by_item_id={item_id_for(config.run_id, w.problem_id): w for w in workloads}
    )
    item_ids = list(index.by_item_id)
    semaphore = asyncio.Semaphore(config.concurrency)
    try:
        active = await _create_roots(client, index, config, collector, semaphore)
        depth = 0
        while active and depth < config.max_replay_depth:
            active = await _step_once(
                client, index, config, collector, semaphore, active, depth, distractor_pool
            )
            depth += 1
    finally:
        totals = await _cleanup(client, item_ids, config, collector, semaphore)
    return totals


async def _create_roots(
    client: httpx.AsyncClient,
    index: _Workloads,
    config: ReplayConfig,
    collector: MetricsCollector,
    semaphore: asyncio.Semaphore,
) -> list[_ActiveState]:
    items = [
        {"item_id": item_id, "code": workload.root_code, "timeout_ms": config.timeout_ms}
        for item_id, workload in index.by_item_id.items()
    ]
    collector.create_items += len(items)
    responses = await _fan_out(
        client,
        "/exec/create_states",
        [
            {"env_profile": config.env_profile, "items": chunk}
            for chunk in _chunks(items, config.items_per_request)
        ],
        config,
        collector,
        semaphore,
    )

    active: list[_ActiveState] = []
    for response in responses:
        for result in (response or {}).get("items", []):
            collector.record_status(result["status"])
            states = result.get("states", [])
            if result["status"] == "open" and states:
                collector.created_states += 1
                workload = index.by_item_id[result["item_id"]]
                active.append(
                    _ActiveState(
                        item_id=result["item_id"],
                        problem_id=workload.problem_id,
                        state_token=states[0]["state_token"],
                        gold_index=0,
                    )
                )
    return active


async def _step_once(
    client: httpx.AsyncClient,
    index: _Workloads,
    config: ReplayConfig,
    collector: MetricsCollector,
    semaphore: asyncio.Semaphore,
    active: list[_ActiveState],
    depth: int,
    distractor_pool: list[str],
) -> list[_ActiveState]:
    rng = random.Random(f"{config.seed}:{depth}")
    node_to_state = {f"{state.item_id}:d{depth}": state for state in active}
    items = []
    gold_by_node: dict[str, str] = {}
    for node_id, state in node_to_state.items():
        workload = index.by_item_id[state.item_id]
        gold = workload.tactic_units[state.gold_index]
        gold_by_node[node_id] = gold
        items.append(
            {
                "node_id": node_id,
                "state_token": state.state_token,
                "tactics": build_candidate_tactics(
                    gold, distractor_pool, config.tactics_per_item, rng
                ),
                "timeout_ms": config.timeout_ms,
            }
        )

    collector.step_items += len(items)
    responses = await _fan_out(
        client,
        "/exec/step_batch",
        [{"items": chunk} for chunk in _chunks(items, config.items_per_request)],
        config,
        collector,
        semaphore,
    )

    next_active: list[_ActiveState] = []
    for response in responses:
        for batch in (response or {}).get("items", []):
            node_id = batch["node_id"]
            state = node_to_state[node_id]
            workload = index.by_item_id[state.item_id]
            gold = gold_by_node[node_id]
            for result in batch.get("results", []):
                collector.record_status(result["status"])
                collector.step_results += 1
            gold_result = next(
                (r for r in batch.get("results", []) if r["tactic"] == gold), None
            )
            if (
                gold_result is not None
                and gold_result["status"] == "open"
                and gold_result.get("state_token")
                and state.gold_index + 1 < len(workload.tactic_units)
            ):
                next_active.append(
                    _ActiveState(
                        item_id=state.item_id,
                        problem_id=state.problem_id,
                        state_token=gold_result["state_token"],
                        gold_index=state.gold_index + 1,
                    )
                )
    return next_active


async def _cleanup(
    client: httpx.AsyncClient,
    item_ids: list[str],
    config: ReplayConfig,
    collector: MetricsCollector,
    semaphore: asyncio.Semaphore,
) -> CleanupTotals:
    if not item_ids:
        return CleanupTotals()
    responses = await _fan_out(
        client,
        "/exec/cleanup",
        [{"item_ids": chunk} for chunk in _chunks(item_ids, config.items_per_request)],
        config,
        collector,
        semaphore,
    )
    totals = CleanupTotals()
    for response in responses:
        for deleted in (response or {}).get("deleted_items", []):
            totals.deleted_states += deleted.get("deleted_states", 0)
            totals.deleted_bytes += deleted.get("deleted_bytes", 0)
    return totals


async def _fan_out(
    client: httpx.AsyncClient,
    path: str,
    payloads: list[dict],
    config: ReplayConfig,
    collector: MetricsCollector,
    semaphore: asyncio.Semaphore,
) -> list[dict | None]:
    async def one(payload: dict) -> dict | None:
        async with semaphore:
            return await _post(client, path, payload, config, collector)

    return await asyncio.gather(*(one(payload) for payload in payloads))


async def _post(
    client: httpx.AsyncClient,
    path: str,
    payload: dict,
    config: ReplayConfig,
    collector: MetricsCollector,
) -> dict | None:
    headers = {"Authorization": config.api_key} if config.api_key else None
    start = now_ms()
    try:
        response = await client.post(
            f"{config.api_url}{path}", json=payload, headers=headers
        )
    except httpx.HTTPError as exc:
        collector.record_request(path, now_ms() - start)
        collector.record_status(f"http_error:{type(exc).__name__}")
        return None
    collector.record_request(path, now_ms() - start)
    if response.status_code != 200:
        collector.record_status(f"http_{response.status_code}")
        return None
    return response.json()


def _chunks(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]
