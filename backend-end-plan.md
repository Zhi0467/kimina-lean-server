# Env Backend Plan: Kimina + Pantograph

This plan is the concrete backend work implied by the LeanFoundry system
design. It targets the `kimina-lean-server` repo first. LeanFoundry /
training code should call this service over HTTP; the full SLIME rollout and
search orchestration live outside this backend.

## Decisions From Profiling And Design Discussion

1. Use Kimina as the server and worker-pool base.
   - Reuse FastAPI routing, settings, batch request shape, free/busy manager
     semantics, exact-header reuse, idle LRU eviction, timeout handling, and
     `/check`/verification patterns.
   - Do not rewrite a new Lean service from scratch.

2. Use Pantograph as the tactic worker.
   - Pantograph already exposes `goal_start`, `load_sorry`, `goal_tactic`,
     `goal_save`, and `goal_load`.
   - Raw Pantograph `state_id` is process-local and must never be exposed as a
     public state handle.

3. The scheduler unit is a proof-state expansion item, not a tactic.
   - One item contains `state_token + tactics[]`.
   - One item leases one worker.
   - The worker loads the base state once, applies all tactics internally, and
     returns one result per tactic.
   - Never flatten `tactics[]` into separate manager jobs. Doing so would make
     Kimina cold-start duplicate compatible workers when the first matching
     worker becomes busy.

4. Batch across proof states / proof graphs.
   - A single `/exec/step_batch` request may contain many independent items.
   - Backend parallelism is item-level, like Kimina `/check` parallelism over
     independent whole-code checks.
   - Tactic candidates inside one item are sequential in the v0 backend.

5. Use opaque state tokens backed by local tmp/shared state files.
   - The public API carries `state_token`, not a blob and not a raw path.
   - In single-node mode, `state_token` resolves to a Pantograph state file on
     local storage, preferably tmpfs such as `/dev/shm`.
   - Blob export/import can be added later for multi-node or checkpointing, but
     it is not the normal v0 step protocol.

## API

### POST `/exec/init_batch`

Create initial state tokens from goals or sorry blocks.

Request:

```json
{
  "env_profile": "lean4.29.1_mathlib_5e932f97",
  "header": "import Mathlib",
  "items": [
    {
      "item_id": "thm_001",
      "kind": "goal",
      "expr": "∀ (n : Nat), n + 0 = n"
    },
    {
      "item_id": "thm_002",
      "kind": "sorry_block",
      "code": "theorem t : P := by\n  sorry"
    }
  ]
}
```

Response:

```json
{
  "items": [
    {
      "item_id": "thm_001",
      "status": "ok",
      "state_tokens": ["state:v1:..."],
      "goals": [["⊢ ∀ (n : Nat), n + 0 = n"]]
    }
  ]
}
```

For `sorry_block`, one input may return multiple state tokens, one per search
target/sorry state.

### POST `/exec/step_batch`

Step many proof-state expansion items.

Request:

```json
{
  "env_profile": "lean4.29.1_mathlib_5e932f97",
  "header_hash": "a3f9...",
  "items": [
    {
      "node_id": "graph7:n123",
      "state_token": "state:v1:a3f9:8cb2...",
      "tactics": ["simp", "rw [Nat.add_comm]", "nlinarith"],
      "timeout_ms": 5000
    }
  ]
}
```

Response:

```json
{
  "items": [
    {
      "node_id": "graph7:n123",
      "results": [
        {
          "tactic": "simp",
          "status": "incomplete",
          "next_state_token": "state:v1:a3f9:91de...",
          "goals": ["⊢ Q"],
          "messages": [],
          "elapsed_ms": 41
        },
        {
          "tactic": "nlinarith",
          "status": "error",
          "messages": ["nlinarith failed to find a contradiction"],
          "elapsed_ms": 12
        }
      ]
    }
  ],
  "server_metrics": {
    "queue_wait_ms_p50": 3,
    "queue_wait_ms_p95": 31,
    "worker_utilization": 0.91,
    "state_store_bytes": 123456789
  }
}
```

`node_id` is caller bookkeeping only. The backend returns it unchanged and
does not interpret it as a Lean or Pantograph state id.

### `/verify`

Keep Kimina's existing whole-file verification path. Final accepted proofs
must pass strict verification with `sorry`/`sorryAx` and unauthorized axioms
rejected.

## Implementation In This Repo

Proposed files:

```text
server/pantograph_worker.py
  PantographWorker wrapper around pantograph.Server.

server/state_store.py
  Opaque state_token -> local Pantograph state file.
  Metadata, TTL, size accounting, GC.

server/pantograph_manager.py
  Kimina-style Manager variant for PantographWorker.
  Same free/busy structure and exact-header reuse semantics.

server/routers/exec.py
  /exec/init_batch and /exec/step_batch.

server/schemas_exec.py
  Pydantic request/response models for init and step.

tests/test_exec_api.py
  API-level tests with small Init goals.

tests/test_state_store.py
  token resolution, TTL, size accounting, GC.

tests/test_pantograph_worker.py
  goal_start, goal_tactic, save/load, cross-worker load.
```

Keep the existing Kimina `Manager` and `Repl` intact at first. The Pantograph
manager can duplicate the small amount of pool logic, then be refactored into a
generic pool only after behavior is validated.

## Worker Details

`PantographWorker` owns one Pantograph server process.

