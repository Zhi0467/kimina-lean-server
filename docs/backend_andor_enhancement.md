# Backend Enhancement: Structured Goals + AND/OR Independent-Subgoal Search

Design notes for extending the `/exec` backend so the search engine
(LeanFoundry / Aristotle) can run MCGS over an AND/OR hypergraph. Companion to
`docs/canonical_search.md`. Principle: the **backend exposes capabilities,
information, and soundness invariants; the AND/OR algorithm lives entirely in
the search engine.**

## The search model (recap, grounded in canonical_search.md)

- **State = OR-node** (a Lean proof state). Proved if ANY action succeeds.
- **Action = AND-node** (a tactic). Succeeds only if ALL its resulting states
  are proved.
- A tactic can produce multiple goals → AND-children.
- **Split rule** (canonical_search.md line 218): goals split into independent
  states **only if none share metavariables** (`sibling_dep` empty). Coupled
  goals stay bundled as one OR-node (the policy focuses in-band).
- Node identity = *(goal exprs, local context, variable names)*; dedup across
  paths makes the tree a hypergraph (the "G" in MCGS).
- Per-OR-node signal: a node is proved when its focused subgraph reaches zero
  in-scope goals. Theorem-done = AND-combination of the per-goal nodes, tracked
  by the search, not the backend.

## Key mechanism (PyPantograph + verified spikes)

- `goal_resume` is **not** an extraction primitive — it brings *already
  suspended* goals back into scope and errors on in-scope goals. Not used.
- **Sibling-dragging** (a step carrying the other goals forward) happens **only
  in automatic mode** (`auto_resume=True`, the default we currently run).
- **`automaticMode: False` + `Site(goal_id, auto_resume=False)`** *suspends* the
  siblings instead of dragging them → stepping a chosen goal yields a clean,
  undragged subtree. (Spike: focusing goal 0 of `True ∧ True` left 0 in-scope
  goals; goal 1 was suspended, not carried.) This is exactly the "independent
  subgoal" behavior — achieved **without** extraction or separate tokens.
- `sibling_dep` (metavariable coupling) is populated **only** when the server is
  started with `options={"printDependentMVars": True}`. (Spike: empty for
  `constructor` on `∧`; `{1}` for the coupled witness of `apply Exists.intro`.)
  We currently start with neither flag (automatic mode, coupling off).

### Why this revives `goal_id` and drops extraction

The earlier "extraction is required, `goal_id` is not enough" conclusion assumed
automatic-mode dragging. With `auto_resume=False` there is no dragging, so a
multi-goal bundle (token `T`) is represented by the search as per-goal OR-nodes
`(T, goal_id)`; expanding goal *i* = step `T` with `Site(goal_id=i,
auto_resume=False)`; children are fresh tokens, the parent `T` is shared. Dedup
still works because the search keys on the **in-scope goal content**, not the
token. No `/exec/factor`, no `goal_resume`, no `goal_continue`, no pre-extracted
tokens.

### Two resolved objections

- `is_solved=True` on a focused-goal state = *that goal-node's subgraph is
  proved* (the correct per-OR-node signal). It does not mean theorem-done, and
  that's fine — theorem-done is the search-level AND-combination.
- Save/load need **not** preserve suspended siblings: each goal is searched in
  its own branch off the shared parent, and final-proof assembly is **text-level**
  (collect tactics along the subgraph → emit a Lean file → re-verify with the
  kernel), so there is no state-level splicing.

## Backend ↔ search mapping (validated)

How the PUCT/MCGS loop (canonical_search.md) uses the backend. The search
algorithm — UCB over actions, LCB over AND-children, `AND = min` / `OR = max`
value backup, status propagation, dedup — lives **entirely in the search
engine**; the backend exposes only the primitives below.

| Search operation | Backend primitive |
|---|---|
| **Expand** a frontier OR-node `(token, goal_id)`: sample K tactics | one `step_batch` item: `state_token=token`, `goal_id`, `auto_resume=False`, `tactics=[…]` → K AND actions, ~K·ḡ child OR-nodes (fresh tokens) |
| **Represent / value** an OR-node (value model input) | the node's `GoalInfo` (`target`, `hypotheses`, `name`) from the result — **no stepping** |
| **Split decision** (independent vs coupled child goals) | `sibling_dep` on each returned goal (empty ⇒ own OR-node; non-empty ⇒ stay bundled, one OR-node) |
| **Node identity / dedup** | `(target, hypotheses, variable names)` from `GoalInfo` — keyed on content, not `(token, goal_id)` |
| **Solved leaf** of a focused subgraph | `status="complete"` / zero in-scope goals on that node |
| **Final proof** once root PROVED | text-level: tactics along the proof subgraph → `/verify` |

