# Env Backend Plan: Production Bounded Pantograph Backend

This plan is the mainline backend work for `kimina-lean-server`.

The backend is an execution service. It creates executable Lean proof states,
steps them with candidate tactics, returns child state tokens, and deletes saved
states when the caller says a proof-search attempt is done. Search, policy
calls, value calls, graph logic, rollout extraction, proof selection, and
training live outside this repo.

Status as of 2026-05-30:

- `main` uses the old-command Pantograph path as the production direction:
  `goal_load` + sequential `goal_tactic` attempts inside each worker.
- Parallelism is item-level through a bounded Pantograph process pool.
- The in-process `pantograph_task` / `goal.step_batch` work is research only
  and lives off the mainline branch. Do not use it as the production plan unless
  we explicitly reopen that research direction.
- Deployment target is one node, one FastAPI server process, one in-process
  `PantographManager`, and multiple bounded Pantograph/Lean worker processes
  managed by that manager.

The current production question is no longer "can one Lean process run 16 items
concurrently?" It is:

```text
Can a bounded pool of old-command Pantograph workers provide correct,
recoverable, observable, and load-controlled env stepping for RL/search?
```

## Production Shape

The runtime stack is:

```text
Trainer
  -> SearchEngine
  -> EnvClient
  -> EnvClient Batcher
  -> HTTP /exec route
  -> Server Exec Scheduler
  -> StateStore + PantographManager
  -> PantographWorker
  -> Pantograph/Lean process
```

Responsibilities:

1. Trainer
   - Owns training loop, optimizer steps, checkpoints, and global resume policy.
   - Does not call server internals.

2. SearchEngine
   - Owns per-theorem proof search attempts.
   - Owns graph nodes, edges, PUCT stats, selected proof paths, rollouts,
     subpaths, and training records.
   - Constructs `item_id`s for backend state ownership.
   - Calls cleanup only after it has persisted or handed off all graph/search
     data it needs.

3. EnvClient
   - Search-facing client wrapper.
   - Exposes `create_states`, `step_node`, `step_batch`, and `cleanup`.
   - Converts SearchEngine calls into `/exec` HTTP calls.

4. EnvClient Batcher
   - Client-side microbatcher.
   - Splits large search waves into bounded HTTP requests.
   - Owns microbatch-level resume bookkeeping when a trainer call contains many
     microbatches.

5. HTTP API Router
   - FastAPI routes for `/exec/create_states`, `/exec/step_batch`, and
     `/exec/cleanup`.
   - Validates public request shape, reads settings, and calls server backend
     logic.

6. Server Exec Scheduler
   - Lives behind the route in `server/exec_backends.py`.
   - Enforces request caps.
   - Resolves and pins state tokens.
   - Groups compatible items by `(env_profile, header_hash)`.
   - Splits one HTTP request into bounded worker lanes.

7. StateStore
   - Owns `state_token -> saved state file + metadata`.
   - Creates child tokens, deletes states by `item_id`, and runs TTL/storage GC.

8. PantographManager
   - Owns free/busy Pantograph worker leases.
   - Reuses compatible workers by exact `(env_profile, header_hash)`.
   - Enforces worker count caps and worker startup timeout.
   - Starts, evicts, recycles, and closes worker processes.

9. PantographWorker
   - Python wrapper around one Pantograph subprocess.
   - Implements create-state and step-state operations using the old proven
     Pantograph commands.

10. Pantograph/Lean Process
    - Performs Lean work: `load_sorry`, `goal_load`, `goal_tactic`,
      `goal_save`, and Pantograph in-process GC.

## Batch Levels

There are two batch levels because they solve different problems.

### Client Microbatching

The trainer/search loop may produce a large wave of proof states to step.
The EnvClient should split that wave into bounded HTTP microbatches.

Example:

```text
trainer asks EnvClient to step 1024 items
server request cap is 16 items
EnvClient splits the call into 64 HTTP microbatches
```

This level is the right place to persist coarse progress:

```text
env_call_id = call_abc
  microbatch_0: complete, response persisted
  microbatch_1: complete, response persisted
  ...
  microbatch_62: complete, response persisted
  microbatch_63: pending/running/unknown
```

