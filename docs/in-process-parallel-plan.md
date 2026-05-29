# WIP: in-process item parallelism (`pantograph_task`)

Status: **experimental, not shipped.** The production backend is the bounded
multi-process pool (`pantograph_process_pool`, on `explore/bounded-backend` /
mainline) — see `docs/bounded-backend-decision.md`. This branch keeps the
in-process-parallel exploration alive as a *future optimization*: denser core
use (many items per Lean process) if process count ever becomes the binding
constraint. It must clear the equivalence oracle on Mathlib before it can ship.

## Why this is hard (root cause)

`goal.step_batch` runs items concurrently inside one Lean process via
`Core.wrapAsync` + `Task`. On `import Init` this is **exactly equivalent** to
sequential (oracle: 0/60 mismatches). On Mathlib it crashes and diverges
(74 mismatches) because concurrent tasks share one `Environment` whose **lazy
realization** mutates shared state on first touch:

- `Lean.Environment.realizeValue` routes imported-constant realizations through a
  shared `realizeMapRef : IO.Ref`. The atomic `modifyGet` is fine, but the
  realize callback builds a fresh single-threaded object graph and stores it; a
  second task reading that graph touches objects whose RC was never marked
  atomic → corruption surfacing as crashes in `realizeValue` / `Level.hash` /
  `getFunInfoAux` / simp / defeq / kabstract. There is also a non-atomic
  fast-path `realizeMapRef.get` that races a concurrent writer.
- `markMultiThreaded` makes reference counting atomic but does **not** serialize
  or isolate realization.

Per-task scratch (`Core.State`, `MetavarContext`, `Meta.State.cache`) is already
isolated: `Core.wrapAsync` gives each task a fresh `Core.State` and the caches
are persistent (functional) structures, so sibling writes never collide. The
**only** shared-mutable hole is `Environment` realization.

## What is already on this branch (Track A, UNBUILT/UNTESTED)

A `warmup` mechanism (`third_party/PyPantograph/src/Repl.lean
::warmupGoalStepBatchItems`, gated by `GoalStepBatch.warmup`, plumbed through
`server.py` → `pantograph_worker` → `StepBatchBackendConfig.pantograph_task_warmup`
→ settings/router):

- Before spawning tasks (and only when `parallel > 1`), on the **main thread**:
  resolve the async-elaboration `checked` kernel env once, then `serializeGoals`
  each *distinct* parent to force the instance / fun-info / discr-tree /
  match-equation realizations a tactic would trigger, populating `realizeMapRef`
  single-threaded.
- `markMultiThreaded` is moved to run **after** warmup so objects allocated
  during warmup also get atomic RC.

This compiles in principle but has **not** been built or run. It is a hypothesis,
not a fix.

## Plan to finish

1. **Build**: `cd third_party/PyPantograph/src && lake build repl` (rebuilds the
   Pantograph repl against prebuilt Mathlib oleans; minutes, not a Mathlib
   rebuild). Copy the binary as `build-pantograph.py` does.
2. **Prove equivalence**: run the oracle on Mathlib at `--parallel 16`, with
   `--repeat` high, many times:
   `equivalence_oracle.py --imports Mathlib --imports Aesop --project mathlib4 --parallel 16 --repeat 8`.
   Acceptance: **0 mismatches** across ≥ 20 runs, **no crashes**. Use a workload
   that actually triggers realization (Mathlib `simp`/`ring`/`omega`/typeclass
   goals, not just `intro`).
3. **If warmup is insufficient** (realization of constants not reached by
   `serializeGoals` still races): escalate in order of preference —
   a. broaden warmup to force a larger constant closure (e.g. run the actual
      first tactic of each item under a discard, or `Meta.realizeConst` over a
      computed dependency set);
   b. wrap realization in a lock (serialize realization, parallelize the rest);
   c. per-thread realization tables (requires Lean core patch).
4. **Two-level**: once a single process is exact at `parallel = K`, run it inside
   the bounded process pool — each of N pool workers runs `goal.step_batch` with
   `maxParallelItems = K`. Total concurrency = N × K. Reuse the
   `pantograph_process_pool` lane machinery; the lane's "step" becomes a batch
   call instead of per-item `step_state_with_tactics`.
5. **Benchmark**: compare tactics/sec and peak private footprint against the
   process-pool-only baseline on the frozen 200×8 workload. In-thread only earns
   its keep if it beats `N` processes on the same memory budget.

## Acceptance criteria (before merging to mainline)

- Oracle: 0 mismatches on Mathlib at `parallel ≥ 16`, repeated, zero crashes.
- Throughput strictly better than the process pool at equal memory budget.
- `exec_backend` default stays `pantograph_pool` until the above hold;
  `pantograph_task` remains opt-in.

## Risks

- Warmup may be fundamentally incomplete: a tactic can realize constants that no
  pre-pass can enumerate without running the tactic itself. If so, only options
  3b/3c are sound, and 3c needs a Lean core change.
- Even if exact, in-thread may not beat the process pool on this hardware
  (cores, not memory, bound throughput — see the decision doc), making this an
  optimization for memory-starved hosts only.
