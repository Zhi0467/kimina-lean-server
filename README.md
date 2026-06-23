# Kimina Lean Server Fork

This fork packages a Lean execution server for downstream search and RL
systems. It has drifted from the upstream Kimina Lean Server shape in one
important way:

- the server is deployed as an application process, normally a Docker image;
- `packages/lean-client/` is the only vendored Python package downstream repos
  should depend on;
- the root `pyproject.toml` is a local development environment
  (`tool.uv.package = false`), not a distributable server package.

The default server mode is `exec`, which serves the Pantograph-backed proof-state
API under `/exec/*`. The legacy whole-file REPL checker still exists, but only
when the server is started in `verify` mode.

## Repository Layout

- `server/` - FastAPI server, mode gate, `/exec` routes, legacy verify routes,
  Pantograph worker pool, state store, and runtime settings.
- `packages/lean-client/` - packaged downstream client (`lean-client`) and shared
  wire models.
- `docs/` - design notes, backend plans, diagnostics notes, and research context.
- `Dockerfile` - production server image build. It installs Lean/mathlib and runs
  the app from the built virtualenv.
- `compose.yaml` - local compose entrypoint for building/running the server
  image.

## Server Modes

`LEAN_SERVER_MODE=exec` is the default and production path for proof search. It
mounts:

- `GET /health`
- `POST /exec/create_states`
- `POST /exec/step_batch`
- `POST /exec/verify`
- `POST /exec/cleanup`
- `POST /exec/cancel`
- `GET /exec/limits`
- `GET /exec/stats`

`LEAN_SERVER_MODE=verify` is the legacy REPL mode. It mounts `/api/check`,
`/verify`, and `/health`, and does not construct the Pantograph `/exec` pool.

Run one process per state-store ownership domain. The default
`LEAN_SERVER_SINGLE_PROCESS=true` takes a lock under `LEAN_SERVER_STATE_STORE_DIR`
to prevent two server processes from sharing one local state store.

## Docker Server Image

Use the published server image when you only need to run the server:

```sh
docker pull zzzzhi/kimina-lean-server:latest
docker pull zzzzhi/kimina-lean-server:9820b9a
```

`latest` is the moving Docker Hub tag for the current published server image.
Use an immutable commit tag, such as `9820b9a`, for reproducible jobs.

Run the published exec-mode server:

```sh
docker run --rm \
  --name kimina-lean-server \
  -p 8000:8000 \
  zzzzhi/kimina-lean-server:latest
```

Build a local server image from this checkout when you are testing local source
changes:

```sh
docker build -t kimina-lean-server:local .
```

The build runs `setup.sh`, installs Lean `v4.29.1`, downloads/builds mathlib, and
generates the Prisma client. The legacy REPL is skipped by default because the
default image is for `exec` mode. Build the REPL only for a verify-mode image:

```sh
docker build --build-arg SETUP_REPL=1 -t kimina-lean-server:verify .
```

Run a small local exec-mode server when sharing the host with other jobs:

```sh
docker run --rm \
  --name kimina-lean-server \
  -p 8000:8000 \
  -e LEAN_SERVER_MAX_PANTOGRAPH_WORKERS=1 \
  -e LEAN_SERVER_MAX_LEAN_PROCESSES_PER_ENV_PROFILE=1 \
  -e LEAN_SERVER_RECOMMENDED_ITEMS_PER_STEP_BATCH=1 \
  kimina-lean-server:local
```

Without explicit caps, the exec server sizes itself from CPU count and a
conservative free-node memory budget. On this 256-CPU, 755 GiB node, that
default sized to 60 Pantograph workers, 60 Lean processes per env profile, 8
admitted exec requests, and 32 queued exec requests. Override the worker
variables when a shared node needs a smaller footprint.

The app binds `0.0.0.0` inside the container. Docker `-p 8000:8000` publishes
container port `8000` to host port `8000`, so a process on the same host, such
as a proof-search process outside the container, should connect to
`http://127.0.0.1:8000`. A process on another machine must use the node's
reachable host/IP and whatever firewall or scheduler port publishing applies. A
client running in another container on the same Docker network should use the
server container or Compose service name, for example `http://server:8000`.

Smoke-test the running container:

```sh
curl -fsS http://127.0.0.1:8000/health | jq
curl -fsS http://127.0.0.1:8000/exec/limits | jq
```

Minimal proof-state smoke:

```sh
create=$(curl -fsS -X POST http://127.0.0.1:8000/exec/create_states \
  -H "Content-Type: application/json" \
  --data '{"env_profile":"default","items":[{"item_id":"docker-smoke","code":"theorem docker_smoke : True := by\n  sorry","acquire_timeout_ms":600000,"step_timeout_ms":600000}]}')

token=$(echo "$create" | jq -r '.items[0].states[0].state_token')

curl -fsS -X POST http://127.0.0.1:8000/exec/step_batch \
  -H "Content-Type: application/json" \
  --data "{\"items\":[{\"node_id\":\"docker-smoke:n0\",\"state_token\":\"$token\",\"tactics\":[\"trivial\"],\"acquire_timeout_ms\":600000,\"step_timeout_ms\":600000}]}" | jq
```

The image starts with `.venv/bin/python -m server`; it should not run `uv sync`
at container startup.

## Local Development

From a fresh checkout:

```sh
cp .env.template .env
uv sync --dev
uv run prisma generate
bash setup.sh
uv run python -m server
```

