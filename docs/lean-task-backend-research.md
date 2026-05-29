# Lean Task Backend Research

Date: 2026-05-27

Goal: support at least one `/exec/step_batch` request containing 16 proof-state
items with 8 tactics each, i.e. 128 tactic attempts, without running one
Mathlib-loaded OS process per item.

## Backend Migration Progress

Status is tracked here while the migration is active. Keep this section current
before returning to broader updates in `docs/backend-end-plan.md`.

| Step | Status | Notes |
| --- | --- | --- |
| Repo-local PyPantograph fork | Done | `pantograph` resolves from `third_party/PyPantograph` via uv editable source. |
| File-backed `CompactedRegion` ownership | Done | `goal.load` regions are retained and freed on the next command after delete/reset. |
| Lean-side `goal.step_batch` | Done | File-backed parents, one task per item, sequential tactics per item, child state saving, and deferred parent-region cleanup are implemented in the fork. |
| PyPantograph `goal_step_batch_async` wrapper | Done | Thin one-command/one-response wrapper exists in the forked Python client. |
| StateStore backend metadata and pinning | Done | Tokens carry `backend_kind` and `state_format`; `/exec` pins resolved parents while Lean work is in flight. |
| `/exec` backend seam | Done | `pantograph_pool` remains the default; `pantograph_task` is opt-in and groups compatible items into worker chunks. |
| Caps and tests | Done | Config caps reject oversized requests; tests cover grouping, incompatible headers, per-command child output isolation, stale batch-dir GC, pool fallback, invalid tokens, cleanup, route e2e for both backends, and direct 16 x 8 one-process stepping. |
| Runtime metrics | Done | `/exec/stats` reports backend settings, effective worker caps, pool occupancy, worker PIDs/RSS, and state-store totals; the Goedel benchmark samples process-tree RSS and `/exec/stats`. |
| Goedel task-backend stress benchmark | Done | `examples/pantograph_step_benchmark.py --launch-server --exec-backend pantograph_task` exercises the real HTTP path with 16 items x 8 tactics in one worker process and asserts backend mode, worker caps, and cleanup. |
| Batch failure isolation | Done | Lean task failures are converted to per-item error results; one timeout no longer poisons an entire compatible chunk. |
| Default backend flip | Pending | `pantograph_pool` remains the default until repeated warm saturation runs and trainer integration needs are validated. |

Current implementation checkpoint, 2026-05-29:

- Lean fork: `third_party/PyPantograph/src/Pantograph/Protocol.lean` defines
  the `goal.step_batch` ABI, and `third_party/PyPantograph/src/Repl.lean`
  dispatches it.
- Python wrapper: `third_party/PyPantograph/pantograph/server.py` exposes
  `goal_step_batch_async(...)`.
- Server seam: `server/exec_backends.py` owns the pool/task backend selection,
  caps, grouping, chunking, worker return, and child-token promotion.
- State lifetime: `server/state_store.py` persists backend metadata and pins
  active parent tokens so cleanup/GC cannot delete files after resolution but
  before Lean loads them.
- Settings: `exec_backend` defaults to `pantograph_pool`; `pantograph_task`
  remains available as an opt-in backend until repeated warm saturation runs and
  trainer integration needs are validated.
- Benchmarking: `examples/pantograph_step_benchmark.py` can launch a configured
  benchmark server, force `pantograph_task`, cap one Lean process per
  `env_profile`, prewarm the pool, sample `/exec/stats`, sample process-tree
  RSS, and fail if backend mode, worker caps, or scoped cleanup do not hold.
- Operational fix: `StateStore` canonicalizes `root_dir` to an absolute path so
  saved state files can be loaded by Pantograph subprocesses whose working
  directory is the Mathlib project.
- Lean failure isolation: `goal.step_batch` now turns a failed item task into
  error results for that item only instead of throwing the whole chunk.

Verified tests:

```text
uv run pytest -q
SSL_CERT_FILE=$(uv run python -c 'import certifi; print(certifi.where())') \
  uv run pre-commit run --all-files
uv run pyright --project=pyrightconfig.json
uv run mypy --config-file=pyproject.toml --cache-dir=.mypy_cache \
  --show-error-codes
```

`uv run pytest -q` passed 136 tests with 103 deselected by the repo's default
markers. The run includes the direct `goal_step_batch_async` 16 items x 8
tactics capacity test in one Pantograph process and the real
`/exec/create_states -> /exec/step_batch -> /exec/cleanup` route test under both
`pantograph_pool` and `pantograph_task`.

Real Goedel stress benchmark, 2026-05-29:

```sh
uv run python examples/pantograph_step_benchmark.py \
  --launch-server \
  --server-port 8011 \
  --exec-backend pantograph_task \
  --n-proofs 16 \
  --items-per-request 16 \
  --tactics-per-item 8 \
  --max-replay-depth 1 \
  --concurrency 4 \
  --timeout-ms 180000 \
  --max-pantograph-workers 2 \
  --max-lean-processes-per-env-profile 1 \
  --max-items-per-worker-batch 16 \
  --max-parallel-items-per-lean-process 16 \
  --prewarm-proofs 1 \
  --max-rows-scanned 1000 \
  --state-store-dir .cache/pantograph_benchmark/state-task-stress \
  --output .cache/pantograph_benchmark/results_task_stress.json
```

Observed result:

| Metric | Value |
| --- | ---: |
| Step items | 16 |
| Tactic attempts | 128 |
| Status mix | 40 open, 13 complete, 91 error |
| `max_total_workers` | 1 |
| `max_workers_by_env_profile["lean4.29.1_mathlib"]` | 1 |
| Peak process-tree RSS | 2583.7MB |
| Final process-tree RSS | 1673.4MB |
| Step batch latency | 22705.9ms |
| Cleanup | 40 states / 2043904 bytes deleted |
| Scoped state-store before/after | 0 states / 0 states |
| Final backend | `pantograph_task` |

This run is the first committed end-to-end evidence for the intended two-level
shape: a bounded process pool with one warm Mathlib/Pantograph process for the
profile, and item-level Lean tasks inside that process for a 16 x 8 batch. The
errors are real Lean tactic outcomes from the mined workload; sibling successes
survive timeouts and failed distractors.

The legacy `/api/check` test path is green again after restoring the local REPL
setup: `setup.sh` now builds `repl` by default and forces its `lean-toolchain`
to the same `v4.29.1` used by Mathlib and Pantograph. The default Lean version
reported by the server is also `v4.29.1`.

The target memory shape is bounded by a fixed small number of warm Lean
processes per `env_profile`, ideally one:

```text
peak_rss ~= warm_mathlib_process
          + bounded Lean task workspaces
          + bounded resident state cache
          + StateStore files
```

The design fails if memory scales like:

```text
peak_rss ~= in_flight_items * warm_mathlib_process
```

## Current Evidence

Measured locally with Mathlib loaded:

| Shape | Approx RSS | Conclusion |
| --- | ---: | --- |
| Direct `lake env lean` importing Mathlib and sleeping | 1.16GB child, 1.22GB including `lake` | Plain Lean import is the lower baseline. |
| PyPantograph with `imports=["Mathlib"]` | 3.29GB ready, 3.27GB after `load_sorry` + `simp` | Current Pantograph worker cost is about 3GB. |
| LeanInteract REPL after `import Mathlib` and one proof step | 3.03GB | Stock REPL proof-state interaction has the same order of cost. |

So the issue is not only Pantograph. A pool of Mathlib-loaded proof-state REPL
processes is still too expensive if pool size tracks in-flight proof states.

The current Goedel `/exec` benchmark did prove correctness of the lifecycle:
create states, apply tactics, produce child states, return errors for invalid
tactics, and cleanup files. Its throughput numbers are not useful because the
run was dominated by cold Mathlib loading.