Value model runs on **state nodes**, not actions (canonical_search.md line 356):
action values are search-side bookkeeping aggregated from child OR-node values.
So the backend never needs an "action" or "value" concept — it returns states.

## Design changes

### Change 1 — Structured goal objects + coupling (foundation)
- Start workers with `options={"printDependentMVars": True}`.
- Replace `goals: list[str]` with `goals: list[GoalInfo]` in `create_states`
  (`StateInfo`) and `step_batch` (`StepResult`):
  ```
  GoalInfo: { name, pretty, target, hypotheses:[{names,type,value}], sibling_dep:[int] }
  ```
  Carries everything for the split rule (`sibling_dep`) and node identity
  (target + hypothesis types + var names). `pretty` keeps the old string for
  model input / `/verify`.
- Update `pantograph_normalize` (stop flattening), `schemas_exec`, client
  `exec_models`; migrate tests.

### Change 2 — Goal-targeted stepping (NO server-mode change)
- **Keep `automaticMode: True`** (the default). Do **not** switch to
  non-automatic mode — Check 1 proved that flipping the global mode suspends
  sibling goals on default steps and breaks the existing whole-state path.
- `StepBatchItem` gains optional `goal_id: int | None` and
  `auto_resume: bool | None`, passed as `Site(goal_id, auto_resume)` to
  `goal_tactic_async`. When unset, behaviour is identical to today.
- Focused/independent stepping is **opt-in per call**: the search passes
  `goal_id=i, auto_resume=False`, which focuses goal *i* and suspends siblings
  **even in automatic mode** (verified, Check 3). So the existing path is
  untouched and the new capability is purely additive.

### Change 3 — Recombination / final proof
- Text-level: the search assembles tactics along the proof subgraph and
  re-verifies via the existing `/verify`. No new backend surface.

### Non-changes
- No `/exec/factor`, `goal_resume`, `goal_continue`, or separate extraction
  tokens. No `Site` is needed for coupled bundles (one node, in-band focusing).

## Open checks before building — RESOLVED (spike 2026-05-31)

1. **Default stepping** — RESOLVED, and it reshaped Change 2. Switching to
   `automaticMode: False` **breaks** the existing path: a default `rfl` on goal 0
   of a 2-goal state suspended goal 1 and reported `solved=True` (0 in-scope
   goals). **Fix: keep `automaticMode: True`; opt into focusing per call via
   `Site(goal_id, auto_resume=False)`, which works in automatic mode.** No global
   mode change.
2. **Save/load roundtrip** — PASS. A focused state's in-scope goal (`a = a`)
   survives `goal_save`/`goal_load`; suspended siblings are not needed.
3. **In-scope-only goals** — PASS. The returned `goals` list contained only the
   in-scope goal; the suspended sibling did not appear (so it cannot pollute node
   identity / dedup).

## Sequencing
1. Run Open Checks 1–3 (this pass).
2. Change 1 (structured goals + `printDependentMVars`).
3. Change 2 (add optional `goal_id`/`auto_resume`; keep automatic mode).
4. Confirm text-level recombination via `/verify`.

## Risks
- `printDependentMVars` may alter goal text / add cost → verify negligible.
- Split search mints more state tokens per request → reinforces the
  `max_state_store_bytes` budget + prompt `cleanup` from `safety_net.md`.
- A non-fatal Lean backtrace was observed at worker startup during spikes
  (consistent with the known continue-on-panic, `docs/pantograph-utf8-panic.md`);
  results were unaffected, but worth watching when `printDependentMVars` is on.

## Status
- Mechanism verified by spike (2026-05-31): `goal_resume` is not extraction;
  `auto_resume=False` focuses without dragging **even in automatic mode**;
  `printDependentMVars` populates `sibling_dep`.
- Open Checks 1–3: **resolved** — keep automatic mode, focusing is per-call
  opt-in; save/load and in-scope-only behaviour confirmed.
- Net design: **additive only** — structured goals + `printDependentMVars`
  (Change 1) and optional `goal_id`/`auto_resume` on step (Change 2). No server
  mode change, no extraction endpoint. Ready to implement.
