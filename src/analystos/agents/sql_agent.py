"""SQL Engineer Agent (§13.11): reusable analytical dataset + ad-hoc NL->SQL with gateway repair loop."""
from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlalchemy import select

from analystos.agents.common import asset_rows, catalog_for_prompt, llm_json, task_output
from analystos.artifacts.registry import link, save_artifact
from analystos.contracts.analysis import AnalysisSpec, Derivation, Filter
from analystos.contracts.bi import DatasetDef
from analystos.core.errors import AnalystOSError, InvalidInput, SQLRejected
from analystos.db.base import session_scope
from analystos.db.models import Hypothesis, Insight, Relationship, SourceAsset
from analystos.governance.budgets import ASK_PURPOSE, ask_actor, check_ask_budget
from analystos.runtime.context import RunContext


def slug(text: str, n: int = 40) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")[:n] or "col"


def derivation_alias(d: Derivation) -> str:
    if d.type == "column":
        return d.column
    base = {"duration_hours": f"{d.column}_to_{d.end_column}_hours", "after_hours": f"{d.column}_after_hours",
            "bucket": f"{d.column}_bucket", "equals": f"{d.column}_is_{slug(str(d.value), 20)}", "is_true": f"{d.column}_true",
            "date_trunc": f"{d.column}_{d.grain}", "hour_of_day": f"{d.column}_hour", "day_of_week": f"{d.column}_dow"}[d.type]
    return slug(base, 60)


def primary_asset(ctx: RunContext, verified_specs: list[AnalysisSpec]) -> str:
    counts: dict[str, int] = {}
    for s in verified_specs:
        counts[s.asset] = counts.get(s.asset, 0) + 1
    if counts:
        return max(counts, key=counts.get)
    rows = sorted(asset_rows(ctx), key=lambda ac: -(ac[0].row_count or 0))
    return f"{rows[0][0].schema_name}.{rows[0][0].name}"


