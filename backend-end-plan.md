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
   - Exposes `create_states`, `step_node`, `step_batch`, `cancel`, `cleanup`, and
     `limits`.
   - Converts SearchEngine calls into `/exec` HTTP calls.

4. EnvClient Batcher
   - Client-side microbatcher.
   - Splits large search waves into bounded HTTP requests.
   - Owns microbatch-level resume bookkeeping when a trainer call contains many
     microbatches.

5. HTTP API Router
   - FastAPI routes for `/exec/create_states`, `/exec/step_batch`,
     `/exec/cancel`, `/exec/cleanup`, and `/exec/limits`.
   - Validates public request shape, reads settings, and calls server backend
     logic.

6. Server Exec Scheduler
   - Lives behind the route in `server/exec_backends.py`.
   - Enforces request caps.
   - Maintains per-`item_id` lifecycle state.
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

The trainer/search loop produces proof-state expansions one node at a time. The
EnvClient batcher coalesces those `submit_step` calls bottom-up into bounded HTTP
requests; it does not take a pre-made large list and cut it top-down.

The microbatch shape should be derived, not configured with blind client
constants. The server owns the hard caps and advertises both caps and
recommendations (see `GET /exec/limits`); the client derives:

```text
microbatch size   from recommended_items_per_step_batch, never above
                  max_items_per_step_batch or max_attempts_per_step_batch
flush trigger     send when size is reached OR the current submit burst drains
                  (no fixed coalescing timer)
in-flight bound   from recommended_in_flight_step_batches, with `overloaded` /
                  429 as the backpressure signal to throttle
```

This removes the static `max_items` / `max_wait_ms` / `max_in_flight_batches`
knobs and the risk of a client batch size that disagrees with the server cap. The
Stage 4 client path now has `from_server_limits(...)` and resumable
microbatching; callers that bypass the batcher and invoke raw `step_batch`
directly are still responsible for staying under server caps.

The recommended item count is not the same as worker count. A 16-item request is
valid with four workers: the server creates four lanes and each lane processes
four items sequentially. Worker count limits parallel lanes; request caps and the
server recommendation define the HTTP microbatch size.

Until idempotency exists, the client must not pipeline multiple live
`/exec/step_batch` requests for the same `item_id`. It may have many requests in
flight for different attempts.

This level is also the right place to persist coarse progress so resume is cheap:

```text
env_call_id = call_abc
  microbatch_0: complete, response persisted
  microbatch_1: complete, response persisted
  ...
  microbatch_62: complete, response persisted
  microbatch_63: pending/running/unknown
```

If a crash happens near microbatch 63, resume should restart from microbatch 63,
not from the start of the original call.

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

The examples below show the target production schema. During migration, the
current `timeout_ms` field remains accepted as a deprecated alias for both
`acquire_timeout_ms` and `step_timeout_ms`.

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
      "acquire_timeout_ms": 30000,
      "step_timeout_ms": 30000
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
      "acquire_timeout_ms": 30000,
      "step_timeout_ms": 30000
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
- Per-result `status` is one of `open`, `complete`, `error`,
  `invalid_state_token`, `overloaded`, or `cancelled`. `overloaded` means the
  backend could not lease a Lean worker within the acquire budget: no tactic ran
  and no child state was created, so the item is safe to retry unchanged. Pool
  busyness must never be reported as `error`.
- Two independent timeout budgets apply per item: an acquire budget (time
  willing to wait for a worker) and a step budget (per-tactic Lean execution
  time). See Timeouts.

### POST `/exec/cleanup`

Deletes saved executable states owned by completed or abandoned search attempts.

Request:

```json
{
  "item_ids": ["run_123:theorem_42:attempt_1"]
}
```

Response (quiescent item -- deleted):

```json
{
  "deleted_items": [
    {
      "item_id": "run_123:theorem_42:attempt_1",
      "status": "deleted",
      "deleted_states": 128,
      "deleted_bytes": 73400320
    }
  ]
}
```

Response (work still in flight -- deferred, nothing deleted):

```json
{
  "deleted_items": [
    {
      "item_id": "run_123:theorem_42:attempt_1",
      "status": "deferred",
      "reason": "in_flight",
      "in_flight": 2,
      "pinned_states": 0,
      "deleted_states": 0,
      "deleted_bytes": 0
    }
  ]
}
```

