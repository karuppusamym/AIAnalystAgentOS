"""The `rules` backend: one deterministic rule per decision purpose.

A rule sees the model-facing `state`, the caller's trusted `facts` (never sent to a model) and the
question. It returns a Proposal, or None to abstain (a genuine tie the next backend may break).
`final=True` ends the chain: nothing a model could say would change the outcome, so no call is made.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from analystos.decisions.types import Proposal, Question

Rule = Callable[[dict[str, Any], dict[str, Any], Question], Proposal | None]
RULES: dict[str, Rule] = {}


def rule(purpose: str) -> Callable[[Rule], Rule]:
    def register(fn: Rule) -> Rule:
        RULES[purpose] = fn
        return fn
    return register


def _yes_no(p: float) -> dict[str, float]:
    return {"yes": round(p, 6), "no": round(1.0 - p, 6)}


# ---------------------------------------------------------------------------------------- rank
@rule("hypothesis_priority")
def hypothesis_priority(state: dict, facts: dict, q: Question) -> Proposal:
    """The proposer's own priority label (high/medium/low -> 2/1/0), given by the caller as `hint`."""
    hint = q.hint or {}
    return Proposal(value={k: float(hint.get(k, 1.0)) for k in q.options}, note="priority labels")


_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD.findall((text or "").lower().replace("_", " ")) if len(t) > 2}


@rule("metric_match")
def metric_match(state: dict, facts: dict, q: Question) -> Proposal:
    """Token overlap between the question and each metric's name/description (Jaccard, scaled to 0..2)."""
    want = _tokens(str(state.get("question") or state.get("query") or ""))
    scores = {}
    for k, desc in q.options.items():
        have = _tokens(f"{k} {desc}")
        scores[k] = round(2.0 * len(want & have) / len(want | have), 4) if want and have else 0.0
    return Proposal(value=scores, note="token overlap")


@rule("join_path_choice")
def join_path_choice(state: dict, facts: dict, q: Question) -> Proposal | None:
    """Fewer hops first, then higher relationship confidence (facts.paths[k] = {hops, confidence}).
    Abstains when the two best paths tie, so a model may break the tie."""
    paths = facts.get("paths") or {}
    ranked = sorted(q.options, key=lambda k: (int((paths.get(k) or {}).get("hops", 99)),
                                              -float((paths.get(k) or {}).get("confidence", 0.0))))
    if not ranked:
        return None
    key = [(int((paths.get(k) or {}).get("hops", 99)), float((paths.get(k) or {}).get("confidence", 0.0))) for k in ranked]
    if len(ranked) > 1 and key[0] == key[1]:
        return None
    top = 2.0
    step = top / max(len(ranked) - 1, 1)
    return Proposal(value={k: round(top - i * step, 4) for i, k in enumerate(ranked)}, note="fewest hops, highest confidence")


# ------------------------------------------------------------------------- choose / route
@rule("chart_selection")
def chart_selection(state: dict, facts: dict, q: Question) -> Proposal | None:
    """The chart rules (skills/viz.choose_chart) already picked; only a tie (no hint) reaches a model."""
    if q.hint in q.options:
        return Proposal(value=q.hint, probabilities={q.hint: 1.0}, final=True, note="chart rules")
    return None


@rule("feedback_classification")
def feedback_classification(state: dict, facts: dict, q: Question) -> Proposal:
    """Unclassified feedback is treated as a redirect (the v1 default)."""
    default = q.default if q.default in q.options else next(iter(q.options), None)
    return Proposal(value=default, probabilities={default: 1.0} if default else {}, note="default kind")