def build_dataset(ctx: RunContext) -> dict:
    from analystos.skills.sqlbuild import render_derivation, render_filter

    with session_scope() as s:
        specs = [AnalysisSpec.model_validate(h.spec) for h in s.scalars(
            select(Hypothesis).join(Insight, Insight.hypothesis_id == Hypothesis.id)
            .where(Insight.run_id == ctx.run.id, Insight.status == "verified"))]
    asset = primary_asset(ctx, specs)
    source_id = ctx.scope.asset_sources[asset]
    dialect = ctx.scope.source_dialects.get(source_id, "postgres")
    denied = set(ctx.scope.denied_columns)
    raw_cols = [c for c in ctx.scope.columns[asset] if f"{asset}.{c}" not in denied]
    derived: dict[str, Derivation] = {}
    types = {c.name: c.semantic_type for a, cols in asset_rows(ctx) if f"{a.schema_name}.{a.name}" == asset for c in cols}
    for spec in (sp for sp in specs if sp.asset == asset):
        for d in (spec.outcome, spec.segment, spec.time, *spec.drivers):
            if d is not None and d.type != "column":
                derived.setdefault(derivation_alias(d), d)
    time_col = next((c for c in raw_cols if types.get(c) == "datetime" and re.search(r"opened|created|start", c)), None) or \
        next((c for c in raw_cols if types.get(c) == "datetime"), None)
    if time_col:
        for d in (Derivation(type="date_trunc", column=time_col, grain="month"), Derivation(type="day_of_week", column=time_col),
                  Derivation(type="hour_of_day", column=time_col)):
            derived.setdefault(derivation_alias(d), d)
    select_parts = [f't."{c}"' for c in raw_cols]
    select_parts += [f'{render_derivation(d, dialect, table_alias="t")} AS "{alias}"' for alias, d in derived.items()]
    joins, join_parts = [], []
    with session_scope() as s:
        a_row = s.scalar(select(SourceAsset).where(SourceAsset.workspace_id == ctx.workspace.id,
                                                   SourceAsset.schema_name == asset.split(".")[0], SourceAsset.name == asset.split(".")[1]))
        rels = list(s.scalars(select(Relationship).where(Relationship.workspace_id == ctx.workspace.id,
                                                         Relationship.from_asset_id == a_row.id, Relationship.validated.is_(True))))
        targets = {r.to_asset_id: s.get(SourceAsset, r.to_asset_id) for r in rels}
    for i, r in enumerate(rels):
        tgt = targets.get(r.to_asset_id)
        if tgt is None:
            continue
        tfq = f"{tgt.schema_name}.{tgt.name}"
        if tfq not in ctx.scope.assets or ctx.scope.asset_sources.get(tfq) != source_id or f"{r.from_column}_name" in raw_cols:
            continue
        name_col = next((c for c in ctx.scope.columns.get(tfq, []) if c in ("name", "number", "u_name") and f"{tfq}.{c}" not in denied), None)
        if not name_col:
            continue
        alias = f"j{i}"
        joins.append(f'LEFT JOIN {tfq} {alias} ON t."{r.from_column}" = {alias}."{r.to_column}"')
        join_parts.append(f'{alias}."{name_col}" AS "{slug(r.from_column + "_" + name_col)}"')
    filters = [Filter(column=f["column"], op=f["op"], value=f.get("value"), origin="user_redirect")
               for f in ctx.run.constraints.get("filters", []) if f.get("asset") in (None, asset)]
    where = (" WHERE " + " AND ".join(render_filter(f, dialect, table_alias="t") for f in filters)) if filters else ""
    sql = f"SELECT {', '.join(select_parts + join_parts)} FROM {asset} t {' '.join(joins)}{where}"
    sql = sqlglot.transpile(sql, read=dialect, write=dialect)[0]
    run_sql = ctx.run_sql(source_id)
    count = ctx.tools().invoke("sql.execute", {"purpose": "dataset.validate"},
                               lambda: run_sql(f"SELECT COUNT(*) AS n FROM ({sql}) d", purpose="dataset.validate"))
    sample = run_sql(f"SELECT * FROM ({sql}) d", purpose="dataset.sample", max_rows=5)
    columns = [{"name": c, "semantic_type": ("boolean" if derived.get(c) and derived[c].type in ("equals", "is_true", "after_hours")
                                              else "numeric" if derived.get(c) and derived[c].type in ("duration_hours", "hour_of_day", "day_of_week")
                                              else "categorical" if derived.get(c) and derived[c].type == "bucket"
                                              else "datetime" if derived.get(c) and derived[c].type == "date_trunc" else types.get(c)),
                "derived": c in derived, "label": derived[c].label if c in derived else None} for c in sample.columns]
    name = f"aos_{slug(ctx.run.objective, 30)}_{ctx.run.id[-6:]}"
    ds = DatasetDef(name=name, description=f"Analytical dataset for: {ctx.run.objective[:200]}", sql=sql, columns=columns,
                    time_column=derivation_alias(Derivation(type="date_trunc", column=time_col, grain="month")) if time_col else None,
                    source_assets=[asset] + [f"{t.schema_name}.{t.name}" for t in targets.values() if t], row_count=int(count.rows[0][0]))
    with session_scope() as s:
        art = save_artifact(s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, type_="dataset", name=name,
                            content={**ds.model_dump(), "raw_time_column": time_col, "dialect": dialect, "source_id": source_id},
                            creator_agent=ctx.agent.id)
        for a in ds.source_assets:
            link(s, ctx.workspace.id, ("dataset", art.id), "built_from", ("table", a), run_id=ctx.run.id)
        for code in [i.code for i in s.scalars(select(Insight).where(Insight.run_id == ctx.run.id, Insight.status == "verified"))]:
            link(s, ctx.workspace.id, ("insight", code), "reproducible_in", ("dataset", art.id), run_id=ctx.run.id)
    ctx.event("dataset.created", {"name": name, "rows": ds.row_count, "columns": len(columns), "derived": list(derived)})
    ctx.say(f"Built virtual analytical dataset {name}: {ds.row_count:,} rows, {len(columns)} columns "
            f"({len(derived)} derived: {', '.join(derived)}){'; user filters applied' if filters else ''}. No source writes (minimum ETL).")
    return {"artifact_id": art.id, "name": name, "rows": ds.row_count}


