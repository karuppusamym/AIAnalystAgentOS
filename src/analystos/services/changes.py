"""'What changed' between two runs of the same recurring analysis (SCH-004, §37 example).

Findings are matched by their claim (method, outcome, segment, top group) — not by wording — so a
rephrased narrative is still the same finding. KPIs are matched by name."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos import methods
from analystos.db.models import Artifact, Hypothesis, Insight
from analystos.methods.base import AnalysisMethod


def claim_key(spec: dict[str, Any], highlights: dict[str, Any] | None) -> tuple:
    """Identity of a claim, as the spec's method defines it (analystos.methods): what was tested
    (method, outcome, subject, population filters) and which group came out on top. Wording is not
    part of it. The last element is the top group; everything before it is the question."""
    name = spec.get("method")
    method = methods.get(name) if name in methods.names() else _Unregistered(name)
    return tuple(method.claim_key(spec, highlights))


class _Unregistered(AnalysisMethod):
    """Claims of a method no longer registered (an old run) keep the generic identity."""

    def __init__(self, name: Any):
        self.name = name  # type: ignore[misc]


def _claims(session: Session, run_id: str) -> dict[tuple, dict[str, Any]]:
    from analystos.db.models import Experiment

    out: dict[tuple, dict[str, Any]] = {}
    rows = session.execute(select(Insight, Hypothesis).join(Hypothesis, Insight.hypothesis_id == Hypothesis.id)
                           .where(Insight.run_id == run_id, Insight.status == "verified")).all()
    for ins, hyp in rows:
        exp = session.scalar(select(Experiment).where(Experiment.hypothesis_id == hyp.id, Experiment.role == "primary"))
        hl = (exp.result or {}).get("highlights") if exp else {}
        out[claim_key(hyp.spec, hl)] = {"code": ins.code, "title": ins.title, "finding": ins.finding, "id": ins.id,
                                        "effect": (exp.result or {}).get("effect_size") if exp else None, "highlights": hl}
    return out


def _metrics(session: Session, run_id: str) -> dict[str, Any]:
    return {a.name: (a.content.get("validation") or {}).get("value")
            for a in session.scalars(select(Artifact).where(Artifact.run_id == run_id, Artifact.type == "metric"))}


def _tested(session: Session, run_id: str) -> set[tuple]:
    """(method, outcome, segment/drivers, filters) of every hypothesis the run actually tested."""
    return {claim_key(h.spec, None)[:-1] for h in session.scalars(select(Hypothesis).where(
        Hypothesis.run_id == run_id, Hypothesis.status.in_(["supported", "rejected", "inconclusive"])))}


def diff_runs(session: Session, previous_run_id: str, run_id: str) -> dict[str, Any]:
    before, after = _claims(session, previous_run_id), _claims(session, run_id)
    tested_now = _tested(session, run_id)
    new = [after[k] for k in after if k not in before]
    gone = [k for k in before if k not in after]
    # A previous finding is "resolved" only if the same question was tested again and no longer holds.
    resolved = [before[k] for k in gone if k[:-1] in tested_now]
    not_retested = [before[k] for k in gone if k[:-1] not in tested_now]
    persisting, changed = [], []
    for k in after.keys() & before.keys():
        a, b = after[k], before[k]
        ea, eb = a.get("effect"), b.get("effect")
        moved = isinstance(ea, (int, float)) and isinstance(eb, (int, float)) and eb and abs(ea - eb) / abs(eb) >= 0.25
        (changed if moved else persisting).append({**a, "previous_effect": eb})
    m_after, m_before = _metrics(session, run_id), _metrics(session, previous_run_id)
    metrics = []
    for name, value in m_after.items():
        prev = m_before.get(name)
        delta = (value - prev) / prev if isinstance(value, (int, float)) and isinstance(prev, (int, float)) and prev else None
        metrics.append({"name": name, "value": value, "previous_value": prev, "pct_change": None if delta is None else round(delta, 4)})
    return {"previous_run_id": previous_run_id, "new": new, "persisting": persisting, "changed": changed, "resolved": resolved,
            "not_retested": not_retested, "metrics": metrics}