First falsifiable item-task spike, run against a scratch copy of pinned
Pantograph on 2026-05-28:

- one Lean process loaded parent state files, ran one task per item, tried 8
  tactics sequentially per item, saved open children, reloaded those children,
  and stepped them again;
- `Init`, 16 items x 8 tactics x 50 trials passed;
- `Mathlib`, 16 items x 8 tactics x 1 trial passed;
- Mathlib 16 x 8 peak RSS was about 3.36GiB, not `16 * 3GB`.

This does not prove production readiness. It only says the item-level task shape
is not immediately falsified by light tactics. The remaining blockers are region
ownership, heavier realization-collision tactics, timeouts, and resource caps.

## File-Backed State Memory

There are two different cleanup paths in Pantograph:

```text
process-local stateId cleanup:
  Python GoalState object disappears
  -> PyPantograph queues stateId
  -> server.gc_async sends goal.delete
  -> Pantograph erases that id from State.goalStates

file-backed state cleanup:
  goal.save writes a GoalState to a .bin file
  goal.load reads that file back into the process
  -> Lean also returns a CompactedRegion backing the loaded object graph
```

The current durable `state_token` design relies on the second path because
tokens map to saved state files, not process-local `stateId`s. This is the
reason `CompactedRegion` matters. It is the memory block created when Lean loads
a saved proof-state file. A loaded `GoalState` can reference that block.

Upstream Pantograph's current `goal_load` implementation does this:

```lean
let (goalState, _) ← goalStateUnpickle args.path (background? := .some $ ← getEnv)
let id ← newGoalState goalState
```

The underscore discards the region handle. Pantograph's `Serial.lean` explicitly
says ignoring the returned `CompactedRegion` leaks memory. This is separate from
normal `goal.delete` cleanup: deleting the `stateId` removes the `GoalState`
from the map, but does not by itself prove the backing region from `goal_load`
was released.

Practical rule for the task backend:

- do not manually free regions early while any loaded `GoalState`, child state,
  serialized goal output, or Lean monadic state may still reference them;
- measure whether repeated `goal_load -> goal.delete/gc` grows RSS in current
  Pantograph;
- if it grows, patch the Pantograph fork so a loaded resident state owns its
  optional region and `goal.delete` frees that region after removing the state;
- until that patch is proven, keep worker recycling and RSS limits as the
  safety fallback.

Measured on current pinned Pantograph with one tiny Init state:

```text
goal_start -> goal.delete, 5000 iterations:
  RSS rose once by about 5MB, then stayed flat.

goal_load -> goal.delete, 5000 iterations:
  RSS grew by about 112MB.
```

That confirms the growth is specific to file-backed `goal_load`, not normal
process-local state churn.

Scratch fork resolution:

1. Add `goalStateRegions : HashMap Nat CompactedRegion` to the REPL state.
2. Make `goal_load` store the region under the new resident state id.
3. Make `goal_delete` remove the region from the map, but only move it into a
   `releasedGoalStateRegions` queue.
4. At the start of the next command, free queued regions after the previous
   command frame has returned.

Freeing inside the same `goal_delete` command crashed, because local values in
that command frame can still reference the loaded object graph. Deferring until
the next command avoided that crash.

Measured with the scratch fork:

```text
goal_load -> goal.delete, 5000 iterations:
  RSS stayed within about +1.5MB.
```

This should be the Pantograph-fork fix unless a deeper Lean API offers a cleaner
region-owner abstraction.

## Current Local Fork Slice

As of 2026-05-28, the repo uses a source fork at
`third_party/PyPantograph`, copied from the previously pinned
`stanford-centaur/PyPantograph@ffa7f243824d2762825abddb1e9f6e939ede761f`.
The root `pyproject.toml` points `pantograph` at that path as an editable uv
source.

The fork currently contains both source-level migration fixes:

- `src/Repl.lean` tracks file-loaded `CompactedRegion`s by resident
  `stateId`;
- `goal.load` stores the region instead of discarding it;
- `goal.delete` and `reset` move deleted regions into a deferred-release queue;
- the next command frees that queue before dispatching new work.
- `src/Pantograph/Protocol.lean` defines the `goal.step_batch` request and
  result ABI;
- `src/Repl.lean` dispatches `goal.step_batch`, loads file-backed parents
  inside item tasks, tries tactics sequentially per item, saves open children,
  and queues parent `CompactedRegion`s for deferred release.

Generated artifacts stay local:

- `third_party/PyPantograph/src/.lake/`
- `third_party/PyPantograph/pantograph/pantograph-repl`
- `third_party/PyPantograph/pantograph/lean-toolchain`

After editing Lean files in the fork, rebuild with:

```sh
cd third_party/PyPantograph
uv run python build-pantograph.py
```

Then from the repo root:

```sh
UV_HTTP_TIMEOUT=120 uv lock
UV_HTTP_TIMEOUT=120 uv sync --frozen
```

Current verification for this slice:

- `uv lock` resolves `pantograph` as `source = { editable =
  "third_party/PyPantograph" }`;
- `uv sync --frozen` replaces the upstream git package with the local path;
- `import pantograph` resolves to
  `third_party/PyPantograph/pantograph/__init__.py`;
- 5,000 `goal_load -> goal.delete` iterations on a tiny Init state ended with
  RSS delta `-393216` bytes from baseline, instead of the prior
  roughly `+112MB` growth;
- direct `goal_step_batch_async(...)` tests pass, including 16 items x 8
  tactics in one Pantograph process with `maxParallelItems = 16`;
- real HTTP stress benchmark passes for 16 Goedel proof states x 8 tactics with
  `pantograph_task`, one worker process for `lean4.29.1_mathlib`, and scoped
  cleanup back to zero;
- full repo tests pass with the default marker selection:
  `uv run pytest -q`;
- pre-commit passes with ruff, pyright, and mypy:
  `uv run pre-commit run --all-files`;
- the maintained typecheck scope is the active server plus the exec client
  models; archived `server/server_old`, broad client infotree utilities,
  examples, and perf/match tests are outside that hook scope.

The next slice should not re-prove the 16 x 8 one-process shape from scratch.
That has now passed both the scratch `.cache/pantograph-concurrency-spike`
checks and the real `/exec` stress path. The remaining work is repeated warm
saturation, trainer-shaped multi-request traffic, and deciding whether
`pantograph_task` should become the default backend.

## Migration Plan

The migration target is not "replace Pantograph everywhere in one jump." The
public `/exec` API and `StateStore` remain stable. The implementation order was:

1. Productize the scratch item-task primitive inside the local Pantograph fork.
2. Add a thin PyPantograph wrapper for that command.
3. Insert a backend abstraction behind `server/routers/exec.py`.
4. Keep the current one-worker-per-item behavior as `pantograph_pool`.
5. Add `pantograph_task` behind a config flag and route compatible chunks to one
   `goal.step_batch` command per leased worker.
6. Harden concurrency, timeouts, caps, metrics, and real-data benchmarks before
   making `pantograph_task` the default.

This order is intentional: the one-process item-task feasibility result already
exists, but the server cannot use it until `goal.step_batch` exists as a real
forked Pantograph command.

### Migration Invariants

These must not change during the migration:

- `/exec/create_states`, `/exec/step_batch`, and `/exec/cleanup` public schemas.
- `state_token` as an opaque backend-owned handle.
- `StateStore` metadata ownership: token -> item id, env profile, header hash,
  backend kind, state format, saved state path, timestamps, byte size.
- Search/trainer responsibility for `item_id`, `node_id`, graph ownership, and
  cleanup timing.
- Response normalization: public callers see `open`, `complete`, `error`, or
  `invalid_state_token`, not Pantograph or REPL internals.

