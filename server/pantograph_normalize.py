from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def goal_state_to_goal_texts(goal_state: Any) -> list[str]:
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


def payload_to_messages(payload: Any) -> list[str]:
    if payload is None:
        return []
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, dict):
        return _dict_payload_to_messages(payload)
    if isinstance(payload, Iterable) and not isinstance(payload, (bytes, bytearray)):
        texts: list[str] = []
        for item in payload:
            texts.extend(payload_to_messages(item))
        return texts or [str(payload)]
    if hasattr(payload, "data"):
        return [_message_to_text(payload)]
    return [str(payload)]


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


def _dict_payload_to_messages(payload: dict[Any, Any]) -> list[str]:
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
