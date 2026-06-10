from __future__ import annotations

from typing import Any

from lean_client import (
    AsyncKiminaClient,
    AsyncLeanExecBackend,
    AsyncLeanExecBatcher,
    AsyncLeanExecEnv,
    ExecCleanupResponse,
    ExecCreateStatesResponse,
)


class _RecordingClient(AsyncKiminaClient):
    """Client stub that records calls and replays canned responses (no HTTP)."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.api_url = "http://lean.example"
        self.api_key = None
        self.headers = {}
        self.http_timeout = 60
        self.n_retries = 0
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any] | None, str]] = []

    async def _query(
        self,
        url: str,
        payload: dict[str, Any] | None = None,
        method: str = "POST",
    ) -> Any:
        self.calls.append((url, payload, method))
        return self.responses.pop(0)

    async def aclose(self) -> None:  # no session to close in the stub
        return None


async def test_backend_facade_delegates_across_layers() -> None:
    # Responses are consumed in call order: create -> step -> verify -> cleanup.
    client = _RecordingClient(
        [
            {
                "items": [
                    {
                        "item_id": "thm:a0",
                        "status": "open",
                        "states": [
                            {
                                "state_token": "st_root",
                                "goals": [{"target": "True", "pretty": "⊢ True"}],
                            }
                        ],
                        "messages": [],
                    }
                ]
            },
            {
                "items": [
                    {
                        "node_id": "thm:a0:n0",
                        "results": [{"tactic": "trivial", "status": "complete"}],
                    }
                ]
            },
            {
                "items": [
                    {
                        "item_id": "thm:a0",
                        "status": "accepted",
                        "theorem_name": "t",
                        "axioms": [],
                        "messages": [],
                    }
                ]
            },
            {
                "deleted_items": [
                    {
                        "item_id": "thm:a0",
                        "status": "deleted",
                        "deleted_states": 1,
                        "deleted_bytes": 0,
                    }
                ]
            },
        ]
    )
    env = AsyncLeanExecEnv(client, env_profile="lean_init")
    # Build the batcher directly (avoids a network /exec/limits round-trip);
    # `connect()` does the same composition plus server-limit sizing.
    batcher = AsyncLeanExecBatcher(env, max_items=8, max_wait_ms=0)
    backend = AsyncLeanExecBackend(client=client, env=env, batcher=batcher)

    try:
        create = await backend.create_states(
            "thm:a0", "import Mathlib\n\ntheorem t : True := by\n  sorry"
        )
        assert isinstance(create, ExecCreateStatesResponse)
        assert create.items[0].states[0].state_token == "st_root"

        step = await backend.step("thm:a0:n0", "st_root", ["trivial"])
        assert step.results[0].status == "complete"

        verdict = await backend.verify_one(
            "thm:a0", "import Mathlib\n\ntheorem t : True := trivial", "t"
        )
        assert verdict.status == "accepted"
        assert verdict.axioms == []

        cleanup = await backend.cleanup(["thm:a0"])
        assert isinstance(cleanup, ExecCleanupResponse)
        assert cleanup.deleted_items[0].deleted_states == 1
    finally:
        await backend.aclose()

    # One façade, but each call routed to the right endpoint/layer.
    assert [url for url, _payload, _method in client.calls] == [
        "http://lean.example/exec/create_states",
        "http://lean.example/exec/step_batch",
        "http://lean.example/exec/verify",
        "http://lean.example/exec/cleanup",
    ]
