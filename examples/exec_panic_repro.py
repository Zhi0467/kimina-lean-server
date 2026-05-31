"""Benchmark-shaped reproduction: 8 candidate tactics per step_batch item (gold +
distractors, via the benchmark's own build_candidate_tactics), BFS by depth,
concurrency 1, full per-result capture. Goal: trigger the panic and show exactly
what every result of the panicking request looks like.
"""
from __future__ import annotations
import json, random, sys
import httpx
sys.path.insert(0, ".")
from examples.pantograph_benchmark.mining import build_candidate_tactics  # noqa

API = "http://localhost:8000"
WORKLOAD = ".cache/pantograph_benchmark/goedel_workload_200.jsonl"
N_PROOFS = 16
TACTICS_PER_ITEM = 8
MAX_DEPTH = 3
MAX_NODES_PER_PROOF = 12
TIMEOUT_MS = 20000


def load(path, n):
    out = []
    for line in open(path):
        d = json.loads(line)
        if d.get("root_code") and d.get("tactic_units"):
            out.append(d)
        if len(out) >= n:
            break
    return out


def main():
    rng = random.Random(7)
    proofs = load(WORKLOAD, N_PROOFS)
    pool = []
    seen = set()
    for p in proofs:
        for t in p["tactic_units"]:
            if t not in seen:
                seen.add(t); pool.append(t)
    records = []
    req_idx = 0
    client = httpx.Client(timeout=400.0)
    for p in proofs:
        pid = p["problem_id"]
        own = list(p["tactic_units"])
        create = client.post(f"{API}/exec/create_states", json={
            "env_profile": "lean4.29.1_mathlib",
            "items": [{"item_id": f"p3:{pid}", "code": p["root_code"], "timeout_ms": TIMEOUT_MS}]}).json()
        item = create["items"][0]
        if item.get("status") != "open" or not item.get("states"):
            continue
        queue = [(item["states"][0]["state_token"], 0)]
        nodes = 0
        while queue and nodes < MAX_NODES_PER_PROOF:
            token, depth = queue.pop(0)
            nodes += 1
            gold = own[min(depth, len(own) - 1)]
            tactics = build_candidate_tactics(gold, pool, TACTICS_PER_ITEM, rng)
            resp = client.post(f"{API}/exec/step_batch", json={"items": [
                {"node_id": f"{pid}:{req_idx}", "state_token": token,
                 "tactics": tactics, "timeout_ms": TIMEOUT_MS}]}).json()
            rs = resp["items"][0]["results"]
            results = []
            for r in rs:
                g = (r.get("goals") or [""])[0]
                results.append({"status": r["status"], "has_token": bool(r.get("state_token")),
                                "goal0": g[:60], "corrupt": "�" in g})
                if r["status"] == "open" and r.get("state_token") and depth + 1 < MAX_DEPTH:
                    queue.append((r["state_token"], depth + 1))
            records.append({"req_idx": req_idx, "pid": pid, "depth": depth, "results": results})
            req_idx += 1
    json.dump(records, open("/tmp/panic_probe3_records.json", "w"))
    from collections import Counter
    sc = Counter(r["status"] for rec in records for r in rec["results"])
    corrupt = sum(1 for rec in records for r in rec["results"] if r["corrupt"])
    print(f"requests={len(records)} total_results={sum(len(r['results']) for r in records)}")
    print("status counts:", dict(sc))
    print("results with replacement-char corruption:", corrupt)


if __name__ == "__main__":
    main()
