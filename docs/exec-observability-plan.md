# Plan: Exec Observability + Verify-Equivalence Spike

Status: ready for implementation, 2026-06-07.

Give the `/exec` path structured diagnostics (errors with positions), split
timing (pool-wait vs Lean compute), and optional CPU/RAM under a debug flag.
And answer the question that decides the dual-mode verify story: **can the
warm Pantograph pool certify a complete proof to the same standard as the REPL?**

This is **Plan 2 of 2**. It is independent of
[dual-mode-separation-plan.md](dual-mode-separation-plan.md) (Plan 1) and can
land in either order. Phase 0's spike result feeds Plan 1's deferred verify
decision.

## Motivation

The exec path returns flat `messages: list[str]`
([exec_models.py:95](../packages/lean-client/lean_client/exec_models.py),
`exec_models.py:140`) and no timing or resource data. RL/search needs:

- structured errors with source positions (to localize failures),
- timing that separates queue/pool-wait from actual Lean work (to tune
  concurrency), and
- optional per-call CPU/RAM (to size workers).

Separately, the RL loop verifies each searched batch to compute rewards, then
updates weights — **per batch, in the training loop, throughput-critical.** If
exec can self-certify complete proofs, search and verify share one warm pool
(no second service, no network hop, no cold REPL). That is the
[dual-mode](dual-mode-separation-plan.md) "verify at search time" decision, and
the spike below is what unblocks it.

## Phase 0 — Spike (blocks the rest)

Instrument a local Pantograph worker and capture the **raw** objects on:

- `load_sorry` failure (parse / type error),
- `goal_tactic` failure,
- success with warnings in `child_state.messages`.

Document, per case: is `severity` present? are `pos` / `endPos` present, and are
they body-relative or full-file? are they stable enough to map to `ExecMessage`?

**Verify-equivalence (the decision):** for a state that is *complete* (all goals
closed by stepping), can we certify acceptance equivalent to the REPL — no
`sorry`/`sorryAx`, no errors, axiom-clean? Determine the **minimal** certification:

1. Is a completed exec state already error/`sorry`-free *by construction*
   (Pantograph elaborated each tactic as it was applied)? If so, certification is
   nearly free.
2. Can axioms be enumerated on the resulting term (`#print axioms` equivalent)
   through Pantograph, to enforce an axiom allow-list?
3. Or is a full standalone recompile of the assembled proof required?

**Output:** `docs/exec-diagnostics-spike.md` — sample JSON, a Pantograph→`ExecMessage`
mapping table, and the verdict: which of (1)/(2)/(3) is needed, and therefore
whether `/exec/verify` (Phase 2) is viable on the warm pool (path **C**) or the
verify path must stay on a separate REPL deployment (path **A**, fallback).

## Phase 1 — Structured diagnostics + split timing

All wire types live in `lean_client/exec_models.py` (single source) and are
surfaced through `server/schemas_exec.py` — see
[exec-schema-single-source](../packages/lean-client/lean_client/exec_models.py).

```python
class Pos(BaseModel):
    line: int   # 1-based
    col: int    # 0-based

class ExecMessage(BaseModel):
    severity: Literal["trace", "info", "warning", "error"]
    data: str
    pos: Pos | None = None        # body-relative (see header offset below)
    end_pos: Pos | None = None

class ExecDebugInfo(BaseModel):
    cpu_max: float                # percent of one core
    memory_max: int               # bytes (RSS peak)

class ExecDiagnostics(BaseModel):
    acquire_ms: float             # lease request → worker ready (incl. pool wait)
    lean_ms: float                # time strictly inside the Lean call
    debug: ExecDebugInfo | None = None
```

- **Change `messages: list[str]` → `list[ExecMessage]` directly** on
  `ExecCreateStatesResult` and `ExecStepResult`. No `messages_v2`, no deprecation
  window, no major-version ceremony — the project is unreleased (latest commit is
  truth). This explicitly rejects the staged-migration approach from the earlier
  handoff draft.
- Add `diagnostics: ExecDiagnostics | None` to `ExecCreateStatesResult` (per
  `item_id`) and `ExecStepBatchResult` (per `node_id`).

**Timing.**
- `acquire_ms`: the lease wait. `PantographManager.get_worker()` already records
  lease-wait timing (`pantograph_manager.py:55`, `_record_lease_wait`); expose it
  per-request rather than only as the pool aggregate.
- `lean_ms`: measured in the router around the worker `create_states` /
  `step_batch` invocation, excluding the lease wait.

**Debug CPU/RAM.** A per-item `debug: bool` flag (mirrors the per-item timeout
shape in `_TimeoutItem`). When true, run psutil monitoring on `PantographWorker`
mirroring `repl.py`'s `_cpu_monitor` / `_mem_monitor` (`server/repl.py:168`–`190`),
active only during the `lean_ms` window. `pantograph_worker.py` has no monitoring
today — add it, gated on the flag so the default path stays cheap.

**Normalizer.** In `pantograph_normalize.py`, add `messages_to_exec_messages()`
that preserves structure; keep `messages_to_texts()` (`pantograph_normalize.py:19`)
for log lines only.

**Header offset.** Positions from Pantograph are body-relative. Clients that send
full code need the header offset applied — reuse `_apply_header_offset` from
`server/routers/check.py` (covered by `tests/test_header_offset.py`); lift it to a
shared helper if exec needs it.

## Phase 2 — `/exec/verify` (conditional on the spike)

If the spike says certification is **C-viable**, add `/exec/verify` that certifies
a complete proof on the warm pool using the minimal mechanism the spike chose
(axiom/sorry check on the complete state, or recompile). This is the per-batch,
reward-grade acceptance for the RL loop — one warm pool, no second service, no
hop. If the spike says **not** C-viable, this phase is replaced by "call the
verify deployment" (path A) and documented as such in the dual-mode plan.

## Acceptance criteria & tests

- `CreateStatesResult` and `StepBatchResult` carry `diagnostics` with `acquire_ms`
  and `lean_ms`; `debug=true` adds `cpu_max` / `memory_max`.
- Errors return `list[ExecMessage]`; a known-bad snippet yields a message with a
  position (per spike availability).
- (If Phase 2) `/exec/verify` accepts a known-good proof and rejects one tainted
  with `sorry` or a disallowed axiom.
- Tests mock the worker to assert timing fields are present and positions land on
  a known bad snippet.

## Non-goals

Infotree / `extract_data` on the Pantograph path. Whole-file verify beyond the
certification the reward signal needs. Any backward-compat retention of the old
flat `messages` shape.