If a crash happens near microbatch 63, resume should restart from microbatch 63,
not from item 0 of the original 1024-item call.

### Server Scheduling

One HTTP microbatch is still a batch of proof-state expansion items. The server
scheduler maps that one request onto bounded worker lanes.

Example with one HTTP request:

```text
16 items x 8 tactics = 128 tactic attempts
max_lean_processes_per_env_profile = 4
all 16 items share env/header
```

The scheduler creates 4 lanes:

```text
lane 0: items 0, 4, 8, 12   -> 32 tactic attempts
lane 1: items 1, 5, 9, 13   -> 32 tactic attempts
lane 2: items 2, 6, 10, 14  -> 32 tactic attempts
lane 3: items 3, 7, 11, 15  -> 32 tactic attempts
```

Each lane leases one compatible Pantograph worker and steps its assigned items
sequentially. Each item tries its own tactic list sequentially against the same
parent state. Tactic candidates inside one item are alternatives, not a chain.

If the same request has fragmented headers:

```text
16 items, 16 different headers
```

the scheduler creates 16 compatibility groups. The manager still caps the number
of actual worker processes, so groups queue behind the bounded pool. This
preserves correctness and header-cache reuse, but throughput depends on header
locality.

## Public API

The stable public API is `/exec`.

### POST `/exec/create_states`

Creates initial saved proof states from Lean code blocks.

Request:

```json
{
  "env_profile": "lean4.29.1_mathlib",
  "items": [
    {
      "item_id": "run_123:theorem_42:attempt_1",
      "code": "import Mathlib\n\ntheorem t (n : Nat) : n + 0 = n := by\n  sorry",
      "timeout_ms": 30000
    }
  ]
}
```

Response:

```json
{
  "items": [
    {
      "item_id": "run_123:theorem_42:attempt_1",
      "status": "open",
      "states": [
        {
          "state_token": "st_tS4m1A9uH0bC2WgY9eZp",
          "goals": ["n : Nat\n⊢ n + 0 = n"]
        }
      ],
      "messages": []
    }
  ]
}
```

Semantics:

- The backend splits each code block into header/imports and body.
- The header/import block is the worker warm-cache key.
- Complete code returns `status="complete"` and no state tokens.
- Code with `sorry` holes returns one or more saved executable states.
- Bad Lean code returns `status="error"` with messages.

### POST `/exec/step_batch`

Steps many proof-state expansion items. Each item expands one saved state with
many candidate tactics.

Request:

```json
{
  "items": [
    {
      "node_id": "run_123:theorem_42:attempt_1:n7",
      "state_token": "st_tS4m1A9uH0bC2WgY9eZp",
      "tactics": ["simp", "rw [Nat.add_comm]", "omega"],
      "timeout_ms": 30000
    }
  ]
}
```

Response:

```json
{
  "items": [
    {
      "node_id": "run_123:theorem_42:attempt_1:n7",
      "results": [
        {
          "tactic": "simp",
          "status": "complete",
          "messages": []
        },
        {
          "tactic": "rw [Nat.add_comm]",
          "status": "open",
          "state_token": "st_G6z3h9Kp8n2E",
          "goals": ["n : Nat\n⊢ 0 + n = n"],
          "messages": []
        },
        {
          "tactic": "omega",
          "status": "error",
          "messages": ["omega could not close the goal"]
        }
      ]
    }
  ]
}
```

Semantics:

- The request does not carry `env_profile`, `header`, or `item_id`.
- The backend resolves those from the parent `state_token`.
- Open child states inherit the parent token's `item_id`, `env_profile`, and
  header metadata.
- Results are returned in deterministic input item and tactic order.

### POST `/exec/cleanup`

Deletes saved executable states owned by completed or abandoned search attempts.

Request:

```json
{
  "item_ids": ["run_123:theorem_42:attempt_1"]
}
```

Response:

```json
{
  "deleted_items": [
    {
      "item_id": "run_123:theorem_42:attempt_1",
      "deleted_states": 128,
      "deleted_bytes": 73400320
    }
  ]
}
```