These should change:

- `/exec/step_batch` should stop leasing one worker per item.
- `/exec/create_states` should eventually use the same small fixed process set;
  otherwise root-state creation reintroduces the process-per-item multiplier.
- Worker parallelism should move from Python/process scheduling into one Lean
  process via one Lean-side batch command.
- The Pantograph pool should become only one selectable backend, not the route's
  architecture.

### Phase 0: Freeze The Current Backend As A Compatibility Backend

Current route behavior:

- `server/routers/exec.py` resolves each `state_token` inside `step_one`.
- It calls `asyncio.gather(*(step_one(item) for item in request.items))`.
- Each valid item calls `PantographManager.get_worker`.
- The leased worker loads one parent state and applies that item's tactics.

This is bounded by `max_workers`; it does not spawn an unbounded process per
request item. The scalability problem is narrower: throughput increases only by
occupying more Mathlib-loaded workers, and each worker carries the large warm
process RSS. First, wrap this behavior behind a compatibility backend rather
than deleting it.

Add a small internal backend interface, for example `server/exec_backend.py`:

```python
@dataclass(frozen=True)
class ResolvedStepItem:
    request_index: int
    node_id: str
    parent_token: str
    parent_path: Path
    tactics: list[str]
    timeout_ms: int

@dataclass(frozen=True)
class BackendStepResult:
    tactic: str
    status: ExecStatus
    child_path: Path | None = None
    goals: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

class ExecBackend(Protocol):
    async def step_batch_group(
        self,
        *,
        env_profile: str,
        header: str,
        header_hash: str,
        items: list[ResolvedStepItem],
        state_dir: Path,
    ) -> dict[int, list[BackendStepResult]]: ...
```

Then implement:

- `PantographPoolExecBackend`: exact current behavior, including one worker lease
  per busy item under the existing `max_workers` bound. This makes the refactor
  behavior-preserving.
- `PantographTaskExecBackend`: the future backend, initially hidden behind a
  config flag.

The route becomes backend-agnostic:

1. Resolve all tokens up front.
2. Return `invalid_state_token` immediately for missing tokens.
3. Group valid items by `(env_profile, header_hash, backend_kind,
   state_format)`.
4. Split each group into chunks no larger than
   `max_items_per_worker_batch`.
5. Submit compatible chunks through the existing bounded free/busy worker pool.
   At most `max_workers` chunks may hold processes at once. If every item has a
   different header, batching cannot merge those items, but the scheduler still
   queues them instead of spawning one process per item.
6. Call `backend.step_batch_group(...)` once per leased compatible chunk.
7. Promote `child_path`s with `StateStore.create_child(parent_token, child_path)`.
8. Merge group/chunk results back into original request order.

This one route refactor is the real migration seam. Once it exists, Pantograph
pool, Pantograph task backend, and a REPL fork can all plug into the same public
API.

Acceptance for Phase 0:

- Existing `/exec/create_states -> /exec/step_batch -> /exec/cleanup` tests pass
  with `exec_backend=pantograph_pool`.
- New route tests prove mixed valid/invalid tokens preserve input order.
- New grouping test proves two compatible items are passed to the backend in one
  group rather than independently scheduled by the route.
- New fragmented-header test proves 16 incompatible headers are queued through
  `max_workers`, not treated as an unbounded set of simultaneous worker leases.
- `StateStore` records `backend_kind` and `state_format` from the start, and
  `resolve()` rejects tokens that cannot be loaded by the active backend.

### Process Pool And Batch Chunks

The new backend does not remove the worker pool. It changes the unit leased from
the pool.

Current prototype:

```text
one step item -> one worker lease -> one Pantograph process
```

Task backend:

```text
one compatible batch chunk -> one worker lease -> one Lean/Pantograph process
```

If a request contains 64 compatible items and
`max_items_per_worker_batch = 16`, the scheduler can use four workers, with each
worker running one 16-item Lean-side batch command. If a request contains 16
items with 16 different headers and `max_workers = 4`, the scheduler runs four
single-item chunks at a time and queues the rest. The memory bound remains
`max_workers * warm_process_rss`; batching improves throughput only when items
share a compatible environment.

For training, this means header discipline matters. Prefer canonical
dataset/env-profile headers such as a shared Mathlib import profile. Tiny
problem-specific headers fragment batches and reduce the benefit of Lean-side
item parallelism.

The caps have distinct jobs:

| Cap | Layer | Meaning |
| --- | --- | --- |
| `max_items_per_step_batch` | HTTP/API | Maximum items accepted in one public `/exec/step_batch` request. |
| `max_tactics_per_step_item` | HTTP/API | Maximum candidate tactics per proof-state item. |
| `max_attempts_per_step_batch` | HTTP/API | Maximum `items * tactics` accepted before touching Lean. |
| `max_items_per_worker_batch` | Scheduler | Maximum compatible items sent to one leased process command. |
| `max_parallel_items_per_lean_process` / `maxParallelItems` | Lean command | Maximum item tasks running at once inside one process. Must be `<= max_items_per_worker_batch`. |
| `max_lean_processes_per_env_profile` | Process pool | Upper bound on warm processes for one compatible environment. |

The one-process target also creates head-of-line blocking: the Pantograph
transport remains one command -> one response per process, so a 16 x 8 batch
holds that process until the whole command returns. If same-profile latency
matters, the mitigation is a measured small process cap such as 2-4, smaller
worker chunks, or both. That is a latency tradeoff against the memory goal, not
a reason to return to process-per-item scheduling.

### Phase 0.5: Concurrency Evidence And Remaining Gates

The basic one-process item-task shape has already passed a scratch spike: 16
file-backed parent items, 8 tactics per item, one task per item, sequential
tactics inside each item, `Core.wrapAsync`, `Runtime.markMultiThreaded`, and
Mathlib loaded in one Pantograph process. Peak RSS was about 3.36GiB, not
`16 * warm_process_rss`.

That scratch result is enough to justify porting the item-task primitive into
the fork. It is not enough to make the task backend default. These points still
need committed tests and code-level decisions against Lean 4.29.1:

1. Shared environment safety.
   - Imported declarations live in persistent compacted `.olean` regions, but
     the runtime `Environment` spine is ordinary heap state.
   - If item tasks share one `Environment`, call
     `IO.Runtime.markMultiThreaded` on the shared context before `IO.asTask`,
     and microbenchmark its cost after `import Mathlib`.
   - Prefer a per-item isolated environment branch if Lean's async elaboration
     machinery supports it.
2. Environment mutation and realization.
   - Tactics can force realization of match equations, instances, recursors, or
     other constants.
   - Audit `Environment.realizeConst`, `addDecl`, async constants, and the
     unpickled Pantograph environment path to decide whether concurrent
     realization on a shared environment is safe.
3. Async elaboration machinery.
   - The scratch path used `Core.wrapAsync` around item execution, then
     `.asTask` on the wrapped item action. Product code should keep this shape
     unless a stronger isolated-environment branch is found.
   - Do not use raw shared-env `IO.asTask` for tactic execution.
4. Concurrent pickling.
   - `goalStatePickle` compacts object graphs. Test whether 16 item tasks can
     pickle children concurrently while referencing the shared imported
     environment.
   - If concurrent compaction is unsafe, join item tasks with child states still
     alive, pickle serially on the main loop, then free parent regions. That
     fallback must be measured because it raises peak memory.

Decisive micro-tests before making performance claims about `pantograph_task`:

- Realization collision: run two or more concurrent items whose tactics force
  the same generated constant or instance, repeat enough times to catch
  nondeterministic corruption.
- Concurrent pickle: produce 16 open child states, pickle them simultaneously,
  then load and step every child.
