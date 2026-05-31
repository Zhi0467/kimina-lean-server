# Kimina client

Client SDK to interact with Kimina Lean server. 

Example use:
```python
from kimina_client import KiminaClient

# Specify LEAN_SERVER_API_KEY in your .env or pass `api_key`.
# Default `api_url` is https://projectnumina.ai
client = KiminaClient()

# If running locally use:
# client = KiminaClient(api_url="http://localhost:80")

client.check("#check Nat")
```

## Exec environment client

LeanFoundry/SearchEngine code should call the search-facing exec wrapper instead
of hand-writing `/exec` JSON:

```python
from kimina_client import AsyncKiminaClient, AsyncLeanExecBatcher, AsyncLeanExecEnv

async with AsyncKiminaClient(api_url="http://localhost:8000") as client:
    env = AsyncLeanExecEnv(client, env_profile="lean_init_test")
    limits = await env.limits()
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

Use one unique `item_id` per search attempt. The batcher derives request size and
in-flight limits from `/exec/limits`, serializes live requests for the same
`item_id`, retries observed `overloaded` results, and treats unknown transport
outcomes as uncertain instead of replaying them under the same attempt.

## Backward client

```python
from kimina_client import Lean4Client

client = Lean4Client()

client.verify("#check Nat")
```
