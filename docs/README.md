# Documentation Index

Start with the root [README](../README.md) for the server application: Docker
Hub image pulls, local image builds, runtime configuration, host/container
networking, and smoke tests. Start with [README-client](../README-client.md)
when integrating the packaged `lean-client` dependency into another repo.

The running FastAPI app serves API documentation at:

- `/docs` - Swagger UI
- `/redoc` - ReDoc
- `/api/openapi.json` - OpenAPI JSON

## Mainline References

- `backend-end-plan.md` - production `/exec` backend plan, request lifecycle,
  safety caps, batching contract, state-store ownership, and validation
  checklist.
- `dual-mode-separation-plan.md` - implemented mode split:
  `LEAN_SERVER_MODE=exec` constructs only the Pantograph `/exec` stack, while
  `LEAN_SERVER_MODE=verify` constructs only the legacy REPL checker.
- `exec-observability-plan.md` - exec diagnostics, timing fields, stats, and the
  `/exec/verify` certification path.
- `safety_net.md` - server/client backpressure and `/exec/limits` guardrails.
- `backend_andor_enhancement.md` - backend-only notes for structured goal
  metadata and goal-targeted stepping. Search algorithms remain external.

## Diagnostics And Incident Notes

- `exec-diagnostics-spike.md` - empirical spike result for warm-pool
  `/exec/verify`.
- `pantograph-utf8-panic.md` - canonical writeup for the observed Lean v4.29.1
  UTF-8 diagnostic-rendering panic.

## Research And Historical Notes

- `lean-task-backend-research.md` - in-process Lean task parallelism research;
  useful context, not the mainline backend implementation plan.

## Current Project Boundary

This fork should be read as two deliverables:

- a Dockerized server application published as
  `zzzzhi/kimina-lean-server:latest`, with immutable commit tags for
  reproducible jobs;
- the `lean-client` Python package from `packages/lean-client/`, installed by
  downstream repos and pointed at a running server URL.

Do not point downstream projects at server internals. They should depend on the
client package and connect to a separately launched server URL. For a host
process talking to a local Docker container, that URL is normally
`http://127.0.0.1:8000`; for another container on the same Docker network, use
the service or container DNS name such as `http://server:8000`.
