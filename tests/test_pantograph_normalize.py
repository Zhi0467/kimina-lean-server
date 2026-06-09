from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from server.pantograph_goal import PantographHypothesis
from server.pantograph_normalize import (
    exception_to_messages,
    goal_state_to_goal_texts,
    goal_state_to_goals,
    payload_to_exec_messages,
    payload_to_messages,
)


class FakeMode(Enum):
    TACTIC = 1


@dataclass(frozen=True)
class FakeVariable:
    """Mirrors pantograph ``Variable`` (``t`` type, ``v`` let-value, ``name``)."""

    t: str
    name: str | None = None
    v: str | None = None

    def __str__(self) -> str:
        head = self.name if self.name else "_"
        result = f"{head} : {self.t}"
        if self.v is not None:
            result += f" := {self.v}"
        return result


@dataclass(frozen=True)
class FakeGoal:
    target: str
    variables: list[FakeVariable]
    name: str | None = None
    sibling_dep: set[int] | None = None
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
                variables=[FakeVariable(t="Nat", name="n")],
                name="zero",
            )
        ]
    )

    assert goal_state_to_goal_texts(goal_state) == ["case zero\nn : Nat\n⊢ n + 0 = n"]


def test_goal_state_to_goals_builds_structured_goals() -> None:
    goal_state = FakeGoalState(
        goals=[
            FakeGoal(
                target="n + 0 = n",
                variables=[
                    FakeVariable(t="Nat", name="n"),
                    FakeVariable(t="Nat", name="m", v="0"),
                ],
                name="zero",
                sibling_dep={2, 1},
            )
        ]
    )

    goals = goal_state_to_goals(goal_state)

    assert len(goals) == 1
    goal = goals[0]
    assert goal.target == "n + 0 = n"
    assert goal.name == "zero"
    assert goal.pretty == "case zero\nn : Nat\nm : Nat := 0\n⊢ n + 0 = n"
    assert goal.sibling_dep == [1, 2]  # sorted, deduplicated
    assert goal.hypotheses == [
        PantographHypothesis(type="Nat", name="n", value=None),
        PantographHypothesis(type="Nat", name="m", value="0"),
    ]


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


def test_server_error_payload_omits_category_tag() -> None:
    # ``error`` here is a category ("io"), not display text: the single message
    # comes from ``desc`` (with its parsed position); "io" must not leak as its
    # own message.
    messages = payload_to_exec_messages(
        {
            "desc": "<anonymous>:2:7: error: unexpected end of input\n",
            "error": "io",
        },
        default_severity="error",
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.severity == "error"
    assert message.data == "unexpected end of input"
    assert message.pos is not None
    assert (message.pos.line, message.pos.col) == (2, 7)
