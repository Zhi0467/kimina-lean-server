from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
from typing import Any, cast

from .pantograph_goal import PantographGoal, PantographHypothesis
from .schemas_exec import ExecMessage, ExecMessageSeverity, ExecPos


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


def messages_to_exec_messages(messages: Iterable[Any]) -> list[ExecMessage]:
    exec_messages: list[ExecMessage] = []
    for message in messages:
        exec_messages.extend(payload_to_exec_messages(message))
    return exec_messages


def exception_to_messages(exc: BaseException) -> list[str]:
    if not exc.args:
        return [exc.__class__.__name__]
    if len(exc.args) == 1:
        return payload_to_messages(exc.args[0])
    return payload_to_messages(exc.args)


def exception_to_exec_messages(exc: BaseException) -> list[ExecMessage]:
    if not exc.args:
        return [_text_message(exc.__class__.__name__, severity="error")]
    if len(exc.args) == 1:
        return payload_to_exec_messages(exc.args[0], default_severity="error")
    return payload_to_exec_messages(exc.args, default_severity="error")


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


def payload_to_exec_messages(
    payload: object,
    *,
    default_severity: str = "info",
) -> list[ExecMessage]:
    if payload is None:
        return []
    if isinstance(payload, str):
        return [_text_message(payload, severity=default_severity)]
    if isinstance(payload, Mapping):
        return _dict_payload_to_exec_messages(
            cast(Mapping[object, object], payload),
            default_severity=default_severity,
        )
    if isinstance(payload, Iterable) and not isinstance(payload, (bytes, bytearray)):
        payload_text = str(cast(object, payload))
        messages: list[ExecMessage] = []
        for item in cast(Iterable[object], payload):
            messages.extend(
                payload_to_exec_messages(
                    item,
                    default_severity=default_severity,
                )
            )
        return messages or [_text_message(payload_text, severity=default_severity)]
    if hasattr(payload, "data"):
        return [_pantograph_message_to_exec_message(payload)]
    return [_text_message(str(payload), severity=default_severity)]


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


def _dict_payload_to_exec_messages(
    payload: Mapping[object, object],
    *,
    default_severity: str,
) -> list[ExecMessage]:
    severity = _severity_from_payload(payload, default=default_severity)
    texts: list[str] = []
    for key in ("desc", "parseError", "message", "data"):
        if key in payload:
            texts.extend(payload_to_messages(payload[key]))
    # ``error`` is frequently a category tag (e.g. ``"io"``) rather than display
    # text; surface it only when no descriptive field carried content.
    if not texts and "error" in payload:
        texts.extend(payload_to_messages(payload["error"]))
    if "messages" in payload:
        nested = payload_to_exec_messages(
            payload["messages"],
            default_severity=severity,
        )
        if nested:
            return nested
    return [
        _text_message(text, severity=severity)
        for text in texts
    ] or [_text_message(str(payload), severity=severity)]


def _message_to_text(message: Any) -> str:
    try:
        return str(message)
    except Exception:
        data = getattr(message, "data", None)
        return str(data) if data is not None else repr(message)


def _pantograph_message_to_exec_message(message: Any) -> ExecMessage:
    return ExecMessage(
        severity=_normalize_severity(getattr(message, "severity", None), default="info"),
        data=str(getattr(message, "data", _message_to_text(message))),
        pos=_position_to_exec_pos(getattr(message, "pos", None)),
        end_pos=_position_to_exec_pos(
            getattr(message, "pos_end", getattr(message, "endPos", None))
        ),
    )


def _text_message(
    text: str,
    *,
    severity: str,
) -> ExecMessage:
    parsed = _positioned_text(text)
    if parsed is None:
        return ExecMessage(
            severity=_normalize_severity(severity, default="info"),
            data=text,
        )
    data, pos, parsed_severity = parsed
    return ExecMessage(
        severity=_normalize_severity(parsed_severity or severity, default="info"),
        data=data,
        pos=pos,
    )


def _positioned_text(text: str) -> tuple[str, ExecPos, str | None] | None:
    match = re.match(
        r"^(?:<[^>]+>|[^:\n]+):(\d+):(\d+):\s*(?:(error|warning|info|information|trace):\s*)?(.*)$",
        text,
        flags=re.DOTALL,
    )
    if match is None:
        return None
    line_text, col_text, severity_text, data = match.groups()
    return data.rstrip(), ExecPos(line=int(line_text), col=int(col_text)), severity_text


def _position_to_exec_pos(position: Any) -> ExecPos | None:
    if position is None:
        return None
    line = getattr(position, "line", None)
    col = getattr(position, "column", None)
    if line is None or col is None:
        return None
    # PyPantograph tactic messages can use line 0 for a single tactic string;
    # the API reports source positions as 1-based lines.
    return ExecPos(line=max(int(line), 1), col=max(int(col), 0))


def _severity_from_payload(
    payload: Mapping[object, object],
    *,
    default: str,
) -> str:
    if "severity" in payload:
        return str(payload["severity"])
    if "parseError" in payload or "error" in payload:
        return "error"
    return default


def _normalize_severity(value: Any, *, default: str) -> ExecMessageSeverity:
    text = str(value if value is not None else default).lower()
    if text in {"information", "info", "severity.information"}:
        return "info"
    if text in {"warning", "severity.warning"}:
        return "warning"
    if text in {"error", "severity.error"}:
        return "error"
    if text in {"trace", "severity.trace"}:
        return "trace"
    return "info"
