# Lean Task Backend Research

Date: 2026-05-27

Status as of 2026-05-30: this document is research-only. It is not the
production backend plan for `main`. The mainline backend direction is the
bounded old-command Pantograph process pool described in `../backend-end-plan.md`.
Use this document only when explicitly returning to in-process Lean task /
`pantograph_task` research.

Goal: support at least one `/exec/step_batch` request containing 16 proof-state
items with 8 tactics each, i.e. 128 tactic attempts, without running one
Mathlib-loaded OS process per item.

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

## Migration Plan

The migration target is not "replace Pantograph everywhere in one jump." The
right path is to preserve the public `/exec` API and `StateStore`, insert a
backend abstraction behind the router, then swap only the worker implementation
after a one-process batch primitive passes tests.

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

This is the behavior that cannot scale. First, wrap it behind a compatibility
backend rather than deleting it.

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
  per item. This makes the refactor behavior-preserving.
- `PantographTaskExecBackend`: the future backend, initially hidden behind a
  config flag.

The route becomes backend-agnostic:

1. Resolve all tokens up front.
2. Return `invalid_state_token` immediately for missing tokens.
3. Group valid items by `(env_profile, header_hash, backend_kind,
   state_format)`.
4. Call `backend.step_batch_group(...)` once per compatible group.
5. Promote `child_path`s with `StateStore.create_child(parent_token, child_path)`.
6. Merge group results back into original request order.

This one route refactor is the real migration seam. Once it exists, Pantograph
pool, Pantograph task backend, and a REPL fork can all plug into the same public
API.

Acceptance for Phase 0:

- Existing `/exec/create_states -> /exec/step_batch -> /exec/cleanup` tests pass
  with `exec_backend=pantograph_pool`.
- New route tests prove mixed valid/invalid tokens preserve input order.
- New grouping test proves two compatible items are passed to the backend in one
  group rather than independently scheduled by the route.
- `StateStore` records `backend_kind` and `state_format` from the start, and
  `resolve()` rejects tokens that cannot be loaded by the active backend.

### Phase 0.5: Prove Lean Concurrency Is Feasible

This gate comes before implementing a threaded `goal.step_batch`. The hard
unknown is not the HTTP protocol; it is whether many `TermElabM` tactic runs can
share one Mathlib-loaded Lean process without corrupting Lean runtime state.

Audit and spike these points against Lean 4.29.1 source:

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
   - Investigate whether `Core.wrapAsync`, `Environment.addConstAsync`, and the
     snapshot/async-env APIs can wrap one proof-state item execution.
   - If those APIs apply to a reconstructed `GoalState`, use them rather than
     raw shared-env `IO.asTask`.
4. Concurrent pickling.
   - `goalStatePickle` compacts object graphs. Test whether 16 item tasks can
     pickle children concurrently while referencing the shared imported
     environment.
   - If concurrent compaction is unsafe, join item tasks with child states still
     alive, pickle serially on the main loop, then free parent regions. That
     fallback must be measured because it raises peak memory.

Decisive micro-tests before Phase 2:

- Realization collision: run two or more concurrent items whose tactics force
  the same generated constant or instance, repeat enough times to catch
  nondeterministic corruption.
- Concurrent pickle: produce 16 open child states, pickle them simultaneously,
  then load and step every child.
- `markMultiThreaded` cost: measure wall time and RSS for marking the post-Mathlib
  batch context, then compare single-thread tactic latency before and after.

Phase 2 is allowed to start only after choosing one explicit concurrency model:

- preferred: per-item isolated environment branch through Lean's async
  elaboration framework;
- fallback: shared environment plus mandatory `markMultiThreaded`, with measured
  cost and a passing realization-collision test;
- blocked: neither path is safe, in which case a one-process task backend is not
  viable and the next option is a lower-level Lean-native worker or a small fixed
  process pool.

### Phase 1: Add The Pantograph Fork Without Using Threads Yet

Fork PyPantograph or vendor a repo-local modified `pantograph-repl`. Do not edit
the uv cache. Point `pyproject.toml` at the fork only after the fork builds
cleanly for Lean 4.29.1 and the current Mathlib pin.

In the fork, add `goal.step_batch`, but first implement it sequentially inside
Lean:

1. Parse a batch request.
2. For each item, load parent state from `path`.
3. For each tactic, call the same internal logic as `goal_tactic`.
4. Save open children to `outputDir`.
5. Return one JSON response in deterministic `(itemIdx, tacticIdx)` order.
6. Free unpickled `CompactedRegion`s after all dependent values are saved.

This phase does not solve throughput yet. Its purpose is to prove the new
protocol, state-file flow, and Python integration without adding thread-safety
risk.

Python side:

- Add `goal_step_batch_async(...)` to the forked PyPantograph wrapper.
- Implement `PantographTaskExecBackend.step_batch_group(...)` by sending all
  grouped items in one `goal.step_batch` command.
