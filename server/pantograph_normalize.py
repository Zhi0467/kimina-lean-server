from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, cast

from .pantograph_goal import PantographGoal, PantographHypothesis


def goal_state_to_goals(goal_state: Any) -> list[PantographGoal]:
    """Structured goals for a proof state (target, hypotheses, sibling_dep)."""
    return [_goal_to_goal(goal) for goal in goal_state.goals]


def goal_state_to_goal_texts(goal_state: Any) -> list[str]:
    """Flattened per-goal renderings (legacy string form / ``pretty`` source)."""
    return [_goal_to_text(goal) for goal in goal_state.goals]


def messages_to_texts(messages: Iterable[Any]) -> list[str]:
    texts: list[str] = []
    for message in messages:
        texts.extend(payload_to_messages(message))
    return texts


def exception_to_messages(exc: BaseException) -> list[str]:
    if not exc.args:
        return [exc.__class__.__name__]
    if len(exc.args) == 1:
        return payload_to_messages(exc.args[0])
    return payload_to_messages(exc.args)


def payload_to_messages(payload: object) -> list[str]:
    if payload is None:
        return []
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, Mapping):
        return _dict_payload_to_messages(cast(Mapping[object, object], payload))
    if isinstance(payload, Iterable) and not isinstance(payload, (bytes, bytearray)):
        payload_text = str(cast(object, payload))
        texts: list[str] = []
        for item in cast(Iterable[object], payload):
            texts.extend(payload_to_messages(item))
        return texts or [payload_text]
    if hasattr(payload, "data"):
        return [_message_to_text(payload)]
    return [str(payload)]


def _goal_to_goal(goal: Any) -> PantographGoal:
    hypotheses = [
        _variable_to_hypothesis(variable)
        for variable in getattr(goal, "variables", [])
    ]
    sibling_dep = getattr(goal, "sibling_dep", None)
    return PantographGoal(
        target=str(goal.target),
        pretty=_goal_to_text(goal),
        hypotheses=hypotheses,
        name=getattr(goal, "name", None) or None,
        sibling_dep=sorted(sibling_dep) if sibling_dep else [],
    )


def _variable_to_hypothesis(variable: Any) -> PantographHypothesis:
    value = getattr(variable, "v", None)
    return PantographHypothesis(
        type=str(getattr(variable, "t", "")),
        name=getattr(variable, "name", None) or None,
        value=str(value) if value is not None else None,
    )


def _goal_to_text(goal: Any) -> str:
    lines: list[str] = []
    name = getattr(goal, "name", None)
    if name:
        lines.append(f"case {name}")

    lines.extend(str(variable) for variable in getattr(goal, "variables", []))

    mode = getattr(goal, "mode", None)
    mode_name = getattr(mode, "name", "")
    front = "|" if mode_name == "CONV" else "⊢"
    lines.append(f"{front} {goal.target}")
    return "\n".join(lines)


def _dict_payload_to_messages(payload: Mapping[object, object]) -> list[str]:
    texts: list[str] = []
    for key in ("desc", "error", "parseError", "message", "data"):
        if key in payload:
            texts.extend(payload_to_messages(payload[key]))
    if "messages" in payload:
        texts.extend(payload_to_messages(payload["messages"]))
    return texts or [str(payload)]


def _message_to_text(message: Any) -> str:
    try:
        return str(message)
    except Exception:
        data = getattr(message, "data", None)
        return str(data) if data is not None else repr(message)