Cleanup deletes everything for the item or nothing -- never a partial delete, and
never success while state remains (see StateStore). When `deferred`, the caller
cancels and/or retries cleanup until `deleted`.

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
  -> lifecycle cleanup decision
  -> StateStore.delete_by_item_id_all_or_none(item_id)
```

After cleanup, old `state_token`s for that `item_id` are dead. Training can use
persisted goal text, tactic paths, scores, visit counts, and proof records, but
it cannot keep stepping those deleted backend states.

### POST `/exec/cancel`

Stops accepting and running new work for an attempt so it can be cleaned up while
it still has live work. Cancellation is cooperative: no new create/step work
starts for the `item_id`, and the currently running Pantograph command is allowed
to finish (v0 does not kill mid-tactic).

Request:

```json
{
  "item_ids": ["run_123:theorem_42:attempt_1"]
}
```

Response:

```json
{
  "items": [
    {
      "item_id": "run_123:theorem_42:attempt_1",
      "status": "cancelling",
      "in_flight": 3
    }
  ]
}
```

Semantics:

- Marks the `item_id` `cancelling`; future `/exec/create_states` and
  `/exec/step_batch` items for it return `cancelled`.
- Repeated cancel is idempotent and returns the current `in_flight`, so the caller
  can poll until it reaches zero (`drained`), then call `/exec/cleanup`.
- Cancelling a quiescent item (`in_flight == 0`) goes straight to drained.
- Cancel status is one of `cancelling`, `drained`, or `cleaned`. `cleaned` means
  cleanup already ran, so there is nothing left for cancel to do.
- Cancel abandons the whole attempt. It does not recover a lost response for the
  same attempt; that needs idempotency or persisted microbatch results (Phase 4).

### GET `/exec/limits`

Advertises server caps so the client can derive microbatch shape instead of
hardcoding it.

Response:

```json
{
  "max_items_per_step_batch": 1024,
  "max_tactics_per_step_item": 64,
  "max_attempts_per_step_batch": 8192,
  "max_pantograph_workers": 8,
  "max_lean_processes_per_env_profile": 4,
  "recommended_items_per_step_batch": 16,
  "recommended_in_flight_step_batches": 4,
  "same_item_id_pipelining": false,
  "cleanup_policy": "defer_while_in_flight"
}
```

The client sizes microbatches to `recommended_items_per_step_batch`, validates
against the hard caps before sending, and bounds concurrent HTTP calls with
`recommended_in_flight_step_batches`. This keeps a single source of truth for caps
on the server and removes blind client-side batch constants. See Client
Microbatching.

### `/verify`

Keep Kimina's existing whole-file verification path. Final accepted proofs must
still pass strict verification with unauthorized axioms and unwanted `sorry`
usage rejected.

### Schema Migration Checklist

The status, timeout, and cleanup changes in this plan are schema changes that
must land in lockstep on both sides; there are two copies of the exec models.

- Add `overloaded` and `cancelled` to `ExecStatus` in both
  `server/schemas_exec.py` and `kimina_client/exec_models.py`.
- Add separate literal status types for cleanup (`deleted` | `deferred`) and
  cancel (`cancelling` | `drained` | `cleaned`) instead of overloading
  `ExecStatus`.
- Split `timeout_ms` into `acquire_timeout_ms` and `step_timeout_ms` on the
  request items in both copies; keep accepting `timeout_ms` as a deprecated alias
  so existing callers do not break.
- For `/exec/create_states`, `step_timeout_ms` means the Lean create-state command
  budget; there is no tactic, but the same worker execution timeout concept
  applies.
- Add `status` (`deleted` | `deferred`) and `in_flight` fields to the cleanup
  result so cleanup can report a deferred (no-op) delete.
- Add cleanup `reason` (`in_flight` | `pinned`) and `pinned_states` so unexpected
  pinned-state deferrals are visible instead of looking like empty deletes.
- Add `POST /exec/cancel` request/response models (lifecycle `status` +
  `in_flight`).
- Add the `GET /exec/limits` response model.
- Gate new fields as optional (or version the API) so an old client and new
  server, and vice versa, still interoperate during rollout.

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
delete_by_item_id_all_or_none(item_id) -> DeleteDecision
count_by_item_id(item_id) -> int
gc_expired() -> DeleteStats
stats() -> StateStoreStats
```

`delete_by_item_id` remains useful for best-effort GC-style deletion.
`delete_by_item_id_all_or_none` is the public cleanup path and returns either
`deleted(stats)` or `deferred(pinned_count)` without modifying the store when it
defers.

Current safety nets:

