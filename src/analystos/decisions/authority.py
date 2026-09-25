"""Authority classes, enforced in code (ADR-0015 §2). Whatever a backend answers, these functions
decide what it is allowed to change:

rank                 reorder valid options; an option a backend drops keeps its rule score, an
                     unknown option is ignored
choose_presentation  an answer outside the rule-valid presentations is refused
route                an answer outside the valid processing paths is refused
escalate_only        final level = max(baseline, proposal): nothing can lower a risk or severity,
                     skip a gate or suppress an alert
bounded_stop         a stop before the minimum rounds / supported findings is refused

A refused proposal raises `Refused`; the service records it and moves to the next backend.
"""
from __future__ import annotations

import math
from typing import Any

from analystos.decisions.config import PurposeSpec
from analystos.decisions.types import Proposal, Question


class Refused(Exception):
    """The proposal is outside what the purpose's authority class allows."""


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def rank(question: Question, proposal: Proposal, rule_scores: dict[str, float]) -> tuple[dict[str, float], list[str]]:
    """Scores for EVERY option: backend scores where valid, the rule score elsewhere."""
    if not isinstance(proposal.value, dict):
        raise Refused("rank: answer is not a score per option")
    top = float(max(len(question.criteria) - 1, 1))
    valid = {k: min(max(n, 0.0), top) for k, v in proposal.value.items() if k in question.options and (n := _number(v)) is not None}
    notes = []
    ignored = sorted(set(map(str, proposal.value)) - set(question.options))
    if ignored:
        notes.append(f"ignored unknown options {ignored[:5]}")
    if not valid:
        raise Refused("rank: no score for any valid option")
    missing = [k for k in question.options if k not in valid]
    if missing:
        notes.append(f"kept rule scores for {len(missing)} dropped option(s)")
    return {k: valid.get(k, rule_scores.get(k, top / 2)) for k in question.options}, notes


def choose(question: Question, proposal: Proposal, what: str) -> str:
    if not isinstance(proposal.value, str) or proposal.value not in question.options:
        raise Refused(f"{what}: {str(proposal.value)[:60]!r} is not one of the valid options")
    return proposal.value


def level_index(question: Question, level: str | None) -> int:
    return question.levels.index(level) if level in question.levels else 0


def escalate(question: Question, baseline: str, p: float | None) -> tuple[str, list[str]]:
    """max(baseline, what the probability proposes). A low probability never lowers the baseline."""
    if p is None or question.escalate_to not in question.levels:
        return baseline, []
    proposed = question.escalate_to if (p >= question.escalate_at if question.escalate_when == "high"
                                        else p < question.escalate_at) else baseline
    if level_index(question, proposed) > level_index(question, baseline):
        return proposed, []
    notes = []
    if level_index(question, proposed) < level_index(question, baseline):
        notes.append(f"refused to lower {baseline} to {proposed}")
    return baseline, notes


def bounded_stop(spec: PurposeSpec, state: dict[str, Any], stop: bool, *, by_model: bool) -> tuple[bool, list[str]]:
    """Nobody stops before `min_rounds`; a model (a tie-break, not a rule) also needs `min_supported` findings."""
    if not stop:
        return False, []
    round_no, supported = int(state.get("round") or 0), int(state.get("supported") or 0)
    if round_no < spec.min_rounds:
        return False, [f"refused stop before the minimum {spec.min_rounds} round(s)"]
    if by_model and supported < spec.min_supported:
        return False, [f"refused stop with {supported} < {spec.min_supported} supported findings"]
    return True, []