- `markMultiThreaded` cost: measure wall time and RSS for marking the post-Mathlib
  batch context, then compare single-thread tactic latency before and after.

The current implementation model is the scratch fallback:

- shared environment plus mandatory `markMultiThreaded` on the batch context;
- `Core.wrapAsync` for each item task;
- bounded `maxParallelItems`;
- no mutation of `State.goalStates` from item tasks.

If the remaining stress tests fail under that model, either switch to a
per-item isolated environment branch or reject the one-process task backend. A
Lean REPL fork is not an automatic escape hatch because it uses the same Lean
runtime and compaction classes unless it introduces a different isolation
strategy.

### Phase 1A: Port The Scratch Item-Task Primitive Into The Fork

The repo-local fork already exists at `third_party/PyPantograph`, and
`pyproject.toml` already points `pantograph` at it. Do not return to editing uv
cache checkouts.

The next code slice is to add `goal.step_batch` to the fork by porting the
scratch item-task model:

1. Parse a batch request.
2. Chunk items by `maxParallelItems`.
3. For each item task, load exactly one parent state from `parentPath`.
4. Loop over that item's tactics sequentially, always from the same parent.
5. Save open children to unique files under `outputDir`.
6. Return one JSON response in deterministic `(itemIdx, tacticIdx)` order.
7. Preserve a debug path with `maxParallelItems = 1`; this is for isolating
   bugs, not the target implementation.
8. Track every `CompactedRegion` returned by `goalStateUnpickle`. The scratch
   spike deliberately leaked those regions; committed code must keep parent
   regions alive through tactic execution, child save, and goal serialization,
   then defer freeing until the next command.

For the first implementation, support only file-backed parents:

```text
GoalStepBatchItem.parentPath : System.FilePath
```

Do not accept resident `stateId` parents yet. Resident ids would pull
`State.goalStates` and `goalStateRegions` into the concurrent path, which is
exactly the shared mutable state this slice is trying to avoid. Public
`state_token`s already resolve to files, so file-backed parents are sufficient
for `/exec`.

Python side:

- Add `goal_step_batch_async(...)` to the forked PyPantograph wrapper.
- Keep the wrapper as one-command/one-response over stdin/stdout. Do not issue
  overlapping Python calls into one Pantograph process.
- Add direct wrapper tests before changing `server/routers/exec.py`.

Acceptance for Phase 1A:

- `maxParallelItems = 1` matches repeated `goal.tactic` on Init examples.
- Candidate tactics are independent: for example, in a parent where
  `intro hp` would create a local hypothesis, a later candidate `exact hp`
  must fail if it is run as a separate candidate from the original parent.
- `maxParallelItems > 1` runs 2, 4, 8, then 16 independent parent items in one
  Pantograph process.
- 16 items x 8 tactics works against the same light Mathlib workload used in
  the scratch spike, with memory metrics recorded.
- Python can call `goal_step_batch_async(...)` directly and get child files that
  reload and step later.
- One compatible multi-item request uses one Pantograph subprocess and one
  stdin/stdout command, not one command per item and not one process per item.
- Repeated file-backed parent loads in `goal.step_batch` do not show unbounded
  RSS growth, or the worker is recycled under a documented temporary
  RSS/load-count cap while the region-lifetime bug is fixed.

### Phase 1B: Implement Item-Level Lean Task Parallelism In `goal.step_batch`

Use the scratch implementation model as the starting point: shared batch context
marked with `Runtime.markMultiThreaded`, `Core.wrapAsync` around item execution,
and bounded task fanout. Raw `IO.asTask` over a shared, unmarked `Environment`
is not an acceptable implementation.

The parallelism unit is the step item, not the tactic. A 16 item x 8 tactic
request should become up to 16 Lean tasks in one process. Each item task owns one
parent proof state and loops over that item's 8 tactics sequentially.

For current `/exec/step_batch` semantics, the 8 tactics are candidate actions
from the same parent state, not a tactic script. Sequential inside an item means:

```text
for tactic in item.tactics:
  run tactic against the same parent state
  save/return that tactic's child if it is open
```

It does not mean tactic 2 starts from tactic 1's child.

Start with this item-task model:

- Chunk items by `maxParallelItems`; do not spawn all 16 items unless the cap
  allows it.
- Each item task loads exactly its parent state from the file path decoded from
  `state_token`.
- Each item task keeps that parent state private to the task and applies its
  tactics sequentially.
- Do not mutate `State.goalStates` from tasks.
- Do not print or write JSON from tasks.
- Each task writes open child states to unique output paths and returns only
  serializable result data: item index, tactic index, status, child path, goals,
  and messages.
- The main command loop joins item tasks, sorts by `(itemIdx, tacticIdx)`, and
  serializes one JSON response.

This is simpler than tactic-level fanout and is the actual target. It avoids
sharing one parent `GoalState` across multiple tasks because the parent is owned
by one item task. The only shared values are the imported environment/options and
batch context.

Only after item-level concurrency passes should we consider tactic-level fanout
within one item. That is not required for the 16 x 8 minimum backend capacity.

### Pantograph Item Task Machinery

Patch Pantograph in these places:

1. `src/Pantograph/Protocol.lean`
   - Add `GoalStepBatch`, `GoalStepBatchItem`, `GoalStepBatchResult`, and
     per-tactic result structures.
   - A batch item carries `itemIdx`, `parentPath`, `tactics`, optional `goalId`,
     and optional timeout.
   - The batch carries `outputDir` and `maxParallelItems`.
   - Do not add result fields literally named `error`. `Protocol.lean` reserves
     top-level `error` for `InteractionError`, and the Python wrapper currently
     treats `"error" in result` as command failure. Use fields such as
     `status`, `messages`, `parseError?`, or `failure?` for per-attempt
     failures.

2. `src/Repl.lean`
   - Add command dispatch: `"goal.step_batch" => run goal_step_batch`.
   - Implement `goal_step_batch` as one stdin command -> one JSON response.
   - Capture immutable batch context on the main loop: current `env`, scope,
     options, and `Core.Context` settings.
   - Apply the Phase 0.5 concurrency decision before spawning tasks: either
     construct per-item isolated environment branches, or mark the shared batch
     context multi-threaded and use only APIs proven safe under that model.

3. Extract a non-mutating tactic helper from current `goal_tactic`
   - Do not call the existing `goal_tactic` command from tasks because it reads
     and mutates `State.goalStates`.
   - Extract the site-selection and tactic-running part into a helper that takes
     an explicit `GoalState` and returns a raw result:

```lean
runTacticOnGoalState :
  BatchContext ->
  GoalState ->
  Site ->
  String ->
  CoreM RawTacticResult
```

The helper is the same machinery used by current `goal_tactic`:

- select `Site` from `goalId` and automatic mode;
- run `goalState.tryTactic site tactic` inside a local `TermElabM`;
- serialize goals with `nextGoalState.serializeGoals`;
- serialize messages;
- compute `hasSorry` and `hasUnsafe`;
- return `open`, `complete`, or `error` with the child `GoalState` only long
  enough to save it.

Concrete extraction points from current `Repl.lean`:

- `resolveSite(goalState, goalId?, autoResume?, options)` from the first part of
  `goal_tactic`;
- `runTacticAttempt(...)` from the `goalState.tryTactic` action branch;
- `serializeAttemptResult(...)` from the success/failure formatting path,
  including the existing `hasSorry` and `hasUnsafe` rejection logic.

Put dispatch in `Repl.lean`, but move bulky non-dispatch helpers into a small
Pantograph module if the implementation starts making `Repl.lean` harder to
read.

4. Add an item runner:

```lean
runStepItem :
  BatchContext ->
  GoalStepBatchItem ->
  CoreM GoalStepBatchItemResult
```