- Explicit `/exec/cleanup` by `item_id`.
- TTL GC for abandoned states.
- Storage-budget GC controlled by `max_state_store_bytes`.
- Sidecar rehydration after backend restart.
- Orphan sweeping for stale scratch files that were never promoted.
- Pinning parent states during in-flight stepping so cleanup/GC does not delete
  a parent while a worker is loading it.

Production gap:

- Cleanup can race in-flight work for the same `item_id`. The window is between a
  step's `resolve_and_pin` of its parent and its later `create_child`:
  `delete_by_item_id` deletes everything for the item except the pinned parent,
  returns a normal 200 with a `deleted_states` count, and the still-running step
  then registers new children that survive. So cleanup both leaks (pinned parent
  plus post-cleanup children) and lies (reports success while state remains).
- This requires same-`item_id` work to overlap a cleanup. Unique-per-attempt
  `item_id`s remove the cross-attempt version. The intra-attempt version occurs
  only when an attempt has work in flight while it cleans up: pipelined
  same-attempt stepping, proof-found-early with siblings still live,
  cancellation, or a client that abandons after a timeout while the server is
  still running.

Required fix -- lifecycle registry + deferred cleanup (not deletion tombstones):

Track live work per `item_id` and make cleanup defer instead of lie. The server
exec layer keeps an in-memory lifecycle registry, implemented as a small public
class in a dedicated module such as `server/exec_lifecycle.py`:

```text
item_id -> {
  status: active | cancelling | drained | cleaned
  in_flight_count: int
  terminal_expires_at: timestamp | None
}
```

Rules:

```text
create/step begins:
  if item_id is cancelling/drained/cleaned -> return cancelled
  in_flight_count += 1

create/step ends:
  finish all StateStore writes first (create_child, pin release)
  in_flight_count -= 1
  if cancelling and in_flight_count == 0 -> drained

cleanup(item_id):
  if in_flight_count > 0 -> deferred, delete nothing
  else -> all-or-nothing delete all states, mark cleaned

cancel(item_id):
  if in_flight_count == 0 -> drained
  else -> cancelling
  new work for item_id returns cancelled;
  current Pantograph commands drain cooperatively
```

Concrete server-side surface:

```python
class ItemLifecycleRegistry:
    def begin(self, item_id: str) -> BeginResult: ...
    def finish(self, item_id: str) -> LifecycleSnapshot: ...
    def cancel(self, item_id: str) -> LifecycleSnapshot: ...
    def cleanup_decision(self, item_id: str) -> CleanupDecision: ...
    def mark_cleaned(self, item_id: str) -> LifecycleSnapshot: ...
    def sweep_terminal(self) -> int: ...
```

`BeginResult` is `started` or `cancelled`; `CleanupDecision` is `delete` or
`defer(in_flight=N)`. `LifecycleSnapshot` carries the public lifecycle status and
current `in_flight_count`.

These methods should be synchronous. The route/backend code can call
`cleanup_decision`, `StateStore.delete_by_item_id_all_or_none`, and
`mark_cleaned` in one non-`await` block. Likewise, step can call
`resolve_and_pin` and `begin` in one non-`await` block. If the registry itself
needs a lock later, do not hold an async lock across worker execution; only
protect the small synchronous lifecycle mutations.

Critical invariants:

- The in-flight bracket must enclose state registration, not just Lean
  execution. Decrement `in_flight_count` only after `StateStore.put` /
  `create_child` and pin release complete, or cleanup can still slip in. Bracket
  the whole item handler with try/finally.
- Cleanup marks an item `cleaned` only after all-or-nothing StateStore deletion
  succeeds. A deferred cleanup must leave the prior lifecycle status intact.
- For `/exec/step_batch`, the backend only learns `item_id` after resolving the
  parent `state_token`. Therefore `resolve_and_pin(state_token)` and
  `lifecycle.begin(record.item_id)` must be adjacent synchronous operations with
  no `await` between them. If cleanup can interleave between pinning and lifecycle
  begin, the original race still exists.
- Both `create_states` and `step_batch` register work. A late `create_states`
  can otherwise mint root states after cleanup.
- The lane loop checks `cancelling` before each item so cooperative drain is
  bounded by one item's remaining tactics, not the whole lane.
- v0 cancellation does not interrupt `PantographManager.get_worker` while an item
  is waiting for a lease. A waiter may drain only when `acquire_timeout_ms`
  expires unless the manager is later made cancellation-aware.