- Keep `exec_backend=pantograph_pool` as the default while this is tested.

Acceptance for Phase 1:

- Sequential `goal.step_batch` matches repeated `goal.tactic` on Init examples.
- Python `/exec/step_batch` can run through `exec_backend=pantograph_task` for
  one item and multiple items.
- One compatible multi-item request uses one Pantograph subprocess and one
  stdin/stdout command, not one command per item.

### Phase 2: Add Item-Level Lean Task Parallelism

Do not start this phase until Phase 0.5 has selected a concrete Lean
concurrency model. The implementation must either run item tasks in isolated
environment branches through Lean's async elaboration machinery, or it must use
a shared environment that has been explicitly marked multi-threaded and has
passed the realization-collision tests. Raw `IO.asTask` over a shared,
unmarked `Environment` is not an acceptable implementation.

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
  IO RawTacticResult
```

The helper is the same machinery used by current `goal_tactic`:

- select `Site` from `goalId` and automatic mode;
- run `goalState.tryTactic site tactic` inside a local `TermElabM`;
- serialize goals with `nextGoalState.serializeGoals`;
- serialize messages;
- compute `hasSorry` and `hasUnsafe`;
- return `open`, `complete`, or `error` with the child `GoalState` only long
  enough to save it.

4. Add an item runner:

```lean
runStepItem :
  BatchContext ->
  GoalStepBatchItem ->
  IO GoalStepBatchItemResult
```

Its shape is:

```lean
def runStepItem ctx item := do
  let (parent, region) ← goalStateUnpickle item.parentPath (background? := some ctx.env)
  try
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
    pure { itemIdx := item.itemIdx, results }
  finally
    region.free
```

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
- The task frees the parent `CompactedRegion` only after all children depending
  on that region have been saved and no returned value still references the
  region.

5. Spawn bounded item tasks:

```lean
def runItemsChunk ctx items := do
  let tasks ← items.mapM fun item =>
    IO.asTask (runStepItem ctx item)
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

Acceptance for Phase 2:

- 2, 4, 8, then 16 different parent items run concurrently in one process.
- Each item still tries its 8 tactics sequentially.
- 16 parent items x 8 tactics succeeds in one process with `maxParallelItems`
  capped.
- Bad tactics and cooperative timeouts are per-attempt errors. Non-cooperative
  tactics may still require process kill/restart, which loses the whole batch;
  that limitation must be explicit in the API/metrics.
- Worker remains usable after mixed success/error batches.

### Phase 3: Add Resource Caps And Metrics Before Defaulting

Add backend settings before the task backend becomes default:

```text
exec_backend = pantograph_pool | pantograph_task | repl_task
max_items_per_step_batch = 16
max_tactics_per_step_item = 8
max_attempts_per_step_batch = 128
max_parallel_items_per_lean_process = measured value
max_lean_processes_per_env_profile = 1 initially, with 2-4 as an explicit
                                     fallback if one process cannot satisfy the
                                     absolute memory and latency budget
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

Source pointers:

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
- each item has `itemIdx`, parent by `stateId?` or `path?`, `tactics`, optional
  `goalId`, and optional timeout.
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
runGoalAttemptIO :
  BatchContext ->
  GoalState ->
  Site ->
  String ->
  IO AttemptRawResult
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
5. Free the region.
6. Run resident-state deletion for anything not intentionally cached.

For repeated tactics on the same parent, load the parent once inside the item
task and loop over tactics sequentially. Do not share that parent with other
tasks in the first implementation.

### Step 5: Bound Task Fanout

The backend must enforce both HTTP-level and Lean-level caps:

```text
max_items_per_batch = 16
max_tactics_per_item = 8
max_attempts_per_batch = 128
max_parallel_items = measured, probably 8 or 16 initially
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
backend_kind = pantograph_task | repl_task
state_format = pantograph_goalstate_v1 | repl_proofsnapshot_v1
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

## Decisive Spike Tests

These tests decide whether the Pantograph route is viable.

### Test 0: Lean Runtime Concurrency Gates

Before testing `goal.step_batch` semantics, run the Phase 0.5 gates:

- shared-env or isolated-env execution model chosen and documented;
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
- resident `goalStates` count returns to baseline after deletion.

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

Proceed with the Pantograph fork only if Test 0 and Tests A through E pass.

If Tests A or B fail for Pantograph-specific `GoalState` reasons after Test 0
has passed, pivot to the Lean REPL fork and repeat the same tests with
`ProofSnapshot`.

If Test 0 fails, the REPL fork is not an automatic escape hatch because it uses
the same Lean elaboration/runtime classes. The honest conclusion is then that
constant process memory requires a different Lean-native environment-isolation
design or accepting smaller fixed process pools plus lower in-flight capacity.
Do not return to process-per-item scheduling.