# ------------------------------------------------------------------------------------------- ad hoc
def _authorize_ask(ctx: Any) -> None:
    """Ask runs model-written SQL as the SQL agent's ``sql.execute`` tool: same gate as a run."""
    from analystos.contracts.policy import ExecutionIdentity
    from analystos.tools.registry import ToolRuntime

    identity = ExecutionIdentity(user_id=ctx.user.id, workspace_id=ctx.workspace.id, agent_id=ctx.agent.id, purpose=ASK_PURPOSE)
    ToolRuntime(user=ctx.user, identity=identity, agent=ctx.agent).authorize("sql.execute", {"purpose": ASK_PURPOSE})


def _check_budget(ctx: Any) -> None:
    with session_scope() as s:
        check_ask_budget(s, ctx.workspace.id, ctx.user.id, ctx.policy)


def ask(ctx: RunContext, question: str, *, max_repairs: int = 2) -> dict[str, Any]:
    """NL question -> governed SQL -> result. Repairs use the gateway's rejection message.
    Gate and budget are checked before any model call, and the budget again before every attempt."""
    _authorize_ask(ctx)
    _check_budget(ctx)
    catalog = catalog_for_prompt(ctx)
    dialect = next(iter(ctx.scope.source_dialects.values()), "postgres")
    data, model = llm_json(ctx, "sql_generation", "sql_generation.v1", {"question": question, "dialect": dialect, "catalog": catalog},
                           prompt_vars={"dialect": dialect})
    if not isinstance(data, dict) or not data.get("sql"):
        raise InvalidInput("SQL generation unavailable (no model route) — write SQL directly in the query console")
    attempts = []
    sql = str(data["sql"])
    for attempt in range(max_repairs + 1):
        if attempt:
            _check_budget(ctx)  # every attempt counts; over budget ends the loop (no repair)
        try:
            result = ctx.services.gateway.execute(ctx.scope, sql, actor=ask_actor(ctx.user.id), purpose=ASK_PURPOSE,
                                                  run_id=None, task_id=None)
            return {"sql": sql, "explanation": data.get("explanation"), "chart": data.get("chart"), "model": model,
                    "attempts": attempts, "result": {"query_id": result.query_id, "columns": result.columns,
                                                     "rows": result.rows[:500], "row_count": result.row_count, "truncated": result.truncated}}
        except (SQLRejected, AnalystOSError) as exc:
            attempts.append({"sql": sql, "error": exc.message})
            if attempt == max_repairs:
                raise
            fix, _ = llm_json(ctx, "sql_repair", "sql_repair.v1", {"question": question, "dialect": dialect, "sql": sql,
                                                                  "error": exc.message, "catalog": catalog})
            if not isinstance(fix, dict) or not fix.get("sql"):
                raise
            sql = str(fix["sql"])
    raise InvalidInput("unreachable")


def dataset_def(run_id: str) -> tuple[DatasetDef, dict]:
    content = None
    with session_scope() as s:
        from analystos.db.models import Artifact

        art = s.scalar(select(Artifact).where(Artifact.run_id == run_id, Artifact.type == "dataset").order_by(Artifact.created_at.desc()))
        if art is None:
            raise InvalidInput("no dataset artifact for this run")
        content = dict(art.content)
        content["artifact_id"] = art.id
    return DatasetDef.model_validate({k: content[k] for k in DatasetDef.model_fields if k in content}), content


__all__ = ["ask", "build_dataset", "dataset_def", "derivation_alias", "task_output"]
