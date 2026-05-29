# Bounded-memory step backend: decision & evidence

_Date: 2026-05-29_

## Problem

Step proof-state items at scale (target: hundreds of in-flight items, batches
like 16 items × 8 candidate tactics) without multiplying Mathlib processes. The
prior attempt ran items concurrently *inside one Lean process* via Lean tasks
(`goal.step_batch` with `Core.wrapAsync`), which **crashed** on real Mathlib and
produced **74 mismatched item results** versus sequential execution.

Requirements: parallel results must be **byte-identical to sequential**, memory
must be **bounded**, and it must **scale to hundreds of items**.

## Decisive measurements

These drove the decision (tools: `examples/pantograph_benchmark/measure_memory_sharing.py`,
`examples/pantograph_benchmark/equivalence_oracle.py`).

1. **Memory is shared, not per-process.** A Mathlib worker shows ~2.6 GB RSS but
   only **~0.77 GB private `phys_footprint`**; the rest is memory-mapped,
   shared `.olean` pages. Three workers together added **~0.75 GB** of system
   memory. The "3-4 GB per process" figure is RSS double-counting shared pages —
   the marginal cost of another worker is ~0.77 GB.

2. **In-thread parallelism is exact on `import Init` but races on Mathlib.** The
   oracle proved `maxParallelItems=1` vs `=4` are byte-identical on Init
   (0/60 mismatches) — the batching logic is correct. It diverges only under
   Mathlib because concurrent tasks share one `Environment` whose lazy
   `realizeConst`/`realizeValue` (match-equations, projections, derived
   instances) mutates shared state on first touch. `markMultiThreaded` makes
   reference counting atomic but does **not** make realization race-free.

3. **Cores, not memory, bound throughput.** This host has 4 performance / 10
   logical cores. Since workers cost ~0.77 GB, a pool sized to core-count fits
   in a few GB and already saturates the cores; in-thread parallelism cannot add
   throughput it can't get safely.

## Decision: bounded multi-process pool (`pantograph_process_pool`)

Separate OS processes are the **only design byte-identical to sequential by
construction** — each process owns its own `Environment` and realization caches,
so there is no shared mutable Lean state to race on. Combined with `.olean` mmap
sharing, it is also the bounded-memory design.

**Two-level shape:** level 1 = a pool of N worker processes (parallel,
`max_lean_processes_per_env_profile` lanes per `(env_profile, header)` group);
level 2 = each lane pipelines a queue of items stepped strictly sequentially.
Scales to hundreds of items by queueing across the bounded pool.

**Accepted trade-off:** lower per-process density than (hypothetical safe)
in-thread parallelism, in exchange for guaranteed correctness, bounded memory,
and low implementation cost.

### Implementation

- `server/exec_backend_utils.py` — pure helpers: compatibility grouping +
  round-robin lane distribution (unit-tested, no I/O).
- `server/exec_backends.py::execute_step_batch_process_pool` — groups items by
  `(env_profile, header_hash)`, splits each group across at most
  `max_lean_processes_per_env_profile` lanes; each lane holds **one** worker
  lease and steps its items sequentially via the proven
  `step_state_with_tactics` path. Preserves state pinning, the cap validation,
  and the existing `pantograph_pool` / `pantograph_task` seam.
- `server/settings.py` — `exec_backend="pantograph_process_pool"`;
  `DEFAULT_PROCESS_POOL_LANES=4` when the per-env cap is left unbounded. The lane
  count is the **same knob** as the manager's `max_workers_per_env_profile`, so
  the lanes can actually acquire that many concurrent leases.

### Validation

- `tests/test_exec_backends.py` — 8/8 fakes tests pass, including bounded-lane
  distribution, one-lease-per-lane reuse, header splitting, and
  orchestration-equivalence vs the item-at-a-time backend.
- Real Mathlib, in the production code path (`execute_step_batch_request`),
  P=2 × 12 items: **`equivalent: true`**, **crash-free**, total private
  footprint **1548 MB** (783 + 765 MB). (Throughput micro-numbers from this run
  are not representative — the sample tactics resolve in ~ms; a representative
  throughput comparison needs the frozen 200×8 workload and more RAM headroom.)

### How to run

```bash
# memory sharing
PYTHONPATH="$PWD:$PWD/third_party/PyPantograph" .venv/bin/python \
    examples/pantograph_benchmark/measure_memory_sharing.py
# equivalence oracle (cheap)
PYTHONPATH="$PWD:$PWD/third_party/PyPantograph" .venv/bin/python \
    examples/pantograph_benchmark/equivalence_oracle.py --imports Init --parallel 4
# process-pool throughput / equivalence / memory on Mathlib
PYTHONPATH="$PWD:$PWD/third_party/PyPantograph" .venv/bin/python \
    examples/pantograph_benchmark/process_pool_throughput.py --processes 4 --items 32
```

## Deferred: in-process parallelism (separate WIP branch)

In-process item parallelism is kept as a **future optimization** (denser core
use if memory ever becomes the binding constraint), not a requirement. The
existing `pantograph_task` backend and `goal.step_batch` command are preserved
behind the seam. Continued work — making lazy realization race-free (pre-warm a
constant closure, or per-thread / locked realization), per-item isolated
`Core.State`, and validating 0 mismatches on Mathlib with the oracle — lives on
branch **`wip/in-process-parallel`** (see its plan doc). It must clear the
equivalence oracle on Mathlib before it can ship.
