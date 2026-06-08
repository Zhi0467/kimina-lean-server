# Docs

- `backend-end-plan.md` -- authoritative production `/exec` backend plan and
  roadmap.
- `dual-mode-separation-plan.md` -- Plan 1/2: one process per mode
  (`LEAN_SERVER_MODE=verify|exec`), so a deployment never constructs both pools.
- `exec-observability-plan.md` -- Plan 2/2: structured exec diagnostics, split
  timing, and the spike deciding whether exec can self-certify proofs.
- `lean-task-backend-research.md` -- research notes for in-process Lean task
  parallelism; not the mainline backend plan.
- `pantograph-utf8-panic.md` -- canonical writeup for the observed Lean v4.29.1
  UTF-8 diagnostic-rendering panic.
- Server API docs are available from the running FastAPI app via Swagger/ReDoc.
- Client docs should point to the packaged `lean_client` API.
