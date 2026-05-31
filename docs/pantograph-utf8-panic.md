# Handoff: Pantograph `String.Slice.pos!` UTF-8 panic

Self-contained. Reader knows Lean/Pantograph but not the originating conversation.

## Symptom

During `/exec/step_batch` (and possible on `/verify`), the Lean worker prints to
stderr:

```
PANIC at String.Slice.pos! Init.Data.String.Basic:1669:4: Offset is not at a valid UTF-8 character boundary
backtrace:
0  pantograph-repl  ... print_backtrace
1  pantograph-repl  ... lean_panic_impl
...  (~100 stripped/optimized interpreter frames, under evalTactic)
```

Seen ~5 times in a small benchmark; ~28 times in a heavier reproduction run.

## Root cause (Lean v4.29.1)

- `String.Slice.pos!` (`.../Init/Data/String/Basic.lean:1665`) takes a raw **byte**
  offset and panics unless it lands on a UTF-8 character boundary.
- Lean proof/goal/source text is full of multi-byte glyphs (`⊢ → ℝ ≤ λ √ ⟨⟩` …,
  each 2–3 bytes). Some code computes an offset that lands **mid-glyph**.
- The new `String.Slice` API (Lean ~4.29) validates this strictly; the old
  `Substring` API was silently lenient. So this is a **latent upstream
  Lean/Mathlib/Pantograph string bug surfaced loudly** — not in this repo's Python.

## Where it fires -- NOT the text→logic→kernel path (key finding)

Evidence from the v4.29.1 toolchain source
(`~/.elan/toolchains/leanprover--lean4---v4.29.1/src/lean`):

- **The normal parser path does not call it.** `grep -r "\.pos!" Lean/Parser` →
  **0 hits**. The tokenizer/parser uses character-aware iterators, so the
  ordinary term/tactic pipeline that builds the `Expr` the kernel checks does not
  use this `pos!`.
- There is a `Lean/DocString/Parser.lean` caller, so do not state "no parser
  anywhere." That caller is not the observed tactic-execution path.
- The runtime callers are **diagnostic position-mapping**, run *after* the verdict:
  - `Lean/Data/Position.lean:108 ofPosition` — maps `(line,col)` → source byte
    offset to attach a location to an **error message**.
  - `Lean/Meta/TryThis.lean getIndentAndColumn` — indentation for a "try this"
    suggestion.

So the panic happens **while Lean renders an error/suggestion location**, i.e. the
tactic's success/failure was already decided before this code runs.

## Behavior (continue-on-panic)

Pantograph does **not** call `lean_set_exit_on_panic`, and **there is no env var**
to enable it (`lean_set_exit_on_panic` is a C function only; `lean.h:316`). So
`panic!` prints the backtrace, returns `Inhabited.default`, and **continues**. The
worker process survives (warm latency unaffected; no reboot observed).

## Empirical evidence (`/exec`, production = continue mode)

Reproduced with the benchmark's shape (8 candidate tactics/item, gold+distractors,
BFS) at concurrency 1, correlating each panic to the request it occurred in. The
panic is printed to stderr during request processing, before the uvicorn access
line, so request-level attribution is reliable in this single-client run.

- First run: **28 panics across 18 requests / 712 results.**
- Rich rerun capturing full tactics/messages: again **28 panics across 18 requests
  / 712 results**.
- Every panicked request had **3–7 genuine `error` results**; panics-per-request
  was always ≤ that request's error count.
- **0 / 712** results had corrupted text (no U+FFFD) or `open`-without-`state_token`.
- **No** panic landed on a success-only request; **no** `open`/`complete` in a
  panicked request was corrupted.
- Returned error messages in panicked requests looked coherent:
  - `Unknown identifier d` for tactics that referenced `d`;
  - `Try this: ring_nf` next to a `ring` failure;
  - normal `linarith failed...` messages for failed `nlinarith`.
