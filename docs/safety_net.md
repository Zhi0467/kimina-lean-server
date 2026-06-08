# Safety Nets: `/exec` Backend Resource & Admission Control

Design notes and interpretations for the bounded-pool Pantograph `/exec` backend.
Assumptions: one server process per machine; **one client** (one rollout/search
process) per server is the default we size for.

## Layers a request passes through

1. **Client batcher** (`lean_client`, in the caller's process) — shapes the HTTP
   requests. Knobs: `max_items` (request *width*), `max_in_flight_batches`
   (per-client request *count*), `max_wait_ms`, plus per-item
   `acquire_timeout_ms` / `step_timeout_ms`. Derived from the server's
   `/exec/limits` at init via `from_server_limits` where possible. **Advisory.**
2. **HTTP wire** — one `POST /exec/step_batch` carries up to `max_items` items.
3. **Server** — admission (`ExecRequestLimiter`: `max_in_flight`/`max_queued`,
   enforced via 503), hard caps (`max_items_per_step_batch`,
   `max_acquire_timeout_ms`, `max_step_timeout_ms`, etc., enforced via 422),
   execution (worker pool + lanes). **Enforced.**

## Worker pool — collapse to one knob

- **`max_pantograph_workers` = N** is the only worker number to reason about:
  total concurrent Lean processes (machine memory/CPU bound). Default `cpu-1`.
- **`max_lean_processes_per_env_profile`** plays two roles — the manager's
  per-env-profile worker cap *and* the per-request lane fan-out (`max_lanes`).
  Separating it from N only matters for (a) multi-env-profile fairness and
  (b) limiting one request's fan-out to leave room for others. **Neither applies
  to a single-env, single-tenant run**, so default it to **N**: one env_profile
  uses the whole pool, and one request can fan across all workers. Keep it
  overridable for multi-env deployments.
- Unit of work: **1 item ≈ 1 lane ≈ 1 worker.** A request of N items on one
  header → N lanes → N workers, 1 item per lane.

## The substitutes principle (the core interpretation)

Items running concurrently ≈ `(requests in flight) × (items per request)`, and
that competes for N workers. To keep the pool fed you target a small multiple of
N. **Width and count are substitutes — scale only ONE of them with the pool.**
We scale **width**; the counts stay small constants. Scaling both double-counts
and over-subscribes.

## Server admission (enforced)

| Setting | Old | New | Meaning |
|---|---|---|---|
| `max_in_flight_exec_requests` | `-1` | **8** | Global concurrent requests; 503 over. Single client ⇒ equals the client's in-flight. |
| `max_queued_exec_requests` | `-1` | **32** (`4×`) | Backlog admitted before 503. Mostly a buffer for bursts / raw (non-cooperative) HTTP callers. |
| `max_state_store_bytes` | `-1` | **16 GiB** | Disk budget for state files (GC backstop beyond the TTL). |
| `allow_unbounded_exec` | — | **False** | Must be `True` to permit any `-1` cap. |

**Refuse-to-boot:** if any of the three caps is `-1` and `allow_unbounded_exec`
is False, the server refuses to start with a clear error. The safety nets are no
longer silently off.

## Client self-sizing (advisory, from `/exec/limits`)

| Setting (→ client knob) | Old | New | Meaning |
|---|---|---|---|
| `recommended_items_per_step_batch` (→ `max_items`) | 16 | **N** | Request width = pool size: one request fills the pool, 1 item/lane. **The only thing that scales with N.** |
| `recommended_in_flight_step_batches` (→ `max_in_flight_batches`) | 4 | **8** | Per-client request concurrency. 8 in-flight × 1 item/lane ⇒ **~8 items of sequential work per lane** — a backlog deep enough that no worker starves. Constant, not pool-scaled. |

**Client guard:** `from_server_limits` raises if `/exec/limits` advertises an
unbounded cap (backup for the server-side refuse-to-boot).

## Timeout caps

Timeouts are split because waiting for a worker and running Lean are different
failure modes:

- `acquire_timeout_ms`: how long an item may wait for a compatible worker lease.
- `step_timeout_ms`: how long the worker may spend inside Lean for the item.
- Server caps remain `max_acquire_timeout_ms = 600_000` and
  `max_step_timeout_ms = 600_000`; requests above those caps fail with 422.
- Client env defaults keep the same split and still accept legacy `timeout_ms`
  as a compatibility alias for setting both values.

## Advisory vs enforced, per-client vs global

- `recommended_*` is **per-client, advisory** — "how a well-behaved client should
  size itself."
- `max_in_flight` / `max_queued` is **global, enforced** — the hard ceiling
  regardless of who calls. Needed even if every client is perfect, to bound the
  *aggregate* across clients and to cap a raw-HTTP caller that ignores the
  recommendation.
- Relationship: `max_in_flight ≈ (#clients) × recommended_in_flight_step_batches
  + headroom`. **One client ⇒ the two coincide (both 8).** `max_in_flight` does
  **not** scale with N — only request width does. (An earlier `2N` for
  `max_in_flight` was wrong: it assumed thin 1-item requests while the client used
  pool-wide ones. With pool-wide requests both counts stay small.)

## Single process

All server state (StateStore, lifecycle registry, limiter, metrics) lives in
process memory. Running more than one server process against one
`state_store_dir` corrupts token resolution.
- `single_process: bool = True`. At startup, acquire an exclusive lockfile in
  `state_store_dir`; a second server on the same dir fails fast; release on
  shutdown.

## Programmatic launch interface

LeanFoundry should not import server internals. The launch contract is:

- Server side: `server.exec_server_config.ExecServerConfig` is the authoritative
  typed config and maps onto `Settings`.
- CLI side: `python -m server` exposes matching flags, including `--workers`,
  admission caps, state-store cap, timeout caps, recommendations, and
  `--single-process` / `--no-single-process`.
- Client side: `lean_client.ExecServerConfig` mirrors the launch fields using
  stdlib-only code, and `lean_client.launch_server(cfg, server_python=...)`
  builds `python -m server ...` and starts it with `subprocess.Popen`.

## Final defaults

```
max_pantograph_workers              = cpu - 1            (= N)
max_lean_processes_per_env_profile  = N                  (was 4)
max_in_flight_exec_requests         = 8                  (was -1)
max_queued_exec_requests            = 32                 (was -1)
max_state_store_bytes               = 16 * 2**30         (was -1)
allow_unbounded_exec                = False
max_acquire_timeout_ms              = 600_000            (unchanged)
max_step_timeout_ms                 = 600_000            (unchanged)
recommended_items_per_step_batch    = N                  (was 16)
recommended_in_flight_step_batches  = 8                  (was 4)
single_process                      = True
# unchanged hard caps: max_items_per_step_batch=1024, max_tactics_per_step_item=64,
# max_attempts_per_step_batch=8192
```

## Open / to validate (Phase 5 soak)

- The multipliers (`max_queued = 4×`, `16 GiB`, in-flight `= 8`) are heuristics —
  validate under `200×N` and soak runs.
- 8× per-lane item backlog trades deeper queue latency for lane utilization;
  revisit if tail latency matters.
- Pool-wide requests ⇒ coarse, batch-level completion (a request finishes only
  when its slowest item does). If the search needs each node's result ASAP
  (strict one-node-at-a-time), flip to the **thin** design: width = 1,
  `recommended_in_flight ≈ N`, `max_in_flight ≈ N`. Which is right depends on the
  search engine's submission pattern — **TBD**.