Its shape is:

```lean
def runStepItem ctx item := do
  let (parent, region) ← goalStateUnpickle item.parentPath (background? := some ctx.env)
  let mut results := #[]
  for h : tacticIdx in [:item.tactics.size] do
    let tactic := item.tactics[tacticIdx]
    let raw ← runTacticOnGoalState ctx parent item.site tactic
    let result ← match raw with
      | .open child goals messages =>
          let childPath := childPathFor ctx.outputDir item.itemIdx tacticIdx
          goalStatePickle child childPath (background? := some ctx.env)
          pure { status := "open", childPath? := some childPath, goals, messages }
      | .complete messages =>
          pure { status := "complete", childPath? := none, goals := #[], messages }
      | .error messages =>
          pure { status := "error", childPath? := none, goals := #[], messages }
    results := results.push { tacticIdx, tactic, result }
  pure { itemIdx := item.itemIdx, results, parentRegion := region }
```

This sketch is not yet a correct region-ownership implementation. The
production version must not free `region` inside the item task. The item task
should return only JSON-serializable attempt data plus a non-JSON parent-region
handle to the main command. The main command should build the final response,
append all returned parent regions to `releasedGoalStateRegions`, and let the
existing deferred-release hook free them at the start of the next command.

If Lean's type constraints make returning a `CompactedRegion` from item tasks
awkward, use an equivalent command-local owner structure. The invariant is what
matters: parent regions are alive through child save and goal serialization, no
Lean object that references the parent escapes into JSON, and actual freeing is
deferred out of the command frame. A temporary RSS/load-count recycle cap is
acceptable only as a short-lived fallback while this owner structure is fixed.

The important details:

- The parent is loaded once per item task.
- The parent is used only by that item task.
- Tactics are tried sequentially inside that item task.
- Open children are saved inside the item task to unique output files only if
  concurrent `goalStatePickle` has passed the Phase 0.5 compaction test. If
  concurrent compaction is unsafe, item tasks must return child states to the
  main loop for serial pickling, and parent regions must remain alive until
  that serial save completes.
- The task does not allocate process-local ids and does not touch
  `State.goalStates`.
- Region cleanup follows the selected ownership model above. The task must not
  free a region merely because the loop ended.

5. Spawn bounded item tasks:

```lean
def runItemsChunk ctx items := do
  discard <| Runtime.markMultiThreaded ctx.env
  discard <| Runtime.markMultiThreaded ctx.coreContext
  let tasks ← items.mapM fun item =>
    let wrapped ← Core.wrapAsync (runStepItem ctx item) (cancelTk? := none)
    wrapped.asTask
  joinCompletedOnMainLoop tasks
```

The join helper is intentionally a placeholder. Real code should chunk by
`maxParallelItems`, use default task priority, avoid `Task.Priority.dedicated`,
and call `Task.get` only from the main command loop. Do not block from inside
worker tasks or task continuations; that can cause Lean to grow the worker pool
and weaken the memory bound.

The main command loop then concatenates chunks, sorts results by
`(itemIdx, tacticIdx)`, deletes any temporary failed child files, and prints one
JSON response.

Also tighten the existing resident-region cleanup while touching `Repl.lean`:
dedupe `goal.delete` ids before collecting regions. Duplicate ids should not be
able to enqueue the same `CompactedRegion` twice.

Acceptance for Phase 1B:

- 2, 4, 8, then 16 different parent items run concurrently in one process.
- Each item still tries its 8 tactics sequentially.
- 16 parent items x 8 tactics succeeds in one process with `maxParallelItems`
  capped.
- Results are deterministic in `(itemIdx, tacticIdx)` order.
- Every open child path can be loaded and stepped in a later command.
- Bad tactics and cooperative timeouts are per-attempt errors. Non-cooperative
  tactics may still require process kill/restart, which loses the whole batch;
  that limitation must be explicit in the API/metrics.
- Invalid parent path affects only that item.
- Worker remains usable after mixed success/error batches.

Batching increases failure blast radius. In the current compatibility backend,
a non-cooperative tactic normally kills the worker leased for that one item. In
`pantograph_task`, the same process owns the whole compatible chunk, so a
process-level kill loses all sibling attempts in that chunk. This makes
per-attempt cooperative cancellation, moderate chunk sizes, and clear retry
semantics design constraints rather than nice-to-have metrics.

### Phase 2: Add The Server Backend Seam And `pantograph_task`

Once direct PyPantograph `goal_step_batch_async(...)` tests pass, refactor the
server. The current code path is:

```text
server/routers/exec.py
  step_one(item)
  -> StateStore.resolve(item.state_token)
  -> PantographManager.get_worker(env_profile, header)
  -> PantographWorker.step_state_with_tactics(record.path, item.tactics)
  -> StateStore.create_child(parent_token, child_path)
```

That is the compatibility behavior to preserve as `pantograph_pool`: bounded by
`max_workers`, but scaling throughput by occupying more warm workers.

Use the backend seam and route algorithm defined in Phase 0. The new work in
this phase is wiring `PantographTaskExecBackend` to the already-tested
`goal_step_batch_async(...)` wrapper, then making the route select it behind an
`exec_backend=pantograph_task` setting.

Before resolving many tokens and then running chunks asynchronously, decide how
state deletion races are prevented. Today `StateStore.resolve()` refreshes the
access time, while `/exec/cleanup` and storage-budget GC can delete files by
item id or LRU. For chunked execution, add state-token pinning/in-flight leases
or make the API contract explicit that cleanup for an item id must not race
active steps for that same item id. Pinning is preferable for server-side
correctness.

State-store work before enabling multiple backend kinds:

- add `backend_kind` and `state_format` to `StateRecord` sidecars;
- default existing Pantograph files to
  `backend_kind = "pantograph_pool"` and
  `state_format = "pantograph_goal_state_file"`;
- make child tokens inherit the parent's backend metadata;
- reject or route tokens whose format the selected backend cannot load.

Acceptance for Phase 2:

- existing `/exec/create_states -> /exec/step_batch -> /exec/cleanup` tests pass
  under `pantograph_pool`;
- new grouping tests prove compatible items are passed to one backend chunk;
- fragmented-header tests prove incompatible items are queued through
  `max_workers`, not launched as unlimited processes;
- `pantograph_task` works behind a flag for Init examples and the direct
  16 x 8 light Mathlib case;
- mixed valid/invalid tokens preserve input order.
- backend/state-format mismatches are rejected or routed explicitly;
- active token pinning or the cleanup race contract is covered by tests.

### Phase 3: Add Resource Caps And Metrics

Backend settings now exist, but `pantograph_task` is not the default. The
remaining Phase 3 work is production metrics, especially chunk latency,
per-attempt latency, and worker RSS.

```text
exec_backend = pantograph_pool | pantograph_task | repl_task
max_items_per_step_batch = 16
max_tactics_per_step_item = 8
max_attempts_per_step_batch = 128
max_items_per_worker_batch = 16
max_parallel_items_per_lean_process = measured value
max_lean_processes_per_env_profile = -1 by default for pantograph_pool;
                                     set to 1 initially when enabling
                                     pantograph_task, with 2-4 as an explicit
                                     fallback if one process cannot satisfy
                                     the absolute memory and latency budget
```

Reject over-cap requests before Lean work starts.

Metrics that must be emitted:

- backend name;
- request item count and tactic attempt count;
- group count and group sizes;
- warm/cold process marker;
- per-attempt latency and status;
- total batch latency;
- process RSS before/peak/after;
- resident state count/bytes;
- StateStore bytes;
- in-flight Lean task count.

Acceptance for Phase 3:

- Over-cap requests return a clear client error without touching Lean.
- Metrics show one process for the 16 x 8 compatible batch.
- RSS peak has an absolute configured ceiling and is bounded by warm process
  plus task workspace, not `16 * 3GB`. The test should include adversarial
  heavier tactics, not only `simp`/`rfl`, and should allow for Lean allocator
  retention when judging residual RSS.

### Phase 4: Canary The Task Backend, Then Flip The Default

Run both backends in CI and locally:

- Keep `pantograph_pool` tests as compatibility tests.
- Add `pantograph_task` tests for the same public `/exec` flows.
- For small deterministic examples, compare `pantograph_pool` and
  `pantograph_task` outputs.
- Run the real Goedel-mined frontier benchmark through `pantograph_task`.

Flip the default only after:

- public API tests pass on both backends;
- `pantograph_task` passes the 16 x 8 one-process capacity test;
- memory metrics show no process-per-item scaling;
- cleanup returns StateStore and resident state counts to baseline, while RSS
  residuals are interpreted with allocator-retention metrics;
- mixed error/timeout batches do not poison the process.

After the flip:

- keep `pantograph_pool` behind a flag for one release as a fallback;
- remove or demote it only after the task backend is stable under real training
  runs.

### Phase 5: Fallback If Pantograph Tasks Fail

If Pantograph fails for Pantograph-specific reasons after the Lean concurrency
model has been proven, do not undo the route/backend migration. Keep the new
`ExecBackend` interface and implement `ReplTaskExecBackend` instead:

- fork Lean REPL;
- add `ProofStepBatch`;
- use `ProofSnapshot.runString` for attempts;
- use `PickleProofState` / `UnpickleProofState` for StateStore files;
- return the same `BackendStepResult` values to the route.

This is not a fallback for shared-environment or concurrent-compaction unsafety.
`ProofSnapshot.runString` still runs Lean elaboration in `TermElabM`/`CoreM`,
and REPL proof-state pickle uses the same compaction class of machinery. If the
Phase 0.5 Lean-runtime gates fail, the REPL fork is the same core bet with a
different state structure unless it also uses a different environment-isolation
strategy.

This fallback preserves the migration seam. Only the backend implementation
changes; `/exec`, `StateStore`, client code, and trainer/search integration stay
the same.

## Why Stock Components Do Not Solve It

### PyPantograph

Pinned source: `stanford-centaur/PyPantograph@ffa7f243824d2762825abddb1e9f6e939ede761f`.

The Python wrapper cannot multiplex one process. `pantograph/server.py` writes
one command to stdin and immediately awaits one stdout line in `Server.run_async`.
There are no request ids, no response router, and no stream lock.

Source pointers in that pinned tree:

- `pantograph/server.py:177-204`: `Server.run_async` writes one command and
  awaits one `stdout.readline()`.
- `pantograph/server.py:265-309`: `goal_tactic_async` is just a wrapper around
  that single-command transport.
- `pantograph/server.py:505-525`: `goal_save_async` and `goal_load_async` are
  also one command each.

Empirical check on one `Init` Pantograph process:

```text
[('simp', 'ok', 2),
 ('rw [Nat.add_comm]', 'RuntimeError',
  'readuntil() called while another coroutine is already waiting for incoming data')]
```

That means Python-side concurrent calls into one Pantograph process are not a
path. The process protocol must remain one request -> one response, and the
parallelism must happen inside the Lean process.

Lean-side Pantograph is also currently sequential at the command loop:

- `src/Main.lean` reads one stdin line, executes one command, prints one JSON
  response, then loops.
- `src/Repl.lean` stores state in `State.goalStates : HashMap Nat GoalState`.
- `goal_tactic` reads one parent `GoalState`, runs one tactic, inserts one child
  state with `newGoalState`, serializes goals, and returns.
- `goal_save`, `goal_load`, and `goal_delete` already provide the file-backed
  state lifecycle.

Source pointers below refer to the pinned upstream tree before the repo-local
fork edits. The local fork has shifted some line numbers, especially in
`src/Repl.lean`, because of the `CompactedRegion` ownership patch.

- `src/Main.lean:34-50`: stdin loop executes one command and prints one response.
- `src/Repl.lean:14-24`: process-local state contains `goalStates`.
- `src/Repl.lean:41-48`: `newGoalState` mutates the process-local map.
- `src/Repl.lean:264-327`: current `goal_tactic` reads one parent and inserts
  one child.
- `src/Repl.lean:426-470`: command dispatch table, where `goal.step_batch` would
  be added.
- `src/Repl.lean:576-603`: `goal_delete`, `goal_save`, and `goal_load`.
- `src/Pantograph/Goal.lean:59-62`: `GoalState` stores
  `Elab.Tactic.SavedState`.
- `src/Pantograph/Goal.lean:721-742`: `GoalState.tryTactic` restores saved
  elaborator state and starts tactic evaluation.
- `src/Pantograph/Serial.lean:31-49`: unpickled `CompactedRegion`s must be
  freed.
- `src/Pantograph/Serial.lean:121-172`: `goalStatePickle` and
  `goalStateUnpickle`.

This is good news for hacking Pantograph: the missing primitive is not a new
public protocol, it is one new Lean-side batch command that does internal task
fanout and returns one JSON response.

### LeanInteract

Current source inspected at commit `583ce9d5d7760e37b39b26336e72a82aa74320a2`.

LeanInteract is a wrapper around Lean REPL. It does not make one REPL process run
many proof-state requests concurrently:

- `LeanServer._execute_cmd_in_repl` holds `self._lock` while writing stdin and
  reading until the response delimiter.
- `LeanServer.async_run` is `asyncio.to_thread(self.run, ...)`, so it moves the
  blocking call to a Python thread but the same lock still serializes one server.
- `LeanServerPool` creates `num_workers` `AutoLeanServer` instances. Each worker
  is a separate REPL subprocess.
- LeanInteract's performance docs describe external parallelization as multiple
  Lean servers. Its `Elab.async` option is within-command elaboration
  parallelism, not batch proof-state stepping.

Source pointers:

- `src/lean_interact/server.py:187-232`: `_execute_cmd_in_repl` holds the lock
  around stdin write and stdout read.
- `src/lean_interact/server.py:341-363`: `run` routes through the locked
  transport.
- `src/lean_interact/server.py:394-414`: `async_run` is `asyncio.to_thread`.
- `src/lean_interact/pool.py:36-80`: `LeanServerPool` creates multiple
  `AutoLeanServer` subprocess wrappers.
- `src/lean_interact/pool.py:252-314`: batch APIs distribute work across that
  process pool.
- `docs/user-guide/performance.md:98-116`: parallel elaboration is separate
  from external parallelization over multiple servers.

Therefore stock LeanInteract is a useful API/reference and baseline, but it is
not the final scaling mechanism. Using `LeanServerPool(num_workers=16)` would
recreate the same memory multiplier.

### Lean REPL

Current source inspected at `leanprover-community/repl@dde7dd4397951755f1f6c2b4b3a83a26911d63ad`.

Lean REPL has the proof-state machinery we need but not the parallel batch
primitive:

- `REPL/Main.lean` stores `cmdStates : Array CommandSnapshot` and
  `proofStates : Array ProofSnapshot`.
- `runProofStep` looks up one `ProofSnapshot`, calls `proofState.runString`, and
  records the child snapshot.
- `REPL/Snapshots.lean` defines `ProofSnapshot.runString` by parsing a tactic
  string and evaluating it in the snapshot.
- REPL supports `PickleProofState` and `UnpickleProofState`, so file-backed
  state tokens are possible.
- The outer REPL loop is still sequential stdin/stdout.

Source pointers:

- `REPL/Main.lean:62-95`: `cmdStates` and `proofStates` arrays plus recording
  helpers.
