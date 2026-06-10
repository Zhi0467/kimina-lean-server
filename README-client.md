# Kimina client

`lean-client` is the downstream-facing Python package for Kimina Lean Server.
It exposes stable client-side APIs for Lean checking and `/exec` proof-state
execution. A repo such as LeanFoundry should depend on this package and avoid
importing server modules such as `server.settings`, `server.main`, or
`server.exec_backends`.

## Install from another repo

For a uv-managed downstream repo, depend on the package subdirectory:

```toml
[project]
dependencies = [
    "lean-client",
]

[tool.uv.sources]
lean-client = { git = "https://github.com/project-numina/kimina-lean-server", subdirectory = "packages/lean-client", rev = "<pinned-sha-or-tag>" }
```

The server app is not imported by the downstream repo. Deploy or launch it as
an app process, then pass its URL to `AsyncKiminaClient`.

## Whole-code checking

```python
from lean_client import KiminaClient

# Defaults to LEAN_SERVER_API_URL or http://localhost:8000.
client = KiminaClient()
client.check("#check Nat")
```

## Exec environment client

LeanFoundry/SearchEngine code should talk to the search-facing exec backend
instead of hand-writing `/exec` JSON. The server runs as a **separate process**
(its Docker image or `python -m server` from a checkout); the client only
connects to it over HTTP.

`AsyncLeanExecBackend` is the single entry point — one object that owns the HTTP
client, the proof-state env wrapper, and the step batcher. Construct it with
`connect()` (which reads `/exec/limits` to size batching) and use it as an async
context manager.

```python
from lean_client import AsyncLeanExecBackend

async with await AsyncLeanExecBackend.connect(
    "http://localhost:8000", env_profile="lean_init_test"
) as backend:
    created = await backend.create_states(
        "run_123:theorem_42:attempt_1",
        "theorem t (n : Nat) : n + 0 = n := by\n  sorry",
    )
    state_token = created.items[0].states[0].state_token

    stepped = await backend.step(                    # coalesced into /exec/step_batch
        "run_123:theorem_42:attempt_1:n0",
        state_token,
        ["simp", "rw [Nat.add_comm]"],
    )

    verdict = await backend.verify_one(              # warm-pool proof certification
        "run_123:theorem_42:attempt_1",
        "theorem t (n : Nat) : n + 0 = n := by simp",
        "t",
    )

    await backend.cleanup(["run_123:theorem_42:attempt_1"])
```

Use one unique `item_id` per search attempt; a `node_id` should carry enough
caller-side identity to map the response back to the proof graph. `step` derives
request width and in-flight limits from `/exec/limits`, serializes live requests
for the same `item_id`, retries observed `overloaded` results, and refuses a
server that advertises unbounded safety caps. The composed layers remain
available as `backend.client` / `backend.env` / `backend.batcher` for escape
hatches; routine search code should not need them.

## Programmatic server launch (dev convenience only)

> The supported deployment runs the server as its **own process** — its Docker
> image, or `python -m server` from a checkout — reached over HTTP. The helper
> below is a dev/test convenience for spawning that process from a script; it
> requires the `server` checkout to be importable in `server_python`'s
> environment and is **not** how a consumer like LeanFoundry runs in production
> (there the server is a separate container/process and the client holds only its
> URL).

Some downstream workflows want a vLLM-style "start the service for this job"
knob while keeping the downstream repo oblivious to server internals. Use the
client-side launch mirror:

```python
from lean_client import ExecServerConfig, launch_server

cfg = ExecServerConfig(
    host="127.0.0.1",
    port=8000,
    workers=8,
    state_store_dir="/tmp/leanfoundry-state",
)
process = launch_server(cfg, server_python="/path/to/server/.venv/bin/python")
```

`launch_server` builds:

```sh
python -m server --host 127.0.0.1 --port 8000 --workers 8 ...
```

The client package does not import the server package. `server_python` must point
at an environment where the server checkout can run `python -m server`.

Important safety defaults in `ExecServerConfig`:

- `workers`: total Pantograph Lean worker processes.
- `max_lean_processes_per_env_profile`: defaults to `workers`.
- `recommended_items_per_step_batch`: defaults to `workers`.
- `max_in_flight_exec_requests`: defaults to `8`.
- `max_queued_exec_requests`: defaults to `32`.
- `max_state_store_bytes`: defaults to `16 * 2**30`.
- `single_process`: defaults to `True`; do not run multiple server processes
  against one `state_store_dir`.

Setting any bounded cap to `-1` requires `allow_unbounded_exec=True`.

## Legacy verification path

```python
from lean_client import KiminaClient

client = KiminaClient()
client.check("#check Nat")
```
