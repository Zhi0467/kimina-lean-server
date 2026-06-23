# lean-client

`lean-client` is the downstream-facing Python package for this fork. It contains
HTTP clients, shared wire models, and a search-oriented `/exec` backend facade.
It does not package or import the server implementation.

Downstream repos should treat the server as a separate process or container and
hold only its URL.

## Install From This Fork

For a uv-managed repo:

```toml
[project]
dependencies = ["lean-client"]

[tool.uv.sources]
lean-client = { git = "https://github.com/Zhi0467/kimina-lean-server", subdirectory = "packages/lean-client", rev = "<pinned-sha-or-tag>" }
```

Use a pinned commit or tag. The root repo is a development workspace; the
subdirectory is the package boundary.

## Connect To A Server

The client does not start or import the server by default. Point it at a running
server URL.

For a normal host process talking to the published Docker image on the same
machine:

```sh
docker run --rm \
  --name kimina-lean-server \
  -p 8000:8000 \
  zzzzhi/kimina-lean-server:latest
```

```python
api_url = "http://127.0.0.1:8000"
```

Use `zzzzhi/kimina-lean-server:latest` for the current published image and an
immutable commit tag, such as `zzzzhi/kimina-lean-server:9820b9a`, for
reproducible runs.

URL rules:

- host process to local Docker container: `http://127.0.0.1:8000`
- another container on the same Docker network: `http://server:8000`, replacing
  `server` with the Compose service or container DNS name
- another machine: `http://<reachable-host-or-ip>:<published-port>`

Read `GET /exec/limits` at startup, or let `AsyncLeanExecBackend.connect(...)`
do it, so the caller sizes batch width and in-flight work from the server that
is actually running.

## Exec Backend

Use `AsyncLeanExecBackend` for proof-state search. It owns:

- an `AsyncKiminaClient` HTTP session;
- an `AsyncLeanExecEnv` proof-state wrapper;
- an `AsyncLeanExecBatcher` sized from `GET /exec/limits`.

```python
from lean_client import AsyncLeanExecBackend

async with await AsyncLeanExecBackend.connect(
    "http://localhost:8000",
    env_profile="default",
) as backend:
    created = await backend.create_states(
        "run_123:theorem_42:attempt_1",
        "theorem t (n : Nat) : n + 0 = n := by\n  sorry",
    )
    state_token = created.items[0].states[0].state_token

    stepped = await backend.step(
        "run_123:theorem_42:attempt_1:n0",
        state_token,
        ["simp", "rw [Nat.add_comm]"],
    )

    verdict = await backend.verify_one(
        "run_123:theorem_42:attempt_1",
        "theorem t (n : Nat) : n + 0 = n := by simp",
        "t",
    )

    await backend.cleanup(["run_123:theorem_42:attempt_1"])
```

Use one unique `item_id` per search attempt. A `node_id` should carry enough
caller-side identity to map a result back to the proof graph. `step` coalesces
requests into `/exec/step_batch`, serializes live requests for the same
`item_id`, retries observed `overloaded` results, and refuses servers that
advertise unbounded safety caps.

The composed layers remain available as `backend.client`, `backend.env`, and
`backend.batcher` for escape hatches, but routine search code should use the
facade.

## Direct Client Calls

Lower-level calls are available when a caller needs exact request control:

```python
from lean_client import AsyncKiminaClient, ExecCreateStateItem

async with AsyncKiminaClient(api_url="http://localhost:8000") as client:
    response = await client.exec_create_states(
        "default",
        [
            ExecCreateStateItem(
                item_id="attempt-1",
                code="theorem t : True := by\n  sorry",
            )
        ],
    )
```

Prefer the backend facade unless you are building a new adapter layer.

## Programmatic Server Launch

The supported production deployment runs the server as its own process: a Docker
container or `python -m server` from a server checkout. The helper below is a
development convenience for jobs that need to spawn a local server process while
keeping the downstream repo independent of server imports.

```python
from lean_client import ExecServerConfig, launch_server

cfg = ExecServerConfig(
    host="127.0.0.1",
    port=8000,
    workers=8,
    state_store_dir="/tmp/kimina-lean-state",
)
process = launch_server(cfg, server_python="/path/to/server/.venv/bin/python")
```

`launch_server` builds a command like:

```sh
python -m server --host 127.0.0.1 --port 8000 --workers 8 ...
```

The `server_python` interpreter must be able to run the server checkout. In
production, pass a deployed server URL to the client instead of launching the
server from the consumer repo.

Important launch defaults:

- `workers` controls `LEAN_SERVER_MAX_PANTOGRAPH_WORKERS`; by default it is
  sized from CPU count and a conservative free-node memory budget.
- `max_lean_processes_per_env_profile` defaults to `workers`.
- `recommended_items_per_step_batch` defaults to `workers`.
- `max_in_flight_exec_requests` defaults to `min(workers, 8)`.
- `max_queued_exec_requests` defaults to `min(4 * max_in_flight, 32)`, with a
  floor of `4`.
- `max_state_store_bytes` defaults to `16 * 2**30`.
- `single_process` defaults to `True`.

Setting any bounded exec cap to `-1` requires `allow_unbounded_exec=True`.

## Legacy Verify-Mode Client

`KiminaClient.check(...)` talks to the legacy whole-code checking API. It only
works against a server started with `LEAN_SERVER_MODE=verify`; the default
exec-mode server does not mount `/api/check` or `/verify`.

```python
from lean_client import KiminaClient

client = KiminaClient(api_url="http://localhost:8000")
client.check("#check Nat")
```

Use this path for compatibility with older checking workflows, not for
search-time proof-state execution.