Important distinction:

- `POST /exec/cleanup` is public state cleanup. The SearchEngine/EnvClient calls
  it after a proof-search attempt is done or abandoned.
- `PantographManager.cleanup()` is private server shutdown cleanup. It closes
  worker processes. The trainer should never call it.

Cleanup call chain:

```text
SearchEngine has persisted needed proof paths / rollouts / graph records
  -> EnvClient.cleanup([item_id])
  -> POST /exec/cleanup
  -> StateStore.delete_by_item_id(item_id)
```

After cleanup, old `state_token`s for that `item_id` are dead. Training can use
persisted goal text, tactic paths, scores, visit counts, and proof records, but
it cannot keep stepping those deleted backend states.

### `/verify`

Keep Kimina's existing whole-file verification path. Final accepted proofs must
still pass strict verification with unauthorized axioms and unwanted `sorry`
usage rejected.

## Identifiers

### `state_token`

Opaque executable backend handle.

```text
state_token -> saved state file + metadata
```

The caller never constructs or interprets it. It is not a file path, not pretty
proof-state text, and not a process-local Pantograph id.

### `item_id`

Opaque search-attempt owner id.

Canonical examples:

```text
run_123:theorem_42:attempt_1
run_123:theorem_42:attempt_2
```

Every root and child state created during that search attempt is tagged with the
same `item_id`. Cleanup deletes by `item_id`.

### `node_id`

Caller bookkeeping for one node inside the SearchEngine graph. The backend only
echoes it in `/exec/step_batch` responses. It is not used for worker routing or
storage ownership.

### `env_call_id` and `microbatch_id`

Client/trainer-side resume identifiers for large EnvClient calls.

These are not required in the current public `/exec` schema yet, but production
resume should persist a mapping like:

```text
(env_call_id, microbatch_id) -> request payload, response payload, status
```

This lets resume skip already-finished microbatches after a trainer/client
crash.

## StateStore

`StateStore` owns saved proof-state files and metadata.

Required operations:

```python
put(path, *, item_id, env_profile, header, header_hash) -> state_token
resolve(state_token) -> StateRecord
resolve_and_pin(state_token) -> StateRecord
create_child(parent_token, child_path) -> state_token
delete_by_item_id(item_id) -> DeleteStats
gc_expired() -> DeleteStats
stats() -> StateStoreStats
```

Current safety nets:

- Explicit `/exec/cleanup` by `item_id`.
- TTL GC for abandoned states.
- Storage-budget GC controlled by `max_state_store_bytes`.
- Sidecar rehydration after backend restart.
- Orphan sweeping for stale scratch files that were never promoted.
- Pinning parent states during in-flight stepping so cleanup/GC does not delete
  a parent while a worker is loading it.

Production gap:

- Cleanup can race with in-flight step work for the same `item_id`. Pinning
  protects parent files, but a step could finish after cleanup and create new
  child tokens for an item that the caller already tried to delete.

Required fix:

- Add item-level in-flight tracking and/or deletion tombstones.
- If cleanup is called for an active `item_id`, either wait for active work to
  finish before deleting, or mark the `item_id` as deleting and immediately
  delete any children created after the mark.
- Add tests where `/exec/cleanup` races an in-flight `/exec/step_batch` and the
  final state-store count is zero for that `item_id`.

## Crash And Resume Model

The v0 production resume level is microbatch-level EnvClient resume, not
partial proof-graph resume inside the backend.

### Case A: Server Crashes Mid-Microbatch

The client gets a timeout or connection error. Some scratch files or promoted
state files may exist. On restart, StateStore sidecars can be rehydrated and
orphan GC can remove stale scratch.

Policy:

- Treat that microbatch as uncertain.
- Retry or discard that microbatch.
- Do not restart earlier completed microbatches.

### Case B: Server Finishes Microbatch, Client Does Not Receive Response

The server may have created child state tokens, but the client did not see them.

Without idempotency or persisted server-side request results, blindly retrying
can create duplicate child states. This is acceptable only if the SearchEngine
abandons the uncertain microbatch/attempt and cleans by `item_id`.