- `REPL/Main.lean:272-291`: proof-state pickle/unpickle command handlers.
- `REPL/Main.lean:355-363`: `runProofStep`.
- `REPL/Main.lean:421-438`: sequential stdin/stdout REPL loop.
- `REPL/Snapshots.lean:104-153`: `ProofSnapshot` and monad runners.
- `REPL/Snapshots.lean:172-185`: `ProofSnapshot.runString`.
- `REPL/Snapshots.lean:266-314`: proof-state pickle/unpickle implementation.
- `REPL/JSON.lean:182-190`: public JSON structures for proof-state
  pickle/unpickle.

A REPL fork is viable only if we add the same Lean-side `proofStepBatch`
primitive. Stock LeanInteract on top of stock REPL does not satisfy the memory
goal.

### Lean Task API

Lean 4.29.1 has the task primitives needed for the spike:

- `IO.asTask` / `BaseIO.asTask` start IO work eagerly as a `Task`.
- `Task.get` joins a task, but Lean documents that it can temporarily grow the
  thread pool while waiting, so the batch implementation should join in a bounded
  pattern rather than recursively blocking inside task continuations.
- `Task.Priority.dedicated` starts a dedicated thread. We should avoid dedicated
  priority for item tasks; use default priority and an explicit
  `max_parallel_items` cap.
- `IO.Runtime.markMultiThreaded` exists to mark an object graph before sharing
  it across threads. The shared batch context, especially the environment spine,
  must be marked or isolated deliberately before item tasks start.
- Lean's async elaboration machinery (`Core.wrapAsync`, async constants, and
  snapshot/environment APIs) may be the correct wrapper for per-item tactic
  execution. The spike should prefer that framework if it can run from
  file-backed `GoalState`s.

Lean CLI also supports `-M` memory limit, `-T` heartbeat timeout, and `-j`
threads. These are process-level controls; they do not replace request-level
backpressure and per-attempt timeout handling.

Source pointers in local Lean 4.29.1:

- `Init/System/IO.lean:243-310`: `BaseIO.asTask`, `mapTask`, `bindTask`, and
  `mapTasks`.
- `Init/System/IO.lean:448-497`: IO-specialized task helpers.
- `Init/Core.lean:628-675`: `Task.get` and task priority behavior.
- `Init/System/IO.lean:1848-1856`: `Runtime.markMultiThreaded`.
- `Lean/CoreM.lean:92-99` and `141-146`: name generators require child
  branches for parallel elaboration; `wrapAsync*` does this automatically.
- `Lean/CoreM.lean:545-562`: `Core.wrapAsync` wraps a `CoreM` action for task
  execution with child name generators, state, context, and heartbeats.
- `Lean/Elab/Task.lean:39-54`: `Elab.Task.asTask` uses `Core.wrapAsync` with a
  fresh cancel token.
- `Lean/Environment.lean:36-46` and `1008-1020`: environment branches are
  introduced with `addConstAsync`.
- `Lean/Environment.lean:2536-2605`: realization uses shared realization
  contexts and atomically checks/inserts realization promises.
- `lean --help`: `-M`, `-T`, and `-j` process-level controls.

## Preferred Route: Pantograph Fork With `goal.step_batch`

This section is a condensed implementation summary. The authoritative
phase-by-phase plan is `Phase 1A`, `Phase 1B`, and `Phase 2` above; keep this
section aligned with those phases rather than adding a second divergent plan.

Pantograph should be tried first because it already matches our current
`state_token` design:

- A public `state_token` maps to a backend-owned saved state file.
- Pantograph `goal_load` can rehydrate that file into a process-local
  `GoalState`.
- Pantograph `goal_save` can write child states back to files.
- Pantograph `goal_delete` can evict process-local states.

The Python server should continue to send one command and await one response.
Only the Lean-side command changes.

### Step 1: Fork And Build Pantograph

Create a real fork or local package of PyPantograph rather than editing the uv
cache. The package must build a modified `pantograph-repl` for Lean 4.29.1 and
the existing Mathlib pin.

Implementation options:

1. Add a git dependency in `pyproject.toml` pointing to our PyPantograph fork.
2. Or vendor only the Lean executable build in a repo-local package and point
   `PantographWorker` at that executable.

Do not change the public `/exec` API for this spike.

### Step 2: Add Protocol Types

Add batch request/response structures in `src/Pantograph/Protocol.lean`.

The batch request should contain:

- `items`: array of proof-state expansion items.
- each item has `itemIdx`, file-backed `parentPath`, `tactics`, optional
  `goalId`, and optional timeout. Do not support resident `stateId` parents in
  the first implementation.
- `outputDir`: directory where child states should be saved.
- `maxParallelItems`: hard cap on concurrent item tasks inside the process.

The response should contain results in deterministic `(itemIdx, tacticIdx)`
order:

- status: `open | complete | error`
- saved child path for open results
- serialized goals for open results
- messages/errors
- optional internal timing for metrics

The Lean command can be named `goal.step_batch` to keep it clearly inside the
Pantograph goal-state layer.

### Step 3: Split Tactic Execution From Process-Local State Mutation

Do not call the existing `goal_tactic` command from item tasks. It mutates
`State.goalStates` by allocating ids. Instead extract a lower-level helper that
one item task can call sequentially for each tactic:

```lean
runGoalAttemptCoreM :
  BatchContext ->
  GoalState ->
  Site ->
  String ->
  CoreM AttemptRawResult
```

The helper should be built from existing Pantograph pieces:

1. Build the Phase 0.5-approved execution context on the main command loop:
   either a per-item isolated environment branch, or a shared context after
   `IO.Runtime.markMultiThreaded` and realization-safety tests.
2. In the task, run `goalState.tryTactic site tactic`, using the same
   `liftTermElabM` / `runCoreM` pattern as current `goal_tactic`.
3. Return a value, not a process-local id:
   `{status, childGoalState?, goals?, messages?, error?}`.
4. Do not mutate `State.goalStates` from inside the task.
5. Do not write JSON or stdout from inside the task.

The item task saves open children to unique files immediately only if concurrent
pickling is proven safe. Otherwise it returns child states for serial pickling
on the main loop, with parent compacted regions kept alive until serialization
finishes. The main command loop then performs response-level work:

- sort results;
- print exactly one JSON response.

### Step 4: Load Parents And Manage Compacted Regions

For file-backed parents, use `goalStateUnpickle path (background? := some env)`.
It returns `(GoalState, CompactedRegion)`.

The region is a real memory safety issue. Pantograph's `Serial.lean` says the
`CompactedRegion` must be freed after use; ignoring it leaks memory. Therefore:

1. Each item task loads its own parent state.
2. Keep that parent region alive inside the item task until all of that item's
   tactic results are saved.
3. If children are pickled serially on the main loop, return an owned handle
   that keeps the region alive until serialization finishes.
4. Drop all references to the unpickled parent and children that depend on the
   region.
5. Enqueue the parent region for deferred release at the next command boundary.
6. Run resident-state deletion for anything not intentionally cached.

For repeated tactics on the same parent, load the parent once inside the item
task and loop over tactics sequentially. Do not share that parent with other
tasks in the first implementation.

### Step 5: Bound Task Fanout

The backend must enforce both HTTP-level and Lean-level caps:

```text
max_items_per_step_batch = 16
max_tactics_per_step_item = 8
max_attempts_per_step_batch = 128
max_items_per_worker_batch = 16
max_parallel_items_per_lean_process = measured, probably 8 or 16 initially
max_lean_processes_per_env_profile = 1 for the spike
```

Implementation should chunk items by `maxParallelItems` rather than spawning
unbounded work and relying on the runtime. This makes peak task workspace
predictable.

