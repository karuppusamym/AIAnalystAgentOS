"""Data Scientist Agent (§13.8, §21): executes one hypothesis test with deterministic skills."""
from __future__ import annotations

from analystos.agents.investigator import with_constraints
from analystos.artifacts.registry import link
from analystos.contracts.analysis import AnalysisSpec
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import Experiment, Hypothesis
from analystos.events.bus import emit
from analystos.runtime.context import RunContext


def _dump(v):
    return v.model_dump() if hasattr(v, "model_dump") else v


def test_hypothesis(ctx: RunContext) -> dict:
    from analystos.skills.analysis import run_analysis

    with session_scope() as s:
        h = s.get(Hypothesis, ctx.task.input["hypothesis_id"])
        h.status = "testing"
        s.expunge(h)
    spec = with_constraints(AnalysisSpec.model_validate(h.spec), ctx.run.constraints)
    run_sql = ctx.run_sql(ctx.scope.asset_sources.get(spec.asset))
    ctx.check_control()
    outcome = ctx.tools().invoke("analysis.run", {"hypothesis": h.code, "method": spec.method, "asset": spec.asset},
                                 lambda: run_analysis(spec, run_sql, alpha=ctx.policy.alpha))
    stat = _dump(outcome.stat)
    status = {True: "supported", False: "rejected"}.get(stat.get("supported"), "inconclusive")
    with session_scope() as s:
        exp = Experiment(id=new_id("exp"), workspace_id=ctx.workspace.id, run_id=ctx.run.id, hypothesis_id=h.id,
                         method=spec.method, params=spec.model_dump(), result=stat, query_ids=list(outcome.query_ids), role="primary")
        s.add(exp)
        row = s.get(Hypothesis, h.id)
        row.status = status
        row.evidence = [exp.id, *outcome.query_ids]
        row.conclusion = _conclusion(stat, status)
        link(s, ctx.workspace.id, ("hypothesis", h.id), "tested_by", ("experiment", exp.id), run_id=ctx.run.id)
        for q in outcome.query_ids:
            link(s, ctx.workspace.id, ("experiment", exp.id), "derived_from", ("query", q), run_id=ctx.run.id)
            link(s, ctx.workspace.id, ("query", q), "reads", ("table", spec.asset), run_id=ctx.run.id)
        emit(ctx.workspace.id, "hypothesis.updated", {"code": h.code, "status": status, "test": stat.get("test"),
                                                      "p_value": stat.get("p_value"), "effect_size": stat.get("effect_size")},
             run_id=ctx.run.id, session=s)
    ctx.say(f"{h.code} → {status}: {row.conclusion}", data={"highlights": stat.get("highlights")})
    return {"hypothesis": h.code, "status": status, "experiment_id": exp.id, "query_ids": list(outcome.query_ids)}


def _conclusion(stat: dict, status: str) -> str:
    parts = [f"{stat.get('test')} n={stat.get('n')}"]
    if stat.get("p_value") is not None:
        parts.append(f"p={stat['p_value']:.3g}")
    if stat.get("effect_size") is not None:
        parts.append(f"{stat.get('effect_label') or 'effect'}={stat['effect_size']:.3f}")
    if stat.get("warnings"):
        parts.append("warnings: " + "; ".join(stat["warnings"][:2]))
    return f"{status} ({', '.join(parts)})"
