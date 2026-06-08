# Plan: Dual-Mode Separation (mode gate)

Status: ready for implementation, 2026-06-07.

One process runs exactly one mode. A search deployment constructs only the
Pantograph/exec stack; a verify deployment constructs only the REPL/verify
stack. Neither pays for the other's idle pool, and the other mode's routes are
absent (404), not proxied.

This is **Plan 1 of 2**. It is independent of and unblocks nothing in
[exec-observability-plan.md](exec-observability-plan.md) (Plan 2); the two can
land in either order.

## Problem

`create_app()` ([server/main.py](../server/main.py)) constructs **both** stacks
on every boot, regardless of how the deployment is used:

- Verify/REPL: `Manager(...)` + `await manager.initialize_repls()`
  (`main.py:64`, `main.py:104`) — spawns the Lean REPL subprocess pool.
- Exec/Pantograph: `PantographManager`, `StateStore`, `ItemLifecycleRegistry`,
  `ExecMetrics`, `ExecRequestLimiter`, the state-GC task
  (`main.py:71`–`main.py:103`) — spawns the Pantograph worker pool.
- All four routers are mounted (`main.py:157`–`main.py:173`).
- `db.connect()` runs whenever `database_url` is set (`main.py:55`–`main.py:62`).

For an RL/search deployment the entire REPL pool is dead weight; for a batch
verification deployment the entire Pantograph pool, state store, lifecycle,
metrics, and limiter are dead weight. The idle footprint is roughly doubled.

## Decision

- New setting `mode: Literal["verify", "exec"]`, env `LEAN_SERVER_MODE`
  (the `LEAN_SERVER_` prefix already exists — [settings.py:88](../server/settings.py)).
- **Default `exec`.** Exec is the strategic direction, and a required field with
  no default would break every bare `Settings()` construction in the suite and at
  import (`settings.py:110`, `main.py:187`).
- The value is **`exec`, not `search`** — it matches the code's own vocabulary
  (`routers/exec.py`, `/exec/*`, `exec_*`, `schemas_exec`). Introducing `search`
  would be a third synonym for the same path.
- **In place.** No file moves, no schema changes, no compatibility shims. The
  flat `server/` layout stays; only construction and mounting become conditional.

## Changes

### `server/settings.py`
- Add `mode: Literal["verify", "exec"] = "exec"`. An invalid value fails at
  `Settings()` construction (fail fast). No other field becomes required;
  mode-specific fields keep their defaults and are simply unread by the other
  branch (e.g. `max_repls` is harmless in exec mode).

### `server/main.py` — `lifespan`
Branch on `settings.mode`. Each branch constructs and tears down only its own
stack. The existing teardown already uses `getattr(app.state, …, None)` guards
(`main.py:125`–`main.py:144`), so an unconstructed pool is skipped safely — keep
that pattern.

- **exec branch:**
  `ExecServerConfig.validate_settings(settings)` (move it here — it validates
  exec caps, irrelevant to verify); the `single_process_lock` (it guards the exec
  `state_store_dir`, so it belongs to exec only); `PantographManager`,
  `StateStore`, `ItemLifecycleRegistry`, `ExecMetrics`, `ExecRequestLimiter`,
  `run_state_gc` task.
- **verify branch:**
  `Manager(...)` + `await manager.initialize_repls()`; `db.connect()` if
  `database_url` is set (move the DB connect here — Prisma is only touched by
  `repl.py` and `routers/check.py`).
- **both:** `app.state.settings = settings`; logging banner.

### `server/main.py` — router mounting
Mount conditionally:
- exec mode → `exec_router` + `health_router`.
- verify mode → `check_router` (prefix `/api`) + `backward_router` (`/verify`) +
  `health_router`.

`health_router` (`/health`, `/`) is mounted in both and reads no pool, so it is
mode-agnostic. Verified that `exec_router` reads only exec `app.state` and
`check`/`backward` read only `manager`/`db`, so the foreign routes never touch a
missing pool — conditional mounting is sufficient, no handler guards needed.

### CLI (optional, low priority)
`server/_exec_server_cli.py` + `server/__main__.py`: add a `--mode` argument that
sets `settings.mode`. The env var is the primary path; the flag is convenience.

## Acceptance criteria

- `LEAN_SERVER_MODE=exec`: `/exec/*`, `/health`, `/docs` present; `/api/check`
  and `/verify` return 404. No `Manager`, no REPL subprocess, no `db.connect`.
- `LEAN_SERVER_MODE=verify`: `/api/check`, `/verify`, `/health` present; `/exec/*`
  return 404. No `PantographManager`, `StateStore`, exec limiter/lifecycle/metrics,
  or `single_process_lock`.
- Invalid mode → `Settings()` raises (fail fast).
- Shutdown is clean in both modes (no `AttributeError` from a missing pool).

## Tests — `tests/test_mode_gate.py`

- **Route mounting (fast, no lifespan):** `create_app(Settings(mode="exec"))` vs
  `mode="verify"`; assert the mounted path set per mode (foreign paths absent).
  Mounting happens in `create_app`, outside `lifespan`, so this needs no Lean
  subprocess.
- **State construction (stubbed lifespan):** patch `Manager.initialize_repls` and
  `PantographManager` startup to no-ops; drive lifespan via `asgi-lifespan`;
  assert `app.state` has the chosen pool and lacks the foreign one.

## Explicitly deferred (NOT this plan)

These are separate follow-ups, justified on their own once the gate proves the
split is permanent:

- Folder reorg (`server/` into shared / exec / verify subtrees) — high
  blast-radius import churn.
- Client folder split (`lean_client/{shared,verify,exec}`). The package rename
  `kimina-client → lean-client` is already done (commit `17da800`).
- Dual Docker images / CI matrix (`mode=exec`, `mode=verify`).
- `infotree.py` fate (unused, REPL-specific, data-pipeline code) — rides with the
  client reorg.

## Non-goals

No proxying or dual-stack-in-one-process. No wire-schema changes. No deprecation
windows or compatibility shims (project is unreleased; latest commit is truth).
