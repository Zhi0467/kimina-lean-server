from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from server.pantograph_normalize import (
    exception_to_messages,
    goal_state_to_goal_texts,
    payload_to_messages,
)


class FakeMode(Enum):
    TACTIC = 1


@dataclass(frozen=True)
class FakeVariable:
    text: str

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True)
class FakeGoal:
    target: str
    variables: list[FakeVariable]
    name: str | None = None
    mode: FakeMode = FakeMode.TACTIC


@dataclass(frozen=True)
class FakeGoalState:
    goals: list[FakeGoal]


@dataclass(frozen=True)
class BadStringMessage:
    data: str

    def __str__(self) -> str:
        raise RuntimeError("broken")


def test_goal_state_to_goal_texts_formats_promptable_goals() -> None:
    goal_state = FakeGoalState(
        goals=[
            FakeGoal(
                target="n + 0 = n",
                variables=[FakeVariable("n : Nat")],
                name="zero",
            )
        ]
    )

    assert goal_state_to_goal_texts(goal_state) == ["case zero\nn : Nat\n⊢ n + 0 = n"]


def test_payload_to_messages_extracts_nested_pantograph_payloads() -> None:
    assert payload_to_messages(
        {
            "error": "parse",
            "parseError": {"data": "unknown tactic"},
            "messages": [BadStringMessage("fallback message")],
        }
    ) == ["parse", "unknown tactic", "fallback message"]


def test_exception_to_messages_handles_empty_exceptions() -> None:
    assert exception_to_messages(RuntimeError()) == ["RuntimeError"]
