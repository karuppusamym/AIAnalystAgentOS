"""Data Scientist Agent (§13.8, §21): executes one hypothesis test with deterministic skills.

Since P8-15 the test reads only the discovery part of the table (`evidence.holdout`): the held-out rows
are kept for REV's one locked confirmation test. The result records which rows it read."""
from __future__ import annotations

from analystos.agents.investigator import with_constraints
from analystos.artifacts.registry import link, link_queries
from analystos.contracts.analysis import AnalysisSpec
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import Experiment, Hypothesis
from analystos.events.bus import emit
from analystos.runtime.context import RunContext
from analystos.services.platform_settings import get as platform


def _dump(v):
    return v.model_dump() if hasattr(v, "model_dump") else v


def discovery_for(ctx: RunContext, asset: str, dialect: str):
    """(the discovery partition of `asset`, None) or (None, why this table has no held-out rows)."""
    from analystos.agents.critic import MIN_N
    from analystos.evidence.holdout import catalog_facts, discovery_partition, readable_columns

    settings = platform().analysis
    with session_scope() as s:
        key, rows = catalog_facts(s, ctx.scope.asset_sources.get(asset), asset)
    return discovery_partition(fraction=settings.holdout_fraction, dialect=dialect, readable=readable_columns(ctx.scope, asset),
                               key=key, rows=rows, min_rows=max(MIN_N, settings.min_sample_size))


def test_hypothesis(ctx: RunContext) -> dict:
    from analystos.evidence.holdout import partition_record
    from analystos.skills.analysis import run_analysis

    with session_scope() as s:
        h = s.get(Hypothesis, ctx.task.input["hypothesis_id"])
        h.status = "testing"
        s.flush()
        s.expunge(h)
    spec = with_constraints(AnalysisSpec.model_validate(h.spec), ctx.run.constraints)
    run_sql = ctx.run_sql(ctx.scope.asset_sources.get(spec.asset))
    partition, no_holdout = discovery_for(ctx, spec.asset, getattr(run_sql, "dialect", "duckdb"))
    ctx.check_control()
    outcome = ctx.tools().invoke("analysis.run", {"hypothesis": h.code, "method": spec.method, "asset": spec.asset},
                                 lambda: run_analysis(spec, run_sql, alpha=ctx.policy.alpha,
                                                      sample_rows=platform().analysis.sample_rows, partition=partition))
    stat = _dump(outcome.stat)
    stat["details"] = {**(stat.get("details") or {}), "partition": partition_record(partition, no_holdout)}
    status = {True: "supported", False: "rejected"}.get(stat.get("supported"), "inconclusive")
    ctx.check_output("experiment", {"method": spec.method, "params": spec.model_dump(), "result": stat,
                                    "query_ids": list(outcome.query_ids), "role": "primary"})
    with session_scope() as s:
        exp = Experiment(id=new_id("exp"), workspace_id=ctx.workspace.id, run_id=ctx.run.id, hypothesis_id=h.id,
                         method=spec.method, params=spec.model_dump(), result=stat, query_ids=list(outcome.query_ids), role="primary")
        s.add(exp)
        row = s.get(Hypothesis, h.id)
        row.status = status
        row.evidence = [exp.id, *outcome.query_ids]
        row.conclusion = _conclusion(stat, status)
        link(s, ctx.workspace.id, ("hypothesis", h.id), "tested_by", ("experiment", exp.id), run_id=ctx.run.id)
        link_queries(s, ctx.workspace.id, ("experiment", exp.id), list(outcome.query_ids), run_id=ctx.run.id,
                     assets=[spec.asset] if spec.asset else None)
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
