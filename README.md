<h1 align="center">Kimina Lean Server</h1>

<p align="center">
<b>Check Lean 4 code at scale ⚡️</b>

</p>

<p align="center">
    <a href="https://projectnumina.ai/"><img alt="Project Numina" src="images/logo_projectNumina_light.png" style="height:20px; width:auto; vertical-align:middle; border-radius:4px;"></a>
    <a href="https://github.com/project-numina/kimina-lean-server/actions/workflows/ci.yaml" rel="nofollow"><img alt="CI" src="https://github.com/project-numina/kimina-lean-server/actions/workflows/ci.yaml/badge.svg" style="max-width:100%;"></a>
</p>

This local development checkout serves the
[Lean REPL](https://github.com/leanprover-community/repl) using FastAPI.
It supports parallelization to check Lean 4 proofs at scale.

Both the server code and local Python client live in this repository and are
run directly from source with `uv`. The server is not installed as a packaged
Python distribution in this development workflow.

Read the [Technical Report](./Technical_Report.pdf) for more details.

## Table of Contents

- [Server](#server)
- [Client](#client)
- [Contributing](#contributing)
- [License](#license)
- [Citation](#citation)

This repository contains the source code for:
- the Kimina server
- the Kimina client to interact with it

## Server

From a source checkout, use `uv` for the Python environment and run the
server directly from the repository root:
```sh
cp .env.template .env
uv sync --dev
uv run prisma generate
bash setup.sh # Installs Lean and mathlib4 for the /exec Pantograph path
uv run python -m server
```

> [!NOTE]
> In this development checkout, the server is not treated as an installed
> Python package. It is run from source with `uv run python -m server`.
> Make sure `mathlib4` exists in the workspace directory before launching the
> server. The legacy `/api/check` path also needs `repl`; build it with
> `SETUP_REPL=1 bash setup.sh` if you need that path.

The `/exec` backend starts with finite safety caps by default:
`max_in_flight_exec_requests=8`, `max_queued_exec_requests=32`, and
`max_state_store_bytes=16 * 2**30`. Setting any of those caps to `-1` requires
`allow_unbounded_exec=True`; otherwise the app refuses to boot. The server also
defaults to `single_process=True` and takes a lock inside `state_store_dir`, so
do not run multiple uvicorn workers or multiple server processes against the
same state store.

`python -m server` accepts explicit app-launch flags for downstream job
launchers:

```sh
uv run python -m server \
  --host 127.0.0.1 \
  --port 8000 \
  --workers 8 \
  --state-store-dir /tmp/leanfoundry-state
```


Or with `docker compose up`.
Equivalent run command is:
```sh
docker run -d \
  --name server \
  --restart unless-stopped \
  --env-file .env \
  -p 80:${LEAN_SERVER_PORT} \
  projectnumina/kimina-lean-server:2.0.0
```

To shut down the container / view logs:

```sh
docker compose down
docker compose logs -f
```

Build your own image with specific Lean version with:
```sh
docker build --build-arg=LEAN_SERVER_LEAN_VERSION=v4.21.0 .
```

Test it works with a request:

```sh
curl --request POST \
  --url http://localhost:8000/verify \
  --header 'Content-Type: application/json' \
  --data '{
    "codes": [
      {
        "custom_id": "1234",
        "proof": "#check Nat"
      }
    ],
    "infotree_type": "original"
}' | jq
```

Or use the client below.

## Client

The client lives in its own versioned package, `packages/lean-client/`
(distribution name `lean-client`), wired in as a uv workspace member. From
this repo it is importable as `lean_client` via `uv run`; a downstream repo
depends on it directly, e.g.

```toml
[project]
dependencies = ["lean-client"]

[tool.uv.sources]
lean-client = { git = "https://github.com/project-numina/kimina-lean-server", subdirectory = "packages/lean-client", rev = "<pinned-sha-or-tag>" }
```

For whole-code checking:

```python
from lean_client import KiminaClient
client = KiminaClient() # Defaults to "http://localhost:8000", no API key
client.check("#check Nat")
```

For LeanFoundry-style proof-state search, the downstream repo should import the
client package only. It does not import server internals or know how workers are
implemented:

```python
from lean_client import AsyncKiminaClient, AsyncLeanExecBatcher, AsyncLeanExecEnv

async with AsyncKiminaClient(api_url="http://localhost:8000") as client:
    env = AsyncLeanExecEnv(client, env_profile="lean_init_test")
    batcher = await AsyncLeanExecBatcher.from_server_limits(env)
    created = await env.create_states("attempt-1", "theorem t : True := by\n  sorry")
    state_token = created.items[0].states[0].state_token
    result = await batcher.submit_step("attempt-1:n0", state_token, ["trivial"])
```

If a downstream job needs to start the app process itself, use the client-side
launcher mirror. The launcher uses only stdlib `subprocess`; `server_python`
points at the server checkout's virtualenv.

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

See [README-client.md](./README-client.md) for the full client boundary and
`/exec` examples.

## ⚙️ Environment Variables

| Variable                              | Default       | Description                                            |
| ------------------------------------- | ------------- | ------------------------------------------------------ |
| `LEAN_SERVER_HOST`                    | `0.0.0.0`     | Host address to bind the server                        |
| `LEAN_SERVER_PORT`                    | `8000`        | Port number for the server                             |
| `LEAN_SERVER_LOG_LEVEL`               | `INFO`        | Logging level (`DEBUG`, `INFO`, `ERROR`, etc.)         |
| `LEAN_SERVER_ENVIRONMENT`             | `dev`         | Environment `dev` or `prod`                            |
| `LEAN_SERVER_LEAN_VERSION`            | `v4.29.1`     | Lean version                                           |
| `LEAN_SERVER_MAX_REPLS`               | CPU count - 1 | Maximum number of REPLs                                |
| `LEAN_SERVER_MAX_REPL_USES`           | `-1`          | Maximum number of uses per REPL (-1 is no limit)       |
| `LEAN_SERVER_MAX_REPL_MEM`            | `8G`          | Maximum memory limit for each REPL (Linux-only)        |
| `LEAN_SERVER_MAX_WAIT`                | `60`          | Maximum wait time to wait for a REPL (in seconds)      |
| `LEAN_SERVER_INIT_REPLS`              | `{}`          | Map of header to REPL count to initialize with         |
| `LEAN_SERVER_API_KEY`                 | `None`        | Optional API key for authentication                    |
| `LEAN_SERVER_REPL_PATH`               | `repl/.lake/build/bin/repl` | Path to REPL directory, relative to workspace    |
| `LEAN_SERVER_PROJECT_DIR`             | `mathlib4`    | Path to Lean 4 project directory, relative to workspace        |
| `LEAN_SERVER_DATABASE_URL`            |               | URL for the database (if using one)                   |
| `LEAN_SERVER_MAX_PANTOGRAPH_WORKERS`  | CPU count - 1 | Total `/exec` Pantograph Lean worker process count     |
| `LEAN_SERVER_MAX_LEAN_PROCESSES_PER_ENV_PROFILE` | same as `MAX_PANTOGRAPH_WORKERS` | Per-env-profile worker cap and per-request lane cap |
| `LEAN_SERVER_MAX_IN_FLIGHT_EXEC_REQUESTS` | `8`       | Global concurrent admitted `/exec` HTTP requests       |
| `LEAN_SERVER_MAX_QUEUED_EXEC_REQUESTS` | `32`        | Global queued `/exec` requests before 503 backpressure |
| `LEAN_SERVER_MAX_STATE_STORE_BYTES`   | `17179869184` | State-store disk budget before GC backstop             |
| `LEAN_SERVER_ALLOW_UNBOUNDED_EXEC`    | `false`       | Must be true to permit any `-1` safety cap             |
| `LEAN_SERVER_RECOMMENDED_ITEMS_PER_STEP_BATCH` | same as `MAX_PANTOGRAPH_WORKERS` | `/exec/limits` request-width recommendation |
| `LEAN_SERVER_RECOMMENDED_IN_FLIGHT_STEP_BATCHES` | `8` | `/exec/limits` per-client in-flight recommendation     |
| `LEAN_SERVER_SINGLE_PROCESS`          | `true`        | Lock `state_store_dir` and fail fast on a second server |

`LEAN_SERVER_MAX_REPL_MEM` can help avoid certain OOM issues (see Issue #25)
The server also runs all commands with `"gc": true` to automatically discard environments which helps limit memory usage.



## 🚀 Performance Benchmarks

You can run benchmarks with the Kimina client on any HuggingFace dataset: the benchmark run expects `id` and `code` columns in
the dataset, but you can select your own column names.

Example with [Goedel-LM/Lean-workbook-proofs](https://huggingface.co/datasets/Goedel-LM/Lean-workbook-proofs):
```python
from lean_client import KiminaClient

client = KiminaClient()
client.run_benchmark(dataset_name="Goedel-LM/Lean-workbook-proofs", 
                     n=1000,
                     batch_size=8,
                     max_workers=10)
```

If running benchmarks using the synchronous client (`KiminaClient` instead of `AsyncKiminaClient`) from an end-user computer, you may face the following error:

> tenacity.before_sleep:log_it:65 - Retrying **main**.KiminaClient.\_query.<locals>.query_with_retries in 10.0 seconds as it raised ClientConnectorError: Cannot connect to host 127.0.0.1:80 ssl:default [Too many open files].

This happens when you set a number of `max_workers` greater than the allowed number of TCP connections on your machine. 
The synchronous client could not reliably make use of the same connection across threads, so each worker has its session. 

You can check the maximum number of open files on your machine with `ulimit -n` (256 on a MacBook Pro). It may be smaller than what's needed to run the benchmark: increase it with `ulimit -n 4096`.

Alternatively, you can use the asynchronous client `AsyncKiminaClient` which uses a single session and can handle more workers without running into this issue.

### Benchmark reports

Without REPL reuse:
![Benchmark Results without REPL reuse](images/benchmark_results_reuse_false.png)

With REPL reuse:
![Benchmark Results with REPL reuse](images/benchmark_results_reuse_true.png)

**Note**:

The benchmarks were run on a machine with **10 CPUs** (MacBook Pro M2) with the above command and default parameters.
The dataset is available at [`Goedel-LM/Lean-workbook-proofs`](https://huggingface.co/datasets/Goedel-LM/Lean-workbook-proofs). 

To reproduce:
- Server command: `uv run python -m server` (no `.env` file)
- Client (from ipython / Jupyter notebook or `python -m asyncio`):
```python
from lean_client import AsyncKiminaClient
client = AsyncKiminaClient() # defaults to "http://localhost:8000", no API key

# Add `reuse=False` to prevent REPL reuse across requests
await client.run_benchmark(dataset_name="Goedel-LM/Lean-workbook-proofs", n=1000)
```

## Contributing

Contributions are welcome 🤗, just open an issue or submit a pull request.

To contribute, ensure you have Astral's [uv](https://docs.astral.sh/uv/) installed and:

```sh
uv run pre-commit install
```

On commit, the hooks:
- run `ruff`, `pyright` and `mypy`
- enforce [conventional commits](https://www.conventionalcommits.org/en/v1.0.0/). 

`mypy` was slow against the `client` directory, so I excluded it in the pre-commit config, therefore also on the CI. 
You can still run `mypy` manually to check. 

An additional hook runs basic tests on push.

> [!TIP]
> Use `--no-verify` to skip hooks on commit / push (but the CI runs them).


Install [Lean 4](https://github.com/leanprover/lean4) and build [mathlib4](https://github.com/leanprover-community/mathlib4) for the `/exec` Pantograph path:
```sh
bash setup.sh
```

The legacy `/api/check` path uses the Lean REPL binary. Build it only when
needed with `SETUP_REPL=1 bash setup.sh`.

Run tests with (reads your `LEAN_SERVER_API_KEY` so make sure that line is commented):
```sh
uv run pytest

# Performance tests on first rows of Goedel (ensures less than 10s average check time per proof)
uv run pytest -m perfs

# Tests on 100 first Goedel rows to validate API backward-compatibility
uv run pytest -m match # Use -n auto to use all cores.
```

## License

This project is licensed under the MIT License.
You are free to use, modify, and distribute this software with proper attribution. See the [LICENSE](./LICENSE) file for full details.

## Citation
```
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