@rule("ask_route")
def ask_route(state: dict, facts: dict, q: Question) -> Proposal | None:
    """Tool-first ladder: a verified query, else a tool, else generation; decline when an input is missing.
    facts: verified_match (score 0..1), tool_match (score 0..1), missing_inputs (list), verified_rejected
    (registry entries whose words matched but whose SQL does not answer the question, with reasons)."""
    if facts.get("missing_inputs") and "decline" in q.options:
        return Proposal(value="decline", probabilities={"decline": 1.0}, final=True, note="required input missing")
    verified, tool = float(facts.get("verified_match") or 0.0), float(facts.get("tool_match") or 0.0)
    if verified >= 0.8 and "verified_query" in q.options and verified - tool >= 0.1:
        return Proposal(value="verified_query", probabilities={"verified_query": 1.0}, final=True, note="verified query matched")
    if tool >= 0.8 and "tool" in q.options and tool - verified >= 0.1:
        return Proposal(value="tool", probabilities={"tool": 1.0}, final=True, note="tool matched")
    if max(verified, tool) < 0.5 and "generate" in q.options:
        rejected = facts.get("verified_rejected") or []
        if rejected:  # words matched, the SQL answers another question: record why it was not served
            return Proposal(value="generate", probabilities={"generate": 1.0}, final=True,
                            note=f"verified query {rejected[0]['name']} does not fit: {rejected[0]['reasons'][0]}",
                            details={"verified_rejected": rejected})
        return Proposal(value="generate", probabilities={"generate": 1.0}, final=True, note="nothing verified matched")
    return None  # two close candidates: a tie for the next backend


# ------------------------------------------------------------------------- escalate_only
@rule("alert_triage")
def alert_triage(state: dict, facts: dict, q: Question) -> Proposal:
    """Deterministic materiality (completeness, minimum effect; dedupe happens before triage).
    An immaterial signal keeps its severity and is not sent to a model (no noise escalation);
    a critical one cannot go higher, so it is not sent either."""
    reasons = []
    points = facts.get("points")
    if points is not None and int(points) < int(facts.get("min_points", 3)):
        reasons.append(f"only {points} periods observed")
    effect = facts.get("effect")
    min_effect = float(facts.get("min_effect") or 0.0)
    if effect is not None and min_effect and abs(float(effect)) < min_effect:
        reasons.append(f"effect {abs(float(effect)):.1%} below the {min_effect:.1%} minimum")
    material = not reasons
    top = bool(q.levels) and q.baseline == q.levels[-1]
    return Proposal(value=q.baseline, probabilities=_yes_no(1.0 if material else 0.0), final=top or not material,
                    details={"material": material, "reasons": reasons or ["passes completeness and minimum effect"]},
                    note="materiality rules")


# Analysis wording ("drop outliers", "remove closed tickets") narrows scope; these verbs act on systems.
_CONSEQUENTIAL = re.compile(r"\b(delete|truncate|publish|schedule|send|e-?mail|export|share|grant|revoke|deploy|overwrite|"
                            r"drop (?:the )?table)\b", re.I)


@rule("risk_check")
def risk_check(state: dict, facts: dict, q: Question) -> Proposal:
    """Verbs with side effects mark a request consequential; a model may only add risk on top."""
    hit = _CONSEQUENTIAL.search(str(state.get("request") or ""))
    level = q.levels[-1] if hit and q.levels else q.baseline
    return Proposal(value=level, probabilities=_yes_no(1.0 if hit else 0.0), final=bool(hit),
                    note=f"side-effect verb '{hit.group(0)}'" if hit else "no side-effect verb")


@rule("clarify_needed")
def clarify_needed(state: dict, facts: dict, q: Question) -> Proposal:
    """Ask the user when a required input is missing; a model may only add a question, never remove one."""
    missing = list(facts.get("missing_inputs") or [])
    level = q.levels[-1] if missing and q.levels else q.baseline
    return Proposal(value=level, probabilities=_yes_no(1.0 if missing else 0.0), final=bool(missing),
                    details={"missing_inputs": missing}, note="missing inputs" if missing else "inputs complete")


@rule("rev_second_opinion")
def rev_second_opinion(state: dict, facts: dict, q: Question) -> Proposal:
    """REV's deterministic checks already decided verification; the baseline is 'no extra doubt'."""
    return Proposal(value=q.baseline, note="deterministic REV checks")


# -------------------------------------------------------------------------- bounded_stop
@rule("stop_check")
def stop_check(state: dict, facts: dict, q: Question) -> Proposal | None:
    """Continue before the minimum rounds; stop when the last round found nothing new (AUT-005);
    otherwise abstain so a model may break the tie (after the minimum, with enough findings)."""
    round_no, min_rounds = int(facts.get("round") or 0), int(facts.get("min_rounds") or 1)
    if round_no < min_rounds:
        return Proposal(value=False, probabilities=_yes_no(0.0), final=True, note="before the minimum rounds")
    new = facts.get("new_supported_last_round")
    if new is not None and round_no > min_rounds and int(new) == 0:
        return Proposal(value=True, probabilities=_yes_no(1.0), final=True, note="no new supported finding in the last round")
    return None
