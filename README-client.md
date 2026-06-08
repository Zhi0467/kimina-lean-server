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

LeanFoundry/SearchEngine code should call the search-facing exec wrapper instead
of hand-writing `/exec` JSON. The SearchEngine owns graph/node semantics;
`AsyncLeanExecEnv` owns the HTTP calls; `AsyncLeanExecBatcher` shapes
proof-state expansion items into `/exec/step_batch` requests.

```python
from lean_client import AsyncKiminaClient, AsyncLeanExecBatcher, AsyncLeanExecEnv

async with AsyncKiminaClient(api_url="http://localhost:8000") as client:
    env = AsyncLeanExecEnv(client, env_profile="lean_init_test")
    batcher = await AsyncLeanExecBatcher.from_server_limits(env)

    created = await env.create_states(
        "run_123:theorem_42:attempt_1",
        "theorem t (n : Nat) : n + 0 = n := by\n  sorry",
    )
    state_token = created.items[0].states[0].state_token

    stepped = await batcher.submit_step(
        "run_123:theorem_42:attempt_1:n0",
        state_token,
        ["simp", "rw [Nat.add_comm]"],
    )

    await env.cleanup(["run_123:theorem_42:attempt_1"])
```

Use one unique `item_id` per search attempt. A `node_id` should include enough
caller-side identity to map the response back to the proof graph. The batcher
derives request width and in-flight limits from `/exec/limits`, serializes live
requests for the same `item_id`, retries observed `overloaded` results, and
refuses to configure itself from a server that advertises unbounded safety caps.

## Programmatic server launch

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