- A late `/exec/step_batch` for a cleaned item can only return `cancelled` if its
  parent token still resolves and reveals the `item_id`. If cleanup already
  deleted that token, `invalid_state_token` is the correct response. Do not add
  token tombstones just to distinguish those two terminal cases.

GC is a backstop, not the answer. Cleanup must definitively delete or explicitly
defer; TTL/storage GC only matters if the client disappears and never retries
cleanup. Do not rely on GC to resolve a live race.

Public cleanup must not call the current partial-delete behavior directly. It
needs an all-or-nothing StateStore path: if any token for that `item_id` is still
pinned or otherwise not deletable, return `deferred` and delete nothing. TTL and
storage GC may continue to skip pinned states because they are best-effort
maintenance paths, not user-visible cleanup contracts.

Registry retention:

- Keep `cancelling`/`drained`/`cleaned` records for a bounded window so late or
  retried work for that `item_id` is still rejected when the `item_id` is known:

```text
terminal_retention_seconds >= max client HTTP timeout + max retry/backoff window
```

- Evict after that. This is late-request suppression, not durable correctness;
  durable duplicate suppression across restarts would need idempotency keys or a
  persisted request-result table, not StateStore deletion tombstones.
- The registry stays in-memory. On server restart nothing is in flight, so
  `in_flight_count` does not need rehydration.

Deletion tombstones (have in-flight steps refuse to register children for a
deleting item so cleanup can delete *before* work drains) are deliberately out of
scope until we need hard immediate cancellation.

Tests: `/exec/cleanup` racing an in-flight `/exec/step_batch` returns `deferred`
while live and deletes to zero once drained; a late create for a cleaned item
returns `cancelled`; a late step for a cleaned item returns `cancelled` only if
the parent token still resolves, otherwise `invalid_state_token`.

## Crash And Resume Model

The v0 production resume level is microbatch-level EnvClient resume, not
partial proof-graph resume inside the backend.

### Case A: Server Crashes Mid-Microbatch

The client gets a timeout or connection error. Some scratch files or promoted
state files may exist. On restart, StateStore sidecars can be rehydrated and
orphan GC can remove stale scratch.

Policy:

- Treat that microbatch as uncertain.
- If preserving the same attempt, do not blindly replay the uncertain microbatch
  unless idempotency exists.
- v0 policy is to abandon the affected attempt ids, call `cancel` if the endpoint
  exists and the server is still alive, then retry `cleanup` until deleted.
  Reissue work under new attempt ids if search should continue.
- Do not restart earlier completed microbatches.

### Case B: Server Finishes Microbatch, Client Does Not Receive Response

The server may have created child state tokens, but the client did not see them.

Without idempotency or persisted server-side request results, blindly retrying
can create duplicate child states. This is acceptable only if the SearchEngine
abandons the uncertain microbatch/attempt, calls `cancel` if available, and
cleans by `item_id`.

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
- If response was not persisted, treat that microbatch as uncertain. Without
  idempotency, abandon and cleanup the affected attempt ids before reissuing.

### Case D: All Search Data Is Persisted, Cleanup Was Not Called

Old executable states remain in StateStore.

Policy:

- On trainer resume, cleanup abandoned or completed `item_id`s.
- TTL/storage GC remains a backend safety net.

### Retry Safety By Outcome

Retry safety has two axes, and the first one dominates: did the response arrive
at all, and if so, what did each result say.

Axis 1 -- transport outcome:

```text
unknown   the HTTP call timed out / dropped, response never observed
          -> the whole item is uncertain: the server may have run it and created
             children, or nothing. Abandon, cancel if the endpoint exists,
             then retry cleanup until deleted before retrying under a new attempt.
observed  the response arrived, so the exact per-result status is known
          -> apply Axis 2.
```

Axis 2 -- per-result status, only meaningful for an observed response:

```text
overloaded            no worker leased, no tactic ran, no child created
                      -> safe to retry as-is, no idempotency key needed
open                  a child state token was created
                      -> do not re-step the same parent (would duplicate); use the
                         child you already received
complete / error      a terminal answer, no child state created
                      -> no retry needed; the result is final
cancelled             the attempt is being abandoned
                      -> do not retry; the whole item_id is going away
invalid_state_token   parent state already gone
                      -> do not retry
```

