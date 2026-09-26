"""Semantic Model Agent (§13.12, §24): KPI definitions, each validated by execution (SEM-002/003/005).

Approved metrics of the workspace semantic model (P4-K03) are the stable definitions and are used first;
every other validated KPI becomes a proposal there, awaiting a person's approval."""
from __future__ import annotations

import sqlglot
from sqlglot import exp

from analystos.agents.common import compile_for, llm_json, model_gate
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
    """KPI definitions to keep stable: the scheduled predecessor's, else the workspace's most recent
    completed run (an alert investigation redefined critical_incident_rate live when it had neither)."""
    from sqlalchemy import select

    from analystos.db.models import AnalysisRun, Artifact

    previous = (ctx.run.origin or {}).get("previous_run_id")
    with session_scope() as s:
        if not previous:
            previous = s.scalar(select(AnalysisRun.id).where(AnalysisRun.workspace_id == ctx.workspace.id,
                                                             AnalysisRun.status == "COMPLETED", AnalysisRun.id != ctx.run.id)
                                .order_by(AnalysisRun.finished_at.desc().nulls_last()).limit(1))
        if not previous:
            return []
        out = []
        for a in s.scalars(select(Artifact).where(Artifact.run_id == previous, Artifact.type == "metric")
                           .order_by(Artifact.created_at, Artifact.name)):
            m = MetricDef.model_validate(a.content)
            out.append(m.model_copy(update={"status": "proposed", "validation": {}}))
        return out


def admission_clash(name: str, norm: str, seen: dict[str, str], names: set[str]) -> str | None:
    """Why a candidate KPI may not be admitted, given those already admitted (in candidate order:
    carried-forward definitions first). A name keeps its first definition — seen live: a model
    re-proposed critical_incident_rate with a wider expression and silently replaced the stable KPI."""
    if norm in seen:
        return f"duplicate of {seen[norm]}"
    if name in names:
        return "name already defined with a different expression; KPI definitions are stable across runs"
    return None


def approved_workspace_metrics(ctx: RunContext, columns: set[str], dialect: str) -> list[MetricDef]:
    """The workspace semantic model's approved KPIs that apply to this dataset (unqualified columns it
    has): they are the stable definitions and come before anything carried forward or proposed.
    The versions are the ones the run bound (ADR-0021): a pinned schedule fire keeps measuring with
    its baseline's metric versions after a newer version is approved."""
    from sqlalchemy import select

    from analystos.capabilities.binding import bound_metric_ids
    from analystos.db.models import SemanticMetric
    from analystos.semantic.service import approved_metrics, to_metricdef

    bound = bound_metric_ids(ctx.run)
    with session_scope() as s:
        if bound is None:  # a run bound before P7-03
            rows = list(approved_metrics(s, ctx.workspace.id).values())
        else:
            rows = list(s.scalars(select(SemanticMetric).where(SemanticMetric.workspace_id == ctx.workspace.id,
                                                               SemanticMetric.id.in_(bound)).order_by(SemanticMetric.name)))
    out = []
    for row in rows:
        m = to_metricdef(row)
        if row.status == "deprecated" and bound is not None:  # superseded after the pin: still the pinned approved version
            m = m.model_copy(update={"status": "approved"})
        try:
            qualified = any(c.table for c in sqlglot.parse_one(m.sql_expression, read=dialect).find_all(exp.Column))
        except sqlglot.errors.ParseError:
            continue
        if not qualified and _valid_expression(m.sql_expression, columns, dialect) is None:
            out.append(m)
    return out


def valid_metric_definitions(data: object) -> str | None:
    """Escalation check (schema): proposed metrics must validate as MetricDef; an empty list is an answer."""
    if not isinstance(data, dict) or not isinstance(data.get("metrics", []), list):
        return "answer is not an object with a metrics list"
    metrics = data.get("metrics") or []
    ok = 0
    for m in metrics:
        try:
            MetricDef.model_validate({**m, "status": "proposed"})
            ok += 1
        except Exception:
            continue
    return None if ok or not metrics else f"none of {len(metrics)} metric definitions passed schema validation"


