"""Semantic Model Agent (§13.12, §24): KPI definitions, each validated by execution (SEM-002/003/005)."""
from __future__ import annotations

import sqlglot
from sqlglot import exp

from analystos.agents.common import llm_json, model_gate
from analystos.agents.sql_agent import dataset_def
from analystos.artifacts.registry import link, save_artifact
from analystos.contracts.bi import MetricDef
from analystos.core.errors import AnalystOSError
from analystos.db.base import session_scope
from analystos.runtime.context import RunContext

AGGS = (exp.Count, exp.Avg, exp.Sum, exp.Min, exp.Max, exp.PercentileCont, exp.Median, exp.Stddev)


def default_metrics(ds) -> list[MetricDef]:
    cols = {c["name"]: c for c in ds.columns}
    out = [MetricDef(name="record_count", display_name="Record volume", definition="Number of records in the analytical dataset.",
                     sql_expression="COUNT(*)", format="number", grain="record")]
    for name, c in cols.items():
        if c.get("derived") and c.get("semantic_type") == "boolean":
            label = c.get("label") or name
            out.append(MetricDef(name=f"{name}_rate", display_name=f"{label[:1].upper() + label[1:]} rate",
                                 definition=f"Share of records where {label}.",
                                 sql_expression=f'AVG(CASE WHEN "{name}" THEN 1.0 ELSE 0.0 END)', format="percent", grain="record",
                                 source_columns=[name]))
        if c.get("derived") and name.endswith("_hours"):
            label = c.get("label") or name
            out.append(MetricDef(name=f"avg_{name}", display_name=f"Mean {label}", definition=f"Average {label} (e.g. MTTR).",
                                 sql_expression=f'AVG("{name}")', format="hours", grain="record", source_columns=[name]))
            out.append(MetricDef(name=f"median_{name}", display_name=f"Median {label}", definition=f"Median {label}.",
                                 sql_expression=f'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "{name}")', format="hours",
                                 grain="record", source_columns=[name]))
        if not c.get("derived") and c.get("semantic_type") == "numeric" and "count" in name:
            out.append(MetricDef(name=f"avg_{name}", display_name=f"Average {name.replace('_', ' ')}",
                                 definition=f"Average {name.replace('_', ' ')} per record.", sql_expression=f'AVG("{name}")',
                                 format="number", grain="record", source_columns=[name]))
    return out


def _normalize(expr: str, dialect: str) -> str | None:
    """Canonical form for duplicate detection: quoting, case and whitespace do not matter."""
    try:
        tree = sqlglot.parse_one(expr, read=dialect)
    except sqlglot.errors.ParseError:
        return None
    for ident in tree.find_all(exp.Identifier):
        ident.set("quoted", False)
    return tree.sql(dialect=dialect, normalize=True).lower()


def _valid_expression(expr: str, columns: set[str], dialect: str) -> str | None:
    try:
        tree = sqlglot.parse_one(expr, read=dialect)
    except sqlglot.errors.ParseError as exc:
        return f"unparseable: {exc}"
    if not any(isinstance(n, AGGS) for n in tree.walk()):
        return "not an aggregate expression"
    unknown = [c.name for c in tree.find_all(exp.Column) if c.name not in columns]
    if unknown:
        return f"unknown dataset columns: {', '.join(unknown)}"
    if any(isinstance(n, (exp.Select, exp.Subquery)) for n in tree.walk()):
        return "subqueries are not allowed in metric expressions"
    return None


def previous_metrics(ctx: RunContext) -> list[MetricDef]:
    previous = (ctx.run.origin or {}).get("previous_run_id")
    if not previous:
        return []
    from sqlalchemy import select

    from analystos.db.models import Artifact

    with session_scope() as s:
        out = []
        for a in s.scalars(select(Artifact).where(Artifact.run_id == previous, Artifact.type == "metric").order_by(Artifact.created_at)):
            m = MetricDef.model_validate(a.content)
            out.append(m.model_copy(update={"status": "proposed", "validation": {}}))
        return out


def define_metrics(ctx: RunContext) -> dict:
    ds, content = dataset_def(ctx.run.id)
    dialect = content.get("dialect", "postgres")
    columns = {c["name"] for c in ds.columns}
    # Recurring analysis keeps KPI definitions stable: previous validated metrics come first, so
    # equivalent proposals de-duplicate onto the existing names and deltas compare like with like.
    candidates = previous_metrics(ctx) + default_metrics(ds)
    payload = {"objective": ctx.run.objective, "dataset_columns": ds.columns, "existing": [m.name for m in candidates]}
    data, model = llm_json(ctx, "semantic_modeling", "semantic_modeling.v2", payload) \
        if model_gate(ctx, "semantic_modeling", payload, deterministic_ok=len(candidates) >= 4) else (None, "deterministic")
    for m in (data or {}).get("metrics", []) if isinstance(data, dict) else []:
        try:
            candidates.append(MetricDef.model_validate({**m, "status": "proposed"}))
        except Exception:
            continue
    run_sql = ctx.run_sql(content.get("source_id"))
    accepted, seen, rejected = [], {}, []
    for m in candidates:
        problem = _valid_expression(m.sql_expression, columns, dialect)
        norm = _normalize(m.sql_expression, dialect)
        if problem:
            rejected.append({"metric": m.name, "reason": problem})
            continue
        if norm in seen:
            rejected.append({"metric": m.name, "reason": f"duplicate of {seen[norm]}"})
            continue
        try:
            r = run_sql(f"SELECT {m.sql_expression} AS v FROM ({ds.sql}) d", purpose=f"metric.validate.{m.name}")
            value = r.rows[0][0]
        except AnalystOSError as exc:
            rejected.append({"metric": m.name, "reason": f"execution failed: {exc.message[:200]}"})
            continue
        if m.format == "percent" and isinstance(value, (int, float)) and not 0 <= value <= 1:
            rejected.append({"metric": m.name, "reason": f"percent metrics must be fractions in [0, 1]; got {value}"})
            continue
        seen[norm] = m.name
        m.validation = {"value": value, "query_id": r.query_id, "validated_by": "execution"}
        m.status = "validated"
        m.dimensions = m.dimensions or [c["name"] for c in ds.columns if c.get("semantic_type") in ("categorical",)][:8]
        accepted.append(m)
    accepted = accepted[:10]
    with session_scope() as s:
        model_art = save_artifact(s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, type_="semantic_model",
                                  name=f"Semantic model {ds.name}", content={"dataset": ds.name, "metrics": [m.model_dump() for m in accepted],
                                                                             "dimensions": [c for c in ds.columns if c.get("semantic_type") in ("categorical", "boolean")],
                                                                             "time_column": ds.time_column, "rejected": rejected},
                                  creator_agent=ctx.agent.id)
        link(s, ctx.workspace.id, ("semantic_model", model_art.id), "models", ("dataset", content["artifact_id"]), run_id=ctx.run.id)
        for m in accepted:
            art = save_artifact(s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, type_="metric", name=m.name,
                                content=m.model_dump(), creator_agent=ctx.agent.id, status="validated")
            link(s, ctx.workspace.id, ("metric", art.id), "defined_on", ("dataset", content["artifact_id"]), run_id=ctx.run.id)
            ctx.event("metric.created", {"name": m.name, "display_name": m.display_name, "value": m.validation.get("value")})
    ctx.say(f"Defined {len(accepted)} validated KPIs ({', '.join(m.display_name for m in accepted)}); rejected {len(rejected)}"
            + (f" (LLM proposals from {model})" if data else ""))
    return {"metrics": [m.name for m in accepted], "rejected": rejected, "semantic_model_id": model_art.id}