The common mistake is to treat `complete` / `error` as unsafe because "a tactic
ran." They create no child state, so an observed `complete` / `error` is final,
not a retry hazard. The genuinely unsafe case is an `unknown` transport outcome,
because only then might children exist that you cannot see. `overloaded` is the
one class that is both observed and child-free, so it is unconditionally safe to
retry -- which is exactly why acquisition contention must surface as `overloaded`
and never as `error`.

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
recommended_items_per_step_batch
recommended_in_flight_step_batches
max acquire and step timeout per item
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
- Split `timeout_ms` into separate acquire and step budgets and validate both
  (see Timeouts).
- Add route/backend-level in-flight request limits.
- Return retryable backpressure (`overloaded` per item, or 429/503) for
  acquisition contention instead of allowing unbounded waiters or reporting
  per-item `error`.
- Add `GET /exec/limits` with both hard caps and server recommendations; clients
  must treat caps as validation limits and recommendations as batching defaults.
- Add metrics for rejected requests and manager wait time.

## Timeouts

`timeout_ms` on a request item currently drives two unrelated things and should
be split into separate knobs:

```text
acquire_timeout_ms  time the item is willing to wait for a free Lean worker
step_timeout_ms     per-tactic Lean execution time for one Pantograph command
```

These are different concerns. Acquisition contention is a transient,
server-side "the pool is busy" condition. Per-tactic execution time is a
semantic bound on one proof step. Today both come from the same field, so
raising the proof-step timeout also makes items queue longer before failing,
and vice versa.

For `/exec/create_states`, `step_timeout_ms` is the Lean create-state command
budget. The name is kept shared so create and step request items use the same
timeout model.

Timeouts in the runtime stack:

```text
acquire   per item    wait for a free/started worker (PantographManager.get_worker)
step      per tactic   Lean execution per goal_load / goal_tactic command
startup   per worker   new Lean process boot floor (pantograph_worker_startup_timeout_seconds)
state TTL per state    idle state eviction (state_ttl_seconds)
http      per call     client read/connect budget
```

The acquire budget starts ticking when `get_worker` is called, and the acquire
and step budgets are independent (waiting for a worker does not eat into
execution time). Because step time is per Pantograph command, an item with N
tactics has a worst-case execution wall time near `N * step_timeout`, which a
single per-item acquire budget does not account for.