`bash setup.sh` installs Lean and mathlib for the `/exec` path. To develop the
legacy verify path as well:

```sh
SETUP_REPL=1 bash setup.sh
LEAN_SERVER_MODE=verify uv run python -m server
```

The module runner accepts app-launch flags used by local job launchers:

```sh
uv run python -m server \
  --host 127.0.0.1 \
  --port 8000 \
  --workers 8 \
  --state-store-dir /tmp/kimina-lean-state
```

Environment variables use the `LEAN_SERVER_` prefix. The most important
production controls are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LEAN_SERVER_MODE` | `exec` | `exec` for Pantograph `/exec`; `verify` for legacy REPL routes. |
| `LEAN_SERVER_HOST` | `0.0.0.0` | Bind host. |
| `LEAN_SERVER_PORT` | `8000` | Bind port. |
| `LEAN_SERVER_ENVIRONMENT` | `dev` | `dev` or `prod`; Docker sets `prod`. |
| `LEAN_SERVER_LEAN_VERSION` | `v4.29.1` | Lean toolchain label used by the server. |
| `LEAN_SERVER_PROJECT_DIR` | `mathlib4` | Lean project path; Docker sets `/mathlib4`. |
| `LEAN_SERVER_REPL_PATH` | `repl/.lake/build/bin/repl` | Legacy verify-mode REPL binary path. |
| `LEAN_SERVER_MAX_PANTOGRAPH_WORKERS` | memory-aware worker count | Total `/exec` Lean worker count; default caps CPU count by a conservative free-node memory budget: half of total/cgroup memory minus 16 GiB headroom, divided by about 6 GiB per warmed Mathlib worker. |
| `LEAN_SERVER_MAX_LEAN_PROCESSES_PER_ENV_PROFILE` | worker count | Per-env-profile worker lane cap. |
| `LEAN_SERVER_MAX_IN_FLIGHT_EXEC_REQUESTS` | `min(worker count, 8)` | Global admitted `/exec` HTTP requests. |
| `LEAN_SERVER_MAX_QUEUED_EXEC_REQUESTS` | `min(4 * in-flight, 32)` | Global queued `/exec` requests before 503 backpressure. |
| `LEAN_SERVER_MAX_STATE_STORE_BYTES` | `17179869184` | State-store disk budget. |
| `LEAN_SERVER_ALLOW_UNBOUNDED_EXEC` | `false` | Must be true to permit any `-1` exec cap. |
| `LEAN_SERVER_RECOMMENDED_ITEMS_PER_STEP_BATCH` | worker count | Client batch-width recommendation exposed by `/exec/limits`. |
| `LEAN_SERVER_RECOMMENDED_IN_FLIGHT_STEP_BATCHES` | `8` | Client in-flight recommendation exposed by `/exec/limits`. |
| `LEAN_SERVER_STATE_STORE_DIR` | `.leanfoundry-state` | Local state-token store. |
| `LEAN_SERVER_SINGLE_PROCESS` | `true` | Lock the state store and fail fast on a second server process. |
| `LEAN_SERVER_API_KEY` | unset | Optional bearer-token authentication. |

## Client Package

Downstream repos should depend on `lean-client`, not on `server`.

```toml
[project]
dependencies = ["lean-client"]

[tool.uv.sources]
lean-client = { git = "https://github.com/Zhi0467/kimina-lean-server", subdirectory = "packages/lean-client", rev = "<pinned-sha-or-tag>" }
```

Exec-mode proof search code should use the high-level backend facade:

```python
from lean_client import AsyncLeanExecBackend

async with await AsyncLeanExecBackend.connect(
    "http://localhost:8000", env_profile="default"
) as backend:
    created = await backend.create_states(
        "attempt-1",
        "theorem t : True := by\n  sorry",
    )
    state_token = created.items[0].states[0].state_token
    stepped = await backend.step("attempt-1:n0", state_token, ["trivial"])
```

See [README-client.md](./README-client.md) for the client boundary, launch
helper, server URL patterns, and legacy verify-mode notes.

## Tests

Generate Prisma once in a fresh local virtualenv:

```sh
uv run prisma generate
```

Run the normal test suite:

```sh
uv run pytest
```

The default pytest markers skip performance, match, and REPL integration tests.
Run those explicitly when needed:

```sh
uv run pytest -m perfs
uv run pytest -m match
SETUP_REPL=1 bash setup.sh
uv run pytest -m verify
```

For Docker changes, build the image and run the smoke commands in
[Docker Server Image](#docker-server-image).

## Docs

Start with [docs/README.md](./docs/README.md). The running FastAPI app also
serves Swagger at `/docs`, ReDoc at `/redoc`, and OpenAPI JSON at
`/api/openapi.json`.

## Attribution

This fork is derived from Project Numina's Kimina Lean Server and keeps the MIT
license. The original technical report remains useful background:

```bibtex
@misc{santos2025kiminaleanservertechnical,
      title={Kimina Lean Server: Technical Report},
      author={Marco Dos Santos and Haiming Wang and Hugues de Saxcé and Ran Wang and Mantas Baksys and Mert Unsal and Junqi Liu and Zhengying Liu and Jia Li},
      year={2025},
      eprint={2504.21230},
      archivePrefix={arXiv},
      primaryClass={cs.LO},
      url={https://arxiv.org/abs/2504.21230},
}
```
