import asyncio

from kimina_client import AsyncKiminaClient, AsyncLeanExecEnv


async def main() -> None:
    async with AsyncKiminaClient() as client:
        env = AsyncLeanExecEnv(client, env_profile="lean_init", timeout_ms=30_000)
        item_id = "smoke:Nat_add_zero:attempt_1"

        created = await env.create_states(
            item_id,
            "theorem t (n : Nat) : n + 0 = n := by\n  sorry",
        )
        state = created.items[0].states[0]

        stepped = await env.step_node(
            f"{item_id}:n0",
            state.state_token,
            ["simp", "rw [Nat.add_comm]", "bad_tactic"],
        )
        print(stepped.model_dump(exclude_none=True))

        await env.cleanup([item_id])


if __name__ == "__main__":
    asyncio.run(main())