Items after the first in a lane wait behind their lane-mates' execution. That
wait is neither acquire nor step time, but it is already bounded by the per-tactic
timeout (a lane's wall time is at most its total tactics times `step_timeout`), so
it does not need its own knob. The one consequence to respect is sizing: the
client HTTP read timeout must exceed this worst-case lane wall time, or the client
will give up on legitimately running work and falsely abandon it. Keep the HTTP
timeout generous (the default is 600s) and record lane-queue time as a metric, not
a parameter.

Acquisition contention must not be reported as a proof failure. When the
backend cannot lease a worker within the acquire budget, it must return
retryable backpressure (`overloaded` per item, or 429/503 for the whole
request), never `status="error"`. Collapsing pool busyness into `error` makes
contention indistinguishable from a genuinely failing tactic and poisons search
and training signal. See Retry Safety By Outcome.

Cancellation is bounded by these budgets. In v0, cancel does not kill a running
Pantograph command and does not interrupt a `get_worker` waiter. The practical
drain bound for an already-started item is its remaining tactic commands times
`step_timeout_ms`; the practical drain bound for an item still waiting on a lease
is `acquire_timeout_ms`. A later manager change can make worker acquisition
actively cancellable, but that is not required for the first production slice.

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
- lane queue time per item (wait behind lane-mates);
- worker cold start vs warm reuse;
- workers free/busy/starting by `env_profile`;
- per-worker PID, RSS, use count, and header hash;
- state-store count and bytes;
- cleanup deleted states/bytes, and deferred-cleanup count;
- items by lifecycle status, in-flight count per item, and cancels;
- GC deleted states/bytes;
- rejected requests by cap/overload reason;
- status mix: `open`, `complete`, `error`, `invalid_state_token`, `overloaded`,
  `cancelled`;
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
- Item lifecycle registry, all-or-nothing public cleanup, deferred cleanup, and
  cooperative cancel.
- Split acquire/step timeout schema with deprecated `timeout_ms` compatibility.
- `overloaded` and `cancelled` exec statuses.
- `/exec/cancel` and `/exec/limits`.
- Create/step caps, route-level exec request limiter, and retryable overload
  responses.
- Async EnvClient, derived-limit batcher setup, observed-overload retry,
  same-`item_id` request serialization, and JSON microbatch resume journal.

Known gaps:

- No production stats endpoint for worker/state/load metrics.
- Whole-system Goedel E2E benchmark and soak have not been run for this final
  Stage 4 implementation.
- Server-side idempotency keys are still not implemented. The Stage 4 client
  marks unknown microbatch outcomes as uncertain instead of blindly replaying
  them under the same attempt.

## Benchmark And Test Gates By Phase

Do not wait until Phase 6 to start measuring. Each phase adds a small benchmark
or stress harness that targets the property introduced in that phase. Early
phase harnesses are mostly correctness and race tests; throughput numbers become
meaningful only after the API, client, and observability contracts are in place.

Every benchmark or stress script should emit a machine-readable report with:

```text
git_sha
phase
backend config
workload shape
worker/process config
request/microbatch ids when applicable
status counts
state-store before/after
worker stats before/after
wall times and latency percentiles for the operations it exercises
success/failure verdict
```

### Phase 2A Benchmark Gate: Cleanup Race Correctness

Add:

- A deterministic fake-worker race harness where a step resolves and pins a
  parent, pauses before child registration, and cleanup is called while the item
  is live.
- A StateStore cleanup harness for pinned states: one pinned token plus one
  unpinned sibling under the same `item_id`.
- A small real Lean smoke run: create one state, step it with a few tactics,
  cleanup, and verify StateStore returns to zero.

Expected success signal:

- Cleanup during live work returns `status="deferred"`, deletes zero states, and
  leaves all states intact.
- Cleanup after the step drains returns `status="deleted"` and leaves
  `count_by_item_id(item_id) == 0`.
- A pinned state makes public cleanup defer without deleting the unpinned sibling.
- No child state can be registered after cleanup reports `deleted`.
- Lifecycle counts return to zero after every successful or failing step.

This is not a throughput benchmark. It proves cleanup is truthful.

### Phase 2B Benchmark Gate: Cancel And Drain

Add:

- A deterministic fake-worker cancel harness with a multi-item lane sharing one
  `item_id`; cancel after the first item starts.
- A cancel polling scenario: cancel active item, wait until drained, then cleanup.
- Metrics in the harness for `cancel_to_drained_ms`, skipped item count, and
  cleanup outcome.

Expected success signal:

- The currently running Pantograph command is allowed to finish or time out.
- Later lane items for that `item_id` return `cancelled` and do not lease workers
  or create states.
- Repeated cancel is idempotent and reports the current `in_flight` count.
- Cleanup is deferred while work is live and deletes everything after drain.

This proves search can abandon an attempt without starting more work for it.

### Phase 3 Benchmark Gate: Caps And Backpressure

Add:

- A cap-boundary harness with tiny configured caps for create items, step items,
  tactics per item, and total tactic attempts.
- A worker-acquire stress harness with `max_pantograph_workers=1`, short
  `acquire_timeout_ms`, and more concurrent requests than the pool can serve.
- A queue-limit stress harness once route-level queue caps exist.

Expected success signal:

- Oversized requests fail with 422 before worker leasing and before StateStore
  writes.
- Worker acquisition contention returns `overloaded` per item or 429/503 for the
  whole request; it never appears as tactic `error`.
- Rejected/overloaded work creates no child state and is safe to retry.
- Reported manager wait time and overload counts explain the observed failures.

This proves load pressure is explicit backpressure, not corrupted proof signal.

### Phase 4 Benchmark Gate: LeanFoundry EnvClient Reliability

Add:

- A fake-server EnvClient harness that injects `overloaded`, 429/503, dropped
  responses, read timeouts, and malformed/missing node results.
- A persisted microbatch resume harness for a 1024-item logical call split into
  64 microbatches of 16, with a simulated crash at microbatch 63.
- A same-`item_id` scheduling harness that queues multiple nodes from one search
  attempt and verifies they are not sent in overlapping HTTP requests.
- A small real-server client smoke run using `/exec/limits`, create, step,
  cleanup, and retry of an observed `overloaded` fake or controlled response.

Expected success signal:

- Observed `overloaded` results are retried unchanged.
- Unknown transport outcomes are marked uncertain; create/step is not blindly
  replayed under the same attempt.
- Resume restarts at the first incomplete/unknown microbatch, not at item 0.
- The client serializes live requests for the same `item_id` while allowing
  different attempts to run concurrently.
- The client derives batch size and in-flight limits from `/exec/limits`.

This is the first point where the Python EnvClient is LeanFoundry-reliable.

### Phase 5 Benchmark Gate: Observability Completeness

Add:

- A stats/metrics validation run that performs create, step, cleanup, cancel,
  overload, and GC paths.
- Benchmark report checks that required fields are present: header group sizes,
  lane distribution, manager wait, cold/warm worker counts, worker PID/RSS,
  lifecycle counts, status mix, and StateStore bytes.

Expected success signal:

- A benchmark report can explain wall time from cold starts, header grouping,
  lane distribution, manager wait, and worker utilization.
- No run is accepted without enough metrics to distinguish "slow because cold
  start" from "slow because saturated" from "slow because header fragmented."

This makes Phase 6 numbers interpretable.

### Phase 6 Benchmark Gate: Whole-System E2E And Soak

Add:

- Real Goedel-derived workloads through current `main` only:

```text
16 items x 8 tactics
200 items x 8 tactics
1024 items split into 64 microbatches of 16
```

- Cold and warm runs.
- An interrupted 1024-item run that resumes near the end.
- A soak run long enough to exercise worker reuse, TTL/GC backstops, and repeated
  cleanup.

Expected success signal:

- No server crashes, worker leaks, or state-store leaks.
- StateStore count/bytes return to baseline after cleanup for every completed or
  abandoned attempt.
- Peak worker count never exceeds configured caps.
- Memory is bounded by worker count, not item count.
- Throughput and latency are reported with cold-start, header-fragmentation, lane
  distribution, and manager-wait context.
- Resume avoids replaying completed microbatches.
- Status mix is explainable: proof `error` means tactic failure, while load
  pressure appears as `overloaded` or request-level backpressure.

This is the point where we can claim the backend works as a system.

## Production Roadmap

The next implementation slice is Phase 5: observability and health. Phases 2A,
2B, 3, and 4 are implemented and covered by focused tests. Do not call the backend
production-ready until Phase 5 metrics make benchmark results interpretable and
Phase 6 completes the real Goedel E2E/soak gate.

Phase 2A is done only when:

```text
1. create_states and step_batch both register in-flight work by item_id
2. cleanup(active item_id) returns deferred and deletes nothing
3. cleanup(quiescent item_id) deletes every state for that item_id
4. no child state can be registered after cleanup reports deleted
5. a pinned state causes public cleanup to defer without partial deletion
6. tests prove the resolve/pin/lifecycle begin ordering has no await gap
```

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

Status: implemented.

Phase 2A -- item lifecycle registry + deferred cleanup.

- Add an in-memory per-`item_id` registry (`status`, `in_flight_count`,
  `terminal_expires_at`) in a dedicated server module.
- Add the minimal wire schema needed for this phase: `cancelled` exec status and
  cleanup `deleted/deferred` results.
- Register both `create_states` and `step_batch` work; bracket each request with
  try/finally and decrement only after all StateStore writes (`create_child`, pin
  release) complete.
- For `step_batch`, resolve/pin the parent state and begin lifecycle tracking in
  one synchronous block with no `await` between them.
- `cleanup` returns `deferred` (deletes nothing) while `in_flight_count > 0`,
  otherwise deletes all states and marks `cleaned`. Never partial-delete, never
  report success with state remaining.
- Public cleanup uses the all-or-nothing StateStore delete path; pinned tokens
  defer cleanup instead of causing partial deletion.
- Reject new create work for `cancelling`/`drained`/`cleaned` items with
  `cancelled`.
- Reject new step work for `cancelling`/`drained`/`cleaned` items with
  `cancelled` when the parent token still resolves; return `invalid_state_token`
  if cleanup already deleted the parent token.
- Evict terminal records after `terminal_retention_seconds`; keep the registry
  in-memory (no rehydration).

Phase 2B -- strongly useful: `POST /exec/cancel` with cooperative draining.

- Mark `cancelling`, stop starting new work, let the current Pantograph command
  finish; the lane loop checks `cancelling` before each item to bound drain.
- Repeated cancel is idempotent and reports `in_flight` for polling.
- Document and test the v0 drain bound: cancel does not interrupt an active
  tactic command or worker acquisition; those drain at `step_timeout_ms` or
  `acquire_timeout_ms`.

Not now: deletion tombstones that suppress child creation so cleanup can delete
before work drains. Only needed if we require hard immediate cancellation.

Acceptance:

- `cleanup` for an `item_id` either deletes everything for it or returns
  `deferred`; it never returns success while state remains.
- After an attempt drains, `cleanup` brings its state-store count to zero,
  including children produced by racing work.
- A late `create_states` for a `cleaned` / `cancelling` `item_id` returns
  `cancelled`, not new state.
- A late `step_batch` for a `cancelling` item returns `cancelled`; after cleanup
  deletes the parent token, a late step returns `invalid_state_token`.
- GC is exercised only as a backstop (client never retries cleanup), not as the
  normal path.

Phase 2A tests:

- Unit-test lifecycle transitions: active count increments/decrements,
  cleaned rejects future begins, terminal records expire after the retention
  window.
- Unit-test cleanup while active: with `in_flight_count > 0`, cleanup returns
  `status="deferred"` and does not call any StateStore delete method.
- StateStore unit test: all-or-nothing cleanup with any pinned token returns
  deferred and leaves every token for the item in place.
- Unit-test cleanup after drain: it calls StateStore once, returns
  `status="deleted"`, and marks the item cleaned.
- Exec fake test: a step whose child creation is delayed keeps `in_flight_count`
  nonzero until after `create_child` completes.
- Exec fake test: `resolve_and_pin` followed by lifecycle begin has no `await`
  gap; cleanup cannot observe a pinned-but-untracked item.

Phase 2B tests:

- Unit-test cancel transitions: active becomes cancelling, cancelling becomes
  drained at zero, and repeated cancel is idempotent.
- API test: cancel during a multi-item lane causes later same-`item_id` lane
  items to return `cancelled` while the current item drains normally.

### Phase 3: API Caps And Backpressure

Status: implemented.

- Add create-state request caps.
- Split `timeout_ms` into `acquire_timeout_ms` and `step_timeout_ms` and validate
  both (see Timeouts).
- Add an `overloaded` exec status and return it (or 429/503) when a worker cannot
  be leased within the acquire budget, instead of per-item `error`.
- Add `GET /exec/limits` so the client can derive microbatch shape from server
  caps and recommendations instead of hardcoding it.
- Add route/backend-level in-flight and queued request limits.
- Return clear 422 for invalid request size and 429/503 for overload.
- Add tests for cap boundaries and overload behavior, including that acquisition
  contention never surfaces as `error`.

Acceptance:

- Oversized requests are rejected before worker leasing.
- Load beyond configured queue capacity fails predictably with retryable
  backpressure, not as failed tactics.
- Manager worker cap is not the only load-control mechanism.
- `GET /exec/limits` includes hard caps, recommendations, and
  `same_item_id_pipelining=false`.

Tests:

- Schema tests cover `overloaded`, `cancelled`, split timeout fields, deprecated
  `timeout_ms` aliasing, cleanup `deleted/deferred`, cancel, and limits models in
  both server and client model copies.
- Route tests reject create and step requests over item/tactic/attempt caps before
  `PantographManager.get_worker` is called.
- Fake manager tests force acquire timeout and verify per-item `overloaded`
  results, not `error`.
- Queue-limit tests force request-level overload and verify 429/503 with no worker
  lease and no state-store writes.

### Phase 4: Client-Side Exec Semantics

Status: implemented.

- Stop blind automatic retries for non-idempotent `/exec/create_states` and
  `/exec/step_batch`, or add idempotency keys before retrying them. Follow the
  Retry Safety By Outcome taxonomy: `overloaded` items retry as-is; items with an
  unknown transport outcome must be abandoned and cleaned before retry; observed
  terminal results are not retried.
- Replace the static batcher knobs with derived microbatch sizing: size to
  `recommended_items_per_step_batch` from `GET /exec/limits`, flush on
  size-or-drain, and bound in-flight using
  `recommended_in_flight_step_batches` plus `overloaded` / 429 as the
  backpressure signal.
- Enforce `same_item_id_pipelining=false`: do not have two live step requests for
  the same attempt unless the server later advertises that it is supported.
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
- Overloaded items are retried automatically without creating duplicate child
  states; executed-but-lost items are marked uncertain instead of blindly
  retried.
- The batcher can have multiple HTTP calls in flight for different attempts, but
  serializes calls that contain the same `item_id`.

Tests:

- Client unit test: limits response configures batch size and in-flight bound; no
  fixed default silently overrides server caps.
- Client unit test: `overloaded` results are retried unchanged, but unknown
  transport failures mark the microbatch uncertain and do not auto-retry
  create/step.
- Resume test: persisted responses for microbatches 0..62 are reused and only
  microbatch 63 is rerun or abandoned.
- Same-item scheduling test: two queued nodes from the same `item_id` are not sent
  in overlapping HTTP requests.

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