def define_metrics(ctx: RunContext) -> dict:
    ds, content = dataset_def(ctx.run.id)
    dialect = content.get("dialect", "postgres")
    columns = {c["name"] for c in ds.columns}
    # KPI definitions stay stable: approved workspace metrics first, then the previous run's validated
    # metrics, so equivalent proposals de-duplicate onto the existing names and deltas compare like with like.
    approved = approved_workspace_metrics(ctx, columns, dialect)
    candidates = approved + previous_metrics(ctx) + default_metrics(ds)
    origin = ["approved"] * len(approved) + ["carried"] * (len(candidates) - len(approved))
    payload = compile_for(ctx, "semantic_modeling", {"objective": ctx.run.objective, "dataset_columns": ds.columns,
                                                     "existing": [m.name for m in candidates]})
    data, model = llm_json(ctx, "semantic_modeling", "semantic_modeling.v2", payload, validate=valid_metric_definitions) \
        if model_gate(ctx, "semantic_modeling", payload, deterministic_ok=len(candidates) >= 4) else (None, "deterministic")
    for m in (data or {}).get("metrics", []) if isinstance(data, dict) else []:
        try:
            candidates.append(MetricDef.model_validate({**m, "status": "proposed"}))
            origin.append("model")
        except Exception:
            continue
    run_sql = ctx.run_sql(content.get("source_id"))
    accepted, seen, rejected, clashing = [], {}, [], []
    names: set[str] = set()
    for m, source in zip(candidates, origin, strict=True):
        problem = _valid_expression(m.sql_expression, columns, dialect)
        norm = _normalize(m.sql_expression, dialect)
        if problem:
            rejected.append({"metric": m.name, "reason": problem})
            continue
        clash = admission_clash(m.name, norm, seen, names)
        if clash:
            rejected.append({"metric": m.name, "reason": clash})
            if source == "model":  # SEM-005: flagged in the semantic model's conflicts, not silently dropped
                clashing.append(m)
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
        names.add(m.name)
        m.validation = {"value": value, "query_id": r.query_id, "validated_by": "execution"}
        m.status = "approved" if source == "approved" else "validated"
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
        from analystos.semantic import service as semantic

        semantic.save_model(s, ctx.workspace.id, actor=f"agent:{ctx.agent.id}", origin=f"agent:{ctx.agent.id}",
                            datasets=[semantic.dataset_from_def(ds)], run_id=ctx.run.id)
        proposed = []
        for m in accepted:
            art = save_artifact(s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, type_="metric", name=m.name,
                                content=m.model_dump(), creator_agent=ctx.agent.id, status="validated")
            link(s, ctx.workspace.id, ("metric", art.id), "defined_on", ("dataset", content["artifact_id"]), run_id=ctx.run.id)
            ctx.event("metric.created", {"name": m.name, "display_name": m.display_name, "value": m.validation.get("value")})
            if m.status != "approved":  # a validated KPI becomes a proposal; only a person makes it a stable definition
                row, created = semantic.propose_metric(
                    s, ctx.workspace.id, semantic.from_metricdef(m, dataset=ds.name, sql_dialect=dialect),
                    proposed_by=ctx.run.requested_by, via=f"agent:{ctx.agent.id}", run_id=ctx.run.id, source=("metric", art.id))
                if created:
                    proposed.append(m.name)
        for m in clashing:
            semantic.propose_metric(s, ctx.workspace.id, semantic.from_metricdef(m, dataset=ds.name, sql_dialect=dialect),
                                    proposed_by=ctx.run.requested_by, via=f"agent:{ctx.agent.id}", run_id=ctx.run.id)
    n_approved = sum(1 for m in accepted if m.status == "approved")
    ctx.say(f"Defined {len(accepted)} validated KPIs ({', '.join(m.display_name for m in accepted)}); rejected {len(rejected)}"
            + (f" (LLM proposals from {model})" if data else "")
            + f". {n_approved} are approved workspace metrics"
            + (f"; proposed for approval in the workspace semantic model: {', '.join(proposed)}" if proposed else ""))
    return {"metrics": [m.name for m in accepted], "rejected": rejected, "semantic_model_id": model_art.id,
            "approved_metrics": [m.name for m in accepted if m.status == "approved"], "proposed_metrics": proposed}
