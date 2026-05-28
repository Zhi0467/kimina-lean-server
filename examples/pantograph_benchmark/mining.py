"""Mine benchmark workloads from a Goedel-style proof dataset, locally.

Each dataset row is a complete Lean proof (`import Mathlib ... theorem t ... := by
<body>`). We turn it into a benchmark item by replacing the proof body with `sorry`
(the root state) and tokenizing the original body into the sequence of gold tactic
units used to replay the proof. No Lean server is involved in mining.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

THEOREM_INTRO = re.compile(r"\b(theorem|lemma|example)\b")
PROOF_BY = re.compile(r":=\s*by\b")
LINE_COMMENT = re.compile(r"--[^\n]*")

# Lines that signal a structured/focused proof we cannot replay as a linear tactic
# chain (focus bullets, case arms, induction arms). Such rows are skipped.
STRUCTURED_PREFIXES = ("·", "·", "case ", "next ", "| ", "{")
MINING_VERSION = 2


@dataclass(frozen=True)
class ProofWorkload:
    problem_id: str
    source_hash: str
    root_code: str
    tactic_units: list[str] = field(default_factory=list)


def strip_comments(src: str) -> str:
    """Remove Lean block comments (which nest) and line comments."""
    out: list[str] = []
    i = 0
    depth = 0
    while i < len(src):
        pair = src[i : i + 2]
        if pair == "/-":
            depth += 1
            i += 2
            continue
        if pair == "-/" and depth > 0:
            depth -= 1
            i += 2
            continue
        if depth == 0:
            out.append(src[i])
        i += 1
    return LINE_COMMENT.sub("", "".join(out))


def split_header_body(full_proof: str) -> tuple[str, str] | None:
    """Split into (root_code, body), or None if the row is not a `:= by` proof.

    ``root_code`` is the original source up to and including the theorem's
    ``:= by``, with the body replaced by ``sorry``. ``body`` is the raw proof text
    after ``:= by``.
    """
    intro = THEOREM_INTRO.search(full_proof)
    if intro is None:
        return None
    by_match = PROOF_BY.search(full_proof, intro.end())
    if by_match is None:
        return None
    signature_end = by_match.end()
    root_code = full_proof[:signature_end] + " sorry"
    body = full_proof[signature_end:]
    return root_code, body


def tokenize_tactics(body: str) -> list[str]:
    """Split a proof body into tactic units.

    A unit is one top-level tactic. Lines are joined into the same unit when they
    are bracket continuations, or are connected by the `<;>` combinator, so that
    `t1 <;> t2` and multi-line `nlinarith [..., ...]` each stay a single tactic.
    """
    lines = [line for line in strip_comments(body).split("\n")]
    non_blank = [line for line in lines if line.strip()]
    if not non_blank:
        return []
    base_indent = _indent_of(non_blank[0])

    units: list[str] = []
    current: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        joined = "\n".join(current)
        if current and _indent_of(line) <= base_indent and _unit_is_complete(joined, line):
            units.append(joined.strip())
            current = [line]
        else:
            current.append(line)
    if current:
        units.append("\n".join(current).strip())
    return [unit for unit in units if unit]


def is_structured_proof(body: str) -> bool:
    """Whether the body uses focusing constructs we cannot replay linearly."""
    for line in strip_comments(body).split("\n"):
        stripped = line.strip()
        if any(stripped.startswith(prefix) for prefix in STRUCTURED_PREFIXES):
            return True
    return False


def mine_row(problem_id: str, full_proof: str) -> ProofWorkload | None:
    """Mine one dataset row into a workload, or None if it is unsuitable."""
    split = split_header_body(full_proof)
    if split is None:
        return None
    root_code, body = split
    if is_structured_proof(body):
        return None
    tactic_units = tokenize_tactics(body)
    if not tactic_units:
        return None
    return ProofWorkload(
        problem_id=problem_id,
        source_hash=hashlib.sha256(full_proof.encode("utf-8")).hexdigest()[:16],
        root_code=root_code,
        tactic_units=tactic_units,
    )


def build_candidate_tactics(
    gold: str,
    distractor_pool: list[str],
    tactics_per_item: int,
    rng: random.Random,
) -> list[str]:
    """Gold tactic plus seeded distractors, deduped and capped, order shuffled.

    The gold tactic is always present so a successful proof can advance; distractors
    simulate the wrong candidates a search policy would also submit.
    """
    candidates = [gold]
    seen = {gold}
    pool = [tactic for tactic in distractor_pool if tactic not in seen]
    rng.shuffle(pool)
    for tactic in pool:
        if len(candidates) >= tactics_per_item:
            break
        if tactic in seen:
            continue
        seen.add(tactic)
        candidates.append(tactic)
    rng.shuffle(candidates)
    return candidates


def build_workload(
    rows: Iterable[tuple[str, str]],
    *,
    dataset_name: str,
    split: str,
    n_proofs: int,
    seed: int,
    max_rows_scanned: int | None,
    cache_path: Path | None = None,
) -> list[ProofWorkload]:
    """Mine up to ``n_proofs`` workloads from ``(problem_id, full_proof)`` rows.

    Results are cached as JSONL at ``cache_path`` (deterministic data only, never
    state tokens) and reused when the cache already holds enough proofs.
    """
    cache_metadata = {
        "dataset_name": dataset_name,
        "split": split,
        "seed": seed,
        "max_rows_scanned": max_rows_scanned,
        "mining_version": MINING_VERSION,
    }

    if cache_path is not None:
        cached = _load_cache(cache_path, cache_metadata)
        if len(cached) >= n_proofs:
            return cached[:n_proofs]

    mined: list[ProofWorkload] = []
    for problem_id, full_proof in rows:
        workload = mine_row(problem_id, full_proof)
        if workload is not None:
            mined.append(workload)
        if len(mined) >= n_proofs:
            break

    random.Random(seed).shuffle(mined)
    mined = mined[:n_proofs]
    if cache_path is not None:
        _write_cache(cache_path, mined, cache_metadata)
    return mined


def distractor_pool(workloads: list[ProofWorkload]) -> list[str]:
    """All tactic units across workloads, deduped, for sampling wrong candidates."""
    pool: list[str] = []
    seen: set[str] = set()
    for workload in workloads:
        for tactic in workload.tactic_units:
            if tactic not in seen:
                seen.add(tactic)
                pool.append(tactic)
    return pool


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _unit_is_complete(joined: str, next_line: str) -> bool:
    return (
        _brackets_balanced(joined)
        and not joined.rstrip().endswith("<;>")
        and not next_line.lstrip().startswith("<;>")
    )


def _brackets_balanced(text: str) -> bool:
    return (
        text.count("[") == text.count("]")
        and text.count("(") == text.count(")")
        and text.count("{") == text.count("}")
    )


def _load_cache(cache_path: Path, expected_metadata: dict[str, object]) -> list[ProofWorkload]:
    if not cache_path.is_file():
        return []
    lines = cache_path.read_text().splitlines()
    if not lines:
        return []
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError:
        return []
    if header.get("cache_metadata") != expected_metadata:
        return []

    workloads: list[ProofWorkload] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        raw = json.loads(line)
        workloads.append(
            ProofWorkload(
                problem_id=raw["problem_id"],
                source_hash=raw["source_hash"],
                root_code=raw["root_code"],
                tactic_units=raw["tactic_units"],
            )
        )
    return workloads


def _write_cache(
    cache_path: Path,
    workloads: list[ProofWorkload],
    cache_metadata: dict[str, object],
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"cache_metadata": cache_metadata})]
    lines.extend(
        json.dumps(
            {
                "problem_id": w.problem_id,
                "source_hash": w.source_hash,
                "root_code": w.root_code,
                "tactic_units": w.tactic_units,
            }
        )
        for w in workloads
    )
    cache_path.write_text("\n".join(lines) + ("\n" if lines else ""))