- **0** captured goals and **0** captured messages contained replacement-character
  corruption.
- Across the two runs, all 56 panic backtraces had the same top runtime path:
  `Lean.Meta.Tactic.TryThis.getIndentAndColumn` →
  `Suggestion.processEdit` → `Hint.mkSuggestionsMessage` →
  `TryThis.addSuggestion`.

Repro tool: `examples/exec_panic_repro.py` (run against a live server; then attribute
panics in the server log to step requests by order — panics print during processing,
before the uvicorn access line).

Because one `/exec/step_batch` request contains multiple tactic attempts, this
only identifies the request, not the exact tactic result whose diagnostic path
panicked.

## `/verify` / REPL check

`/verify` uses the REPL path (`server/routers/backward.py` →
`server/routers/check.py` → `server/repl.py`) and the same Lean v4.29.1
toolchain, so the same Lean diagnostic code can in principle be reached.

Empirical probe:

- Direct REPL path with `import Mathlib`, full theorem bodies, and tactics taken
  from the `/exec` requests that emitted panics.
- 220 cases without infotree: **0 panics**.
- 120 cases with `infotree=full`: **0 panics**.
- **0** replacement-character message corruption.

So the earlier hypothesis that `/verify` is "more exposed" was too strong. It is
better to say: `/verify` shares the panic-prone Lean code, but the panic was not
reproduced there on the panic-derived workload.

## Conclusions

- **Not a soundness issue.** `complete`/`open` are real and faithful; the kernel
  path never uses `pos!`; any accepted proof is re-checked by `/verify`.
- A **"panicked error" is still a real error**. The tactic failed; the panic fired
  while Lean was rendering suggestion/diagnostic metadata for that failure.
- The captured messages did **not** look wrong. Do not claim the panic corrupts
  returned message text unless we catch a concrete mismatch.
- The most plausible diagnostic risk is `line:col` / range / "try this" metadata,
  especially because the observed call site is `TryThis.getIndentAndColumn`.
- `/verify` shares the same Lean toolchain, but direct REPL probes did not
  reproduce the panic.

## Impact on downstream (rollout / revision SFT)

- Search/rollout branching on `status` and `state_token` is unaffected by the
  observed panic.
- Consumers keyed on error **message text** can likely use the observed messages:
  they remained coherent in the reproduced panicked requests.
- Consumers keyed on exact error **location (`line:col`)** or `Try this`
  suggestion placement should treat panic-adjacent diagnostics as lower trust.
- If strict SFT provenance matters, record a request-level `panic_seen` marker
  when available and decide during dataset construction whether to keep, filter,
  or down-weight those diagnostics. The current response schema does not identify
  which tactic's diagnostic rendering emitted the panic.

## Recommended action

No backend code change is required right now.

Keep this as a documented upstream Lean/Pantograph diagnostic-rendering bug and
keep the repro script. Revisit code mitigation only if we observe one of:

- bad returned diagnostic text, not just a stderr panic;
- worker instability after a panic;
- `/verify` panics on real user workloads;
- a product requirement to tag or filter panic-adjacent diagnostics for SFT.

Possible future mitigation, if needed:

1. Detect `PANIC` in worker/REPL stderr.
2. Recycle the process after the current request.
3. Add a response/log marker such as `panic_seen=true` so SFT pipelines can
   decide whether to keep the diagnostics.
4. Report the `String.Slice` offset bug upstream (Lean/Mathlib/Pantograph).

## Facts to keep handy

- Both `repl/lean-toolchain` and `third_party/PyPantograph/pantograph/lean-toolchain`
  are `leanprover/lean4:v4.29.1`. `settings.lean_version = v4.26.0` is a **stale
  label**; the actual binaries are v4.29.1, which is why both `/exec` and `/verify`
  share the panic-prone code.
- `/verify` route: `server/routers/backward.py:17` (`one_pass_verify_batch`), via
  the `Manager`/`Repl` (not Pantograph).