Production target:

- Persist client-side microbatch status and response once received.
- Consider optional server-side idempotency keys later:

```text
(env_call_id, microbatch_id) -> prior response
```

### Case C: Client Receives Response, Then Trainer Crashes

This time period exists. The client/trainer may have child tokens in memory but
may not have sent the next microbatch yet.

Policy:

- If microbatch response was persisted, resume from the next incomplete
  microbatch.
- If response was not persisted, treat that microbatch as uncertain.

### Case D: All Search Data Is Persisted, Cleanup Was Not Called

Old executable states remain in StateStore.

Policy:

- On trainer resume, cleanup abandoned or completed `item_id`s.
- TTL/storage GC remains a backend safety net.

## Cleanup And Training Data

Cleanup must happen after the SearchEngine has constructed and persisted the
training/search records it needs.

Safe order:

```text
1. Search attempt runs.
2. SearchEngine updates graph and selects paths/rollouts/subpaths.
3. SearchEngine persists or hands off training records.
4. EnvClient calls /exec/cleanup(item_id).
```

The backend does not know which graph nodes are useful. It cannot decide which
branches should become training data. That is SearchEngine/trainer policy.

## Load Control

The manager and API/backend scheduler enforce different caps.

Manager caps protect Lean worker processes:

```text
max_pantograph_workers
max_lean_processes_per_env_profile
max_pantograph_worker_uses
pantograph_worker_startup_timeout_seconds
```

API/backend caps protect the web server, request queue, and state store:

```text
max_items_per_step_batch
max_tactics_per_step_item
max_attempts_per_step_batch
max timeout per item
max create items per request
max in-flight exec requests
max queued exec requests
max_state_store_bytes
state_ttl_seconds
```

Current status:

- `/exec/step_batch` has item/tactic/attempt caps in the server exec scheduler.
- `PantographManager` bounds worker processes and per-env-profile worker count.
- `StateStore` has TTL and storage-budget GC.

Needed:

- Add symmetric caps for `/exec/create_states`.
- Add max timeout validation.
- Add route/backend-level in-flight request limits.
- Return explicit overload responses instead of allowing unbounded waiters.
- Add metrics for rejected requests and manager wait time.

## Deployment Invariant

Run exactly one FastAPI server process per node for this backend.

Correct shape:

```text
one FastAPI process
  one PantographManager
    N Pantograph/Lean worker processes
```

Do not deploy this backend with multiple FastAPI worker processes such as:

```bash
uvicorn server.main:app --workers 4
gunicorn -w 4 ...
```

unless StateStore metadata, item locks, and worker ownership are moved to a
shared database/coordinator. With multiple FastAPI processes today, each process
would have its own in-memory StateStore index and PantographManager. One process
could create a token that another process cannot resolve from memory.

## Observability

Production needs metrics/logs for:

- request count and latency by endpoint;
- create vs step vs cleanup latency;
- item count, tactic count, and attempt count per request;
- manager lease wait time;
- worker cold start vs warm reuse;
- workers free/busy/starting by `env_profile`;
- per-worker PID, RSS, use count, and header hash;
- state-store count and bytes;
- cleanup deleted states/bytes;
- GC deleted states/bytes;
- rejected requests by cap/overload reason;
- status mix: `open`, `complete`, `error`, `invalid_state_token`;
- microbatch completion and retry counts on the client side.

Expose at least a server-side stats endpoint or structured logs before calling
the backend production-ready.

## Implementation Status

Already implemented on `main`:

- Public `/exec/create_states`, `/exec/step_batch`, and `/exec/cleanup` schemas.
- Pantograph worker wrapper using old commands.
- StateStore token metadata, sidecars, rehydration, TTL GC, storage-budget GC,
  orphan sweeping, and parent pinning.
- PantographManager with free/busy leases, exact header reuse, idle eviction,
  worker use recycling, startup timeout floor, per-env-profile cap, and stats.
- Server exec scheduler for `/exec/step_batch` with cap validation,
  compatibility grouping, bounded lanes, and input-order output.
- Async EnvClient and microbatcher shape.