Use default task priority. Do not use `Task.Priority.dedicated` for item tasks
unless a later test proves it is necessary; dedicated priority creates dedicated
threads and weakens the resource bound.

### Step 6: Timeout And Cancellation

Current Pantograph has protocol option `timeout` and uses cancel tokens in
`runCoreM`. The item task should preserve that logic for each tactic in the
sequential item loop.

Minimum acceptable spike behavior:

- a bad tactic returns an error for that attempt only;
- cooperative timeouts return an error for that attempt only;
- sibling attempts complete or fail independently;
- non-cooperative tactics can still force a process-level timeout that kills the
  worker and loses the whole batch.

Per-attempt cancel tokens and heartbeat budgets are required before the 16 x 8
acceptance test is considered meaningful; batch-level timeout remains only the
last-resort guard.

### Step 7: Python Integration

Add a new PyPantograph wrapper method:

```python
async def goal_step_batch_async(
    self,
    items: list[GoalStepBatchItem],
    output_dir: str,
    max_parallel_items: int,
) -> GoalStepBatchResult:
    return await self.run_async("goal.step_batch", payload)
```

Then change `PantographWorker.step_state_with_tactics` or add a new
`step_batch` method that:

1. passes parent state paths directly to the Lean batch command;
2. lets Lean save open child states to a temporary output directory;
3. records/promotes those paths in `StateStore`;
4. returns the existing public schema.

This keeps `state_token` stable. Tokens still map to saved files, not
process-local ids.

## Fallback Route: Lean REPL Fork

This is the same fallback described in Phase 5, repeated here only to make the
state-token implications explicit.

If Pantograph fails for Pantograph-specific reasons after Phase 0.5 proves the
Lean concurrency model, fork Lean REPL and add the same primitive there.

Implementation sketch:

- Add `ProofStepBatch` to `REPL/JSON.lean`.
- Add `runProofStepBatch` to `REPL/Main.lean`.
- Spawn one Lean task per item.
- In each item task, unpickle that item's parent `ProofSnapshot`, loop over
  tactics sequentially using `ProofSnapshot.runString`, and save open children
  with `ProofSnapshot.pickle`.
- Use `PickleProofState` / `UnpickleProofState` for `state_token` file storage.
- Keep the outer stdin/stdout protocol one request -> one response.

For the state-token case, use the `backend_kind` / `state_format` metadata added
in Phase 0:

```text
backend_kind = pantograph_pool | pantograph_task | repl_task
state_format = pantograph_goal_state_file | repl_proofsnapshot_v1
```

`StateStore.resolve` should reject or route tokens whose `state_format` does not
match the active backend. Child tokens inherit the parent's backend kind and
state format. If we migrate live from Pantograph files to REPL files, existing
Pantograph tokens cannot be loaded by REPL; either keep `pantograph_pool`/
`pantograph_task` available until those item ids are cleaned up, or invalidate
stale tokens on backend switch and require trainer/search to restart those
attempts.

This route likely requires more adaptation in our backend because Pantograph's
goal serialization and automatic-mode semantics already match the current
prototype better. It does not avoid Lean-runtime shared-environment or
compaction hazards unless the REPL fork also uses a different environment
isolation strategy.

## Routes To Avoid

Do not use these as the scalable design:

- Python `asyncio.gather` over a single Pantograph server. It races on stdout.
- LeanInteract `async_run` on one server. It serializes under a Python lock.
- LeanInteract `LeanServerPool` sized to in-flight items. It is process-based
  and multiplies Mathlib RSS.
- A Kimina manager job per tactic. It reproduces the process explosion we are
  trying to eliminate.

## Productization Tests

These tests decide whether the scratch Pantograph route has been productized
well enough to use behind `/exec`.

### Test 0: Remaining Lean Runtime Gates

The scratch spike has already justified implementing `goal.step_batch`. Before
making `pantograph_task` default, commit and run these gates:

- shared-env plus `markMultiThreaded`/`Core.wrapAsync`, or a stronger isolated
  environment model, is chosen and documented;
- realization-collision stress test passes repeatedly;
- concurrent child-state pickle test passes, or serial child-state pickling is
  selected with a measured memory cost;
- `markMultiThreaded` cost is measured if the shared-env model is used.

### Test A: Single-Process Correctness

Build modified Pantograph with `goal.step_batch`.

Create one parent proof state:

```lean
theorem t (n : Nat) : n + 0 = n := by
  sorry
```

Run batch tactics:

```text
simp
rw [Nat.add_comm]
bad_tactic
```

Expected:

- `simp` returns `complete`;
- `rw [Nat.add_comm]` returns `open` with goal `n : Nat\n|- 0 + n = n`;
- `bad_tactic` returns `error`;
- sequential `goal_tactic` and batch `goal.step_batch` agree.
- candidate tactics are independent: a later candidate cannot depend on a local
  hypothesis introduced by an earlier candidate in the same item.

### Test B: Multiple Items, Sequential Tactics Per Item

Create 2, then 4, then 8 independent parent states. For each parent, send 8
tactics in the same `goal.step_batch` request with `maxParallelItems` equal to
the number of items.

Pass condition:

- no crash;
- deterministic result order;
- logs/metrics show one Lean task per item, not one task per tactic;
- within each item, tactics run sequentially against that item's parent state;
- all returned child states can be loaded and stepped later;
- `State.goalStates` is not mutated by item tasks.

This is the first real thread-safety test for multiple item-owned parent
`GoalState`s running concurrently inside one process.

### Test C: Multiple Parents

Create or load 16 independent parent states and run 8 tactics per parent in one
request.

Pass condition:

- one `pantograph-repl` OS process handles all 128 attempts;
- no 16-process fanout;
- errors do not poison sibling items;
- all open children are saved and reusable by later requests.
- concurrent batch commands write child files under distinct per-command output
  directories, so deterministic Lean child filenames cannot collide across
  requests;
- the parent input is file-backed `parentPath`; resident `stateId` parents are
  intentionally not part of the first implementation.

### Test D: Memory Bound

Measure:

1. warm RSS after Mathlib load;
2. peak RSS during the 16 x 8 batch;
3. RSS after result serialization, parent region freeing, resident deletion, and
   `/exec/cleanup`.

Pass condition:

- peak stays under an absolute configured ceiling and is not close to
  `16 * 3GB`;
- residual RSS is explained by resident state, compacted regions, or allocator
  retention rather than treated as a simple pass/fail baseline return;
- state-store bytes are tracked separately from process RSS.
- repeated batch parent loads do not show unbounded growth; duplicate
  `goal.delete` ids do not double-free a resident region.

### Test E: Bad Tactics And Timeouts

Include syntax errors, unknown identifiers, long-running tactics, and mixed
success/failure siblings.

Pass condition:

- per-attempt structured errors;
- worker remains usable after the batch;
- cooperative tactic timeouts are per-attempt errors;
- process kill/restart is only used for non-cooperative or process-level
  failure, and losing the whole batch in that case is documented.

## Decision Rule

Proceed with `pantograph_task` server integration after Tests A through E pass
directly against the forked PyPantograph wrapper. Make `pantograph_task` the
default only after Test 0 also passes in committed stress tests and the real
Goedel-mined frontier benchmark passes end to end.

If Tests A or B fail for Pantograph-specific `GoalState` reasons after Test 0
has passed, pivot to the Lean REPL fork and repeat the same tests with
`ProofSnapshot`.

If Test 0 fails, the REPL fork is not an automatic escape hatch because it uses
the same Lean elaboration/runtime classes. The honest conclusion is then that
constant process memory requires a different Lean-native environment-isolation
design or accepting smaller fixed process pools plus lower in-flight capacity.
Do not return to process-per-item scheduling.
