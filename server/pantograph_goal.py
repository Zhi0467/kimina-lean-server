"""Worker-side structured goal data.

These plain dataclasses are the backend-internal representation of a Lean proof
goal as produced by a Pantograph worker. They are deliberately free of any
pydantic / API concerns: the API layer (``schemas_exec.GoalInfo``) is built from
these at the worker -> response boundary (see ``exec_backends``).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PantographHypothesis:
    """A single local-context entry (one Pantograph ``Variable``).

    ``value`` is populated only for ``let``-bound hypotheses; ``name`` is the
    user-facing binder name (``None`` for inaccessible / anonymous binders).
    """

    type: str
    name: str | None = None
    value: str | None = None


@dataclass(frozen=True)
class PantographGoal:
    """One in-scope goal of a proof state.

    - ``target`` / ``hypotheses`` carry the structured content the search engine
      keys node identity on.
    - ``pretty`` is the flattened human/model-facing rendering (byte-identical to
      the legacy ``goals: list[str]`` entry).
    - ``name`` is the case tag (e.g. ``inl``), ``None`` when unnamed.
    - ``sibling_dep`` lists the indices of sibling goals this goal shares a
      metavariable with (the split-rule signal); empty when independent.
    """

    target: str
    pretty: str
    hypotheses: list[PantographHypothesis] = field(
        default_factory=list[PantographHypothesis]
    )
    name: str | None = None
    sibling_dep: list[int] = field(default_factory=list[int])