Known gaps:

- `/exec/create_states` lacks the same caps/backpressure discipline as
  `/exec/step_batch`.
- Cleanup vs in-flight step needs item-level tombstone or wait semantics.
- Exec client calls use generic retry behavior; create/step are not safely
  idempotent yet.
- Client-side persisted microbatch progress does not exist yet.
- No production stats endpoint for worker/state/load metrics.
- Full pre-commit is blocked by unrelated existing client typing/syntax issues.

## Production Roadmap

### Phase 1: Align The Plan And Docs

Status: completed by this revision.

- Make this file the authoritative production backend plan.
- Mark `docs/lean-task-backend-research.md` as research-only, not mainline.
- Keep `main` free of `pantograph_task` and forked Pantograph changes.

Acceptance:

- A reader can tell that `main` production is bounded old-command Pantograph
  process pooling.
- The object boundaries and cleanup ownership are explicit.

### Phase 2: Cleanup Race Safety

- Add item-level in-flight tracking and deletion tombstones to StateStore or the
  server exec layer.
- Define exact behavior when cleanup is called while step work for the same
  `item_id` is running.
- Test cleanup racing step and create paths.

Acceptance:

- After cleanup returns for an `item_id`, no tracked state files remain for that
  `item_id`, including children produced by racing work.
- Parent pinning still prevents load-time deletion while work is active.

### Phase 3: API Caps And Backpressure

- Add create-state request caps.
- Add max timeout validation.
- Add route/backend-level in-flight and queued request limits.
- Return clear 422 for invalid request size and 429/503 for overload.
- Add tests for cap boundaries and overload behavior.

Acceptance:

- Oversized requests are rejected before worker leasing.
- Load beyond configured queue capacity fails predictably.
- Manager worker cap is not the only load-control mechanism.

### Phase 4: Client-Side Exec Semantics

- Stop blind automatic retries for non-idempotent `/exec/create_states` and
  `/exec/step_batch`, or add idempotency keys before retrying them.
- Add a production EnvClient call manager that persists:

```text
env_call_id
microbatch_id
request payload
response payload
status: pending | running | complete | failed | unknown
```

- Resume large calls from the first incomplete/unknown microbatch.
- Keep cleanup idempotent and retryable.

Acceptance:

- A simulated crash at microbatch 63 of 64 resumes from microbatch 63, not 0.
- A network failure after a completed step does not silently duplicate useful
  graph work without being marked uncertain.

### Phase 5: Observability And Health

- Add stats endpoint or structured metrics covering worker pool, state store,
  request caps, status mix, and memory.
- Add logs with `item_id`, `node_id`, `env_profile`, `header_hash`, worker PID,
  and request/microbatch ids where available.

Acceptance:

- During a benchmark, we can explain throughput from header grouping, worker
  cold starts, manager wait, and per-worker utilization.

### Phase 6: E2E Benchmark And Soak

- Run real Goedel-derived workload through current `main` only.
- Benchmark create, step, and cleanup separately and together.
- Use representative shapes:

```text
16 items x 8 tactics
200 items x 8 tactics
1024 items split into 64 microbatches of 16
```

- Record:
  - wall time;
  - throughput;
  - p50/p95/p99 latency;
  - cold vs warm worker behavior;
  - header group sizes;
  - worker lane distribution;
  - state-store count/bytes before and after cleanup;
  - RSS/memory;
  - status mix;
  - cleanup correctness.

Acceptance:

- End-to-end run completes without worker leaks or state-store leaks.
- Cleanup returns state-store usage for the run to zero.
- Memory is bounded by configured worker count, not by item count.
- Throughput numbers are reported with header-fragmentation and cold-start
  context.

## Non-Goals For Mainline

- In-process Lean task parallelism.
- `pantograph_task` as a serving backend.
- Public exposure of Pantograph raw state ids.
- Multi-node state movement.
- Multiple FastAPI worker processes sharing one state directory.
- Backend-owned proof-search graph policy.
- Backend deciding which rollouts/subpaths become training data.

Those can be researched separately, but they are not prerequisites for making
the current backend production-ready.