Required methods:

```python
class PantographWorker:
    env_profile: str
    header: str
    header_hash: str

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def init_goal(self, expr: str) -> InitResult: ...
    async def init_sorry_block(self, code: str) -> list[InitResult]: ...
    async def step_many(self, state_path: str, tactics: list[str]) -> list[StepResult]: ...
```

`step_many` procedure:

1. `goal_load(state_path)` once.
2. For each tactic:
   - apply `goal_tactic(base_state, tactic)`;
   - on success, `goal_save(child_state, next_path)`;
   - return a new `state_token` plus pretty goals;
   - on failure, return structured error/messages.
3. Delete in-process goal states after the item.

The base state must remain reusable for every tactic candidate. Each tactic is
an alternative child of the same parent, not a sequential continuation of the
previous tactic candidate.

## Manager Semantics

The Pantograph manager should mirror Kimina:

```text
_free: list[PantographWorker]
_busy: set[PantographWorker]
max_workers: configured cap
```

Acquisition:

1. If a free worker matches `env_profile + header_hash`, lease it.
2. Else if total workers `< max_workers`, create/start/prep a worker.
3. Else if a free worker exists, evict the oldest free worker and replace it.
4. Else wait until a worker is released or timeout.

Release:

1. Move worker from `_busy` to `_free`.
2. Update `last_used_at`.
3. Notify waiters.
4. If worker is exhausted or over memory limit, close instead of returning it.

No in-request fanout policy belongs in the manager.

## Env Client / SLIME Boundary

The backend should accept batch requests but not decide how theorem searches
are coordinated. That belongs in LeanFoundry's SLIME custom rollout.

Expected caller flow:

```text
SLIME custom rollout
  -> run many proof graph searches
  -> each selected node samples tactics via SLIME/SGLang
  -> EnvClient collects proof-state expansion items
  -> EnvClient sends /exec/step_batch with many items
  -> backend executes items across Kimina/Pantograph worker pool
```

The EnvClient microbatcher batches proof-state expansion items, not tactics:

```python
await env.step(node_id=node.id, state_token=node.state_token, tactics=tactics)
```

Internally the client may wait a few milliseconds or until `max_items_per_batch`
to form one HTTP request.

## Profiling Baseline

Local profiling against Pantograph with Mathlib-compatible imports found:

```text
16 warm Pantograph workers with Mathlib.Data.Finset.Card + Mathlib.Data.Nat.Basic:
  total RSS about 20.5GB, about 1.3GB per worker

Grouped one-worker stepping of 16 tactics:
  92KB state:   ~64ms
  578KB state:  ~329ms
  1.4MB state:  ~827ms

Split shared-path stepping across 16 warm workers:
  92KB state:   ~24ms
  578KB state:  ~86ms
  1.4MB state:  ~251ms
```

These numbers show warm in-request fanout can be faster, but it is not the v0
scheduling model because cold-starting duplicate compatible workers is too
expensive and memory-heavy. v0 uses item-level concurrency across proof graphs.

State size grows much faster than printed proof-state text:

```text
100 hypotheses: raw state about 1.4MB
400 hypotheses: raw state about 11.6MB
800 hypotheses: raw state about 38.9MB
```

So state-store TTL/GC and per-token size metrics are required from the start.

## Milestones

### M0: Pantograph Viability In Server

- Add Pantograph dependency pinned to the tested version/commit.
- Add `PantographWorker`.
- Prove cross-worker `goal_save` / `goal_load` works in a unit test.
- Verify simple `Init` goals and at least one Mathlib import.

### M1: Local State Store

- Implement token generation and metadata records.
- Save/load Pantograph states by token.
- Add TTL cleanup and total-byte accounting.
- Add debug metrics.

### M2: `/exec/init_batch`

- Support `kind="goal"` via `goal_start`.
- Support `kind="sorry_block"` via Pantograph `load_sorry`.
- Return state tokens and pretty goals.

### M3: `/exec/step_batch`

- Batch item execution with one worker lease per item.
- Worker applies all tactic candidates for that item internally.
- Return per-tactic child state tokens or structured failures.
- Preserve input order in response.

### M4: Kimina Compatibility And Verification

- Keep existing `/check` or `/verify` behavior passing.
- Add final-proof verification guardrails for no `sorry`.
- Add integration test: initialize state, step tactic, render proof, verify.

### M5: Throughput Harness

- Benchmark item-level batch sizes: 1, 4, 8, 16, 32 items.
- Benchmark worker pool sizes: 1, 2, 4, 8, 16, 32 workers.
- Record queue wait, goal_load, tactic loop, goal_save, state bytes, RSS/PSS.
- Produce a config recommendation for one local machine.

## Non-Goals For v0

- Multi-node state movement.
- Public blob-per-step protocol.
- Tactic-level manager jobs.
- Session-pinned search state.
- In-request worker fanout for one proof-state item.
- Graph state canonicalization or merge correctness.

## Open Implementation Details

These are engineering details, not architecture blockers:

1. Exact Pantograph dependency form: PyPI pin vs git commit pin.
2. State-store directory default on macOS vs Linux (`/tmp` vs `/dev/shm`).
3. Whether to keep a separate `pantograph_manager.py` permanently or refactor
   Kimina's manager into a generic worker-pool abstraction after M3.
4. The minimum Mathlib import set for CI-compatible integration tests.
