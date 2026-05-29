from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, cast


def _as_dict(value: Any) -> dict[Any, Any]:
    return cast(dict[Any, Any], value)


def _as_iterable(value: Any) -> Iterable[Any]:
    return cast(Iterable[Any], value)


def _as_mapping(value: Any) -> Mapping[Any, Any]:
    return cast(Mapping[Any, Any], value)


def goal_state_to_goal_texts(goal_state: Any) -> list[str]:
    return [_goal_to_text(goal) for goal in goal_state.goals]


def goal_payloads_to_goal_texts(goals: Iterable[Any]) -> list[str]:
    return [_goal_to_text(goal) for goal in goals]


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
        return _dict_payload_to_messages(_as_dict(payload))
    if isinstance(payload, Iterable) and not isinstance(payload, (bytes, bytearray)):
        texts: list[str] = []
        for item in _as_iterable(payload):
            texts.extend(payload_to_messages(item))
        return texts or [str(cast(object, payload))]
    if hasattr(payload, "data"):
        return [_message_to_text(payload)]
    return [str(payload)]


def _goal_to_text(goal: Any) -> str:
    if isinstance(goal, Mapping):
        return _goal_payload_to_text(_as_mapping(goal))

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


def _goal_payload_to_text(goal: Mapping[Any, Any]) -> str:
    lines: list[str] = []
    name = goal.get("userName")
    if name:
        lines.append(f"case {name}")

    for variable in _as_iterable(goal.get("vars", [])):
        if isinstance(variable, Mapping):
            variable_payload = _as_mapping(variable)
            var_name = variable_payload.get("userName") or variable_payload.get("name")
            var_type = _expr_payload_to_text(variable_payload.get("type"))
            if var_name and var_type:
                lines.append(f"{var_name} : {var_type}")
            elif var_name:
                lines.append(str(var_name))
            else:
                lines.append(str(cast(object, variable)))
        else:
            lines.append(str(cast(object, variable)))

    fragment = str(goal.get("fragment", "tactic")).lower()
    front = "|" if fragment == "conv" else "⊢"
    lines.append(f"{front} {_expr_payload_to_text(goal.get('target'))}")
    return "\n".join(lines)


def _expr_payload_to_text(expr: Any) -> str:
    if isinstance(expr, Mapping):
        expr_payload = _as_mapping(expr)
        for key in ("pp", "sexp"):
            value = expr_payload.get(key)
            if value is not None:
                return str(value)
    return str(cast(object, expr))


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
