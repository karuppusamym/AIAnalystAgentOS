"""SQL Engineer Agent (§13.11): reusable analytical dataset + ad-hoc NL->SQL with gateway repair loop."""
from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlalchemy import select

from analystos.agents.common import asset_rows, catalog_for_prompt, compact_json, compile_for, llm_json, task_output
from analystos.artifacts.registry import link, save_artifact
from analystos.capabilities import packs as pack_registry
from analystos.contracts.analysis import AnalysisSpec, Derivation, Filter
from analystos.contracts.bi import DatasetDef
from analystos.core.errors import AnalystOSError, InvalidInput, SpendCapReached, SQLRejected
from analystos.db.base import session_scope
from analystos.db.models import Hypothesis, Insight, Relationship, SourceAsset
from analystos.governance.budgets import ASK_PURPOSE, ask_actor, check_ask_budget
from analystos.runtime.context import RunContext


def has_sql(data: Any) -> str | None:
    """Escalation check for SQL generation/repair: the answer must carry a statement."""
    return None if isinstance(data, dict) and str(data.get("sql") or "").strip() else "no SQL statement in the answer"


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
    hints = pack_registry.hints()
    start_words = "|".join(map(re.escape, ("created", "start", *hints.event_start)))
    time_col = next((c for c in raw_cols if types.get(c) == "datetime" and re.search(start_words, c)), None) or \
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
        name_col = next((c for c in ctx.scope.columns.get(tfq, []) if c in ("name", "number", *hints.display_columns)
                         and f"{tfq}.{c}" not in denied), None)
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
# ask_route options (spec v3 §4.2): the ladder, tool-first. A rule decides; a model may only break a tie.
ASK_ROUTES = {"verified_query": "Answer from a verified query in the registry (no model call)",
              "tool": "Answer with a registered parameterised tool",
              "generate": "Write new SQL with the model, then validate and run it through the gateway",
              "decline": "Do not answer yet: a required input is missing, ask the user for it"}


def _stage(ctx: Any, key: str, text: str, **data: Any) -> None:
    """Report a plain-language step to whoever streams this Ask (the SSE route); a no-op elsewhere."""
    fn = getattr(ctx, "on_stage", None)
    if fn is not None:
        fn(key, text, data)


def _authorize_ask(ctx: Any) -> None:
    """Ask runs model-written SQL as the SQL agent's ``sql.execute`` tool: same gate as a run."""
    from analystos.contracts.policy import ExecutionIdentity
    from analystos.tools.registry import ToolRuntime

    identity = ExecutionIdentity(user_id=ctx.user.id, workspace_id=ctx.workspace.id, agent_id=ctx.agent.id, purpose=ASK_PURPOSE)
    ToolRuntime(user=ctx.user, identity=identity, agent=ctx.agent).authorize("sql.execute", {"purpose": ASK_PURPOSE})


def _check_budget(ctx: Any) -> None:
    with session_scope() as s:
        check_ask_budget(s, ctx.workspace.id, ctx.user.id, ctx.policy)


def _result(result: Any) -> dict[str, Any]:
    return {"query_id": result.query_id, "columns": result.columns, "rows": result.rows[:500], "row_count": result.row_count,
            "truncated": result.truncated, "referenced_assets": list(getattr(result, "referenced_assets", None) or []),
            "cache_hit": bool(getattr(result, "cache_hit", False)), "result_hash": getattr(result, "result_hash", None),
            "duration_ms": getattr(result, "duration_ms", None)}


def decide(ctx: Any, purpose: str, state: dict[str, Any], question: Any, facts: dict[str, Any]) -> dict[str, Any]:
    """One DecisionService decision for Ask, recorded against the Ask turn (`task_id`). A context with
    no model router (tests, embedded callers) gets the purpose's rule directly: the same answer the
    service's `rules` backend gives, not recorded."""
    from analystos.decisions.rules import RULES

    services = getattr(ctx, "services", None)
    if getattr(services, "router", None) is not None and hasattr(ctx, "call_ctx"):
        d = services.decisions.decide(purpose, state, question, facts=facts, ctx=ctx.call_ctx(),
                                      subject=getattr(ctx, "subject", None))
        return {**d.summary(), "authority": d.authority, "probabilities": d.probabilities, "enforced": d.enforced,
                "details": d.details, "note": next((a.get("reason") for a in reversed(d.attempts) if a.get("outcome") == "answered"), None)}
    proposal = RULES[purpose](state, facts, question)
    value = proposal.value if proposal is not None else question.default
    return {"id": None, "purpose": purpose, "backend": "rules" if proposal is not None else "default", "value": value,
            "p": None, "model": None, "fallback_reason": None, "authority": None,
            "probabilities": proposal.probabilities if proposal else {}, "enforced": [], "details": proposal.details if proposal else {},
            "note": proposal.note if proposal else None}


def verified_match_score(score: float) -> float:
    """A registry match (coverage + precision, each past its floor) on the 0..1 scale of `ask_route`:
    the weakest accepted match maps to 0.8, so every match the registry accepts is routed to it by the
    rule, exactly as before the decision existed."""
    from analystos.registries.verified_queries import MIN_COVERAGE, MIN_PRECISION

    floor = MIN_COVERAGE + MIN_PRECISION
    return round(0.8 + 0.2 * max(0.0, min(1.0, (score - floor) / (2.0 - floor))), 4)


def _registry_lookup(ctx: Any, question: str, parameters: dict[str, Any] | None) -> Any:
    """The registry's best compatible match and the closer token matches it rejected (with reasons)."""
    from analystos.registries import verified_queries as vqr

    with session_scope() as s:
        found = vqr.find(s, ctx.workspace.id, question, parameters)
        if found.hit is not None:
            s.expunge(found.hit.entry)
        return found


def _registry_match(ctx: Any, question: str, parameters: dict[str, Any] | None) -> Any:
    return _registry_lookup(ctx, question, parameters).hit


def _record_skip(ctx: Any, reason: str, avoided: int) -> None:
    router = getattr(ctx, "router", None)
    if router is not None:
        router.record_skip("sql_generation", ctx.call_ctx(), estimated_tokens=avoided, reason=reason, rung="registry")


def _registry_answer(ctx: Any, question: str, hit: Any) -> dict[str, Any]:
    """Ladder rung L1 (P4-T05): answer from a verified query with no model call, or decline and ask
    for a missing required parameter."""
    from analystos.llm.cache import estimate_tokens
    from analystos.registries import verified_queries as vqr

    entry = {"id": hit.entry.id, "name": hit.entry.name, "pattern": hit.pattern, "score": round(hit.score, 3)}
    template, params, dialect = hit.entry.sql_template, list(hit.entry.parameters or []), hit.entry.dialect
    avoided = estimate_tokens(compact_json({"question": question, "catalog": catalog_for_prompt(ctx)})) + 500
    if hit.missing:
        _record_skip(ctx, f"L1 registry: declined, {entry['name']} needs {', '.join(p['name'] for p in hit.missing)}", avoided)
        missing = [{k: p.get(k) for k in ("name", "type", "column", "values") if p.get(k) is not None} for p in hit.missing]
        return {"status": "needs_input", "answered_by": "registry", "verified_query": entry, "parameters": hit.values,
                "missing": missing, "sql": None, "model": None, "attempts": [], "result": None,
                "explanation": f"This matches the verified query '{entry['name']}', which needs "
                               f"{', '.join(p['name'] for p in hit.missing)}. Say which value to use; nothing was guessed."}
    _stage(ctx, "registry", f"Found a verified answer: '{entry['name']}'", verified_query=entry["id"])
    values = {p["name"]: vqr.coerce(p, hit.values[p["name"]]) for p in params}  # vocabulary spelling, typed
    sql = vqr.render(template, params, values, dialect)
    _stage(ctx, "execute", "Running it through the query gateway (read-only, within your access)")
    result = ctx.services.gateway.execute(ctx.scope, sql, actor=ask_actor(ctx.user.id), purpose=ASK_PURPOSE, run_id=None, task_id=None)
    _record_skip(ctx, f"L1 registry: verified query {entry['name']}", avoided)
    with session_scope() as s:
        vqr.record_hit(s, entry["id"])
    return {"status": "answered", "answered_by": "registry", "verified_query": entry, "parameters": values, "sql": sql,
            "explanation": f"Answered by the verified query '{entry['name']}' (no model call).", "chart": None, "model": None,
            "attempts": [], "result": _result(result)}


def ask_registry(ctx: Any, question: str, parameters: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The registry rung alone; None is a miss (the model path may answer)."""
    hit = _registry_match(ctx, question, parameters)
    return None if hit is None else _registry_answer(ctx, question, hit)


def route(ctx: Any, question: str, hit: Any, rejected: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """`ask_route` (authority `route`): verified query > tool > generate, decline when a required input
    is missing. The facts are deterministic; the rule answers every case but a close tie. A verified
    query whose words matched but whose SQL does not answer the question (`rejected`) scores 0: it is
    never a candidate, and the decision records why it was passed over."""
    from analystos.decisions.types import Question

    facts = {"verified_match": verified_match_score(hit.score) if hit is not None else 0.0,
             "tool_match": 0.0,  # no parameterised Ask tools are registered yet: the rung is never matched
             "missing_inputs": [p["name"] for p in hit.missing] if hit is not None else []}
    if rejected:
        facts["verified_rejected"] = [{k: r[k] for k in ("name", "score", "reasons")} for r in rejected]
    return decide(ctx, "ask_route", {"question": question},
                  Question.choice("How should this analytics question be answered?", ASK_ROUTES, default="generate"), facts)


def clarify(ctx: Any, question: str) -> dict[str, Any]:
    """`clarify_needed` (authority `escalate_only`): ask the user when the question names nothing to
    measure; a model may only add a question, never remove one."""
    from analystos.decisions.types import Question
    from analystos.registries.verified_queries import tokens

    missing = [] if tokens(question) else ["what to measure"]
    q = Question.escalation("Is this analytics question too ambiguous to answer without asking the user?",
                            levels=["answer", "clarify"], baseline="answer", escalate_to="clarify", escalate_at=0.7)
    return {**decide(ctx, "clarify_needed", {"question": question}, q, {"missing_inputs": missing}), "missing_inputs": missing}

def ask(ctx: RunContext, question: str, *, max_repairs: int = 2, parameters: dict[str, Any] | None = None,
        use_registry: bool = True) -> dict[str, Any]:
    """NL question -> governed SQL -> result. Tool-first: `ask_route` sends a registry match to the
    verified query (or declines for a missing parameter) before any model is asked; `clarify_needed`
    may stop an ambiguous question before generation. The decisions choose a path only: the SQL
    still goes through the gateway validator and scope. Repairs use the gateway's rejection message.
    Gate and budget are checked before any model call, and the budget again before every attempt."""
    _stage(ctx, "scope", "Checking what you are allowed to see")
    _authorize_ask(ctx)
    _check_budget(ctx)
    hit, rejected = None, []
    if use_registry:
        _stage(ctx, "registry", "Looking for a verified answer to this question")
        found = _registry_lookup(ctx, question, parameters)
        hit, rejected = found.hit, found.rejected
        if rejected and hit is None:
            _stage(ctx, "registry", f"A verified query is close but does not answer this: {rejected[0]['reasons'][0]}",
                   rejected=[r["name"] for r in rejected])
    chosen = route(ctx, question, hit, rejected)
    decisions = [chosen]
    path = chosen["value"]
    if path == "tool" or (path in ("verified_query", "decline") and hit is None):
        path = "verified_query" if hit is not None else "generate"  # nothing to run on that rung: the next one
    _stage(ctx, "route", {"verified_query": "Answering from the verified query registry",
                          "decline": "A required input is missing",
                          "generate": "No verified answer fits: writing new SQL"}[path], route=chosen["value"])
    if path in ("verified_query", "decline"):
        out = _registry_answer(ctx, question, hit)
        return {**out, "route": chosen["value"], "decisions": decisions}
    asked = clarify(ctx, question)
    decisions.append(asked)
    if asked["value"] == "clarify":
        _stage(ctx, "clarify", "The question needs more detail before it can be answered")
        return {"status": "clarify", "answered_by": None, "route": chosen["value"], "decisions": decisions, "sql": None,
                "model": None, "attempts": [], "result": None,
                "missing": [{"name": n} for n in asked["missing_inputs"]],
                "explanation": "This question is too open to answer safely. Say what to measure (a count, a rate, an "
                               "average), over which records and period, and how to group it."}
    _stage(ctx, "context", "Finding the tables that answer this")
    catalog = catalog_for_prompt(ctx, objective=question, capped=False)
    dialect = next(iter(ctx.scope.source_dialects.values()), "postgres")
    _stage(ctx, "generate", "Writing the SQL")
    generation = compile_for(ctx, "sql_generation", {"question": question, "dialect": dialect}, objective=question,
                             catalog=catalog, reference_text=question)
    data, model = llm_json(ctx, "sql_generation", "sql_generation.v1", generation, prompt_vars={"dialect": dialect},
                           validate=has_sql)
    if model in ("spend_cap_reached", "spend_counters_unavailable"):
        raise SpendCapReached("SQL generation was not sent to a model: the model spend cap is reached "
                              "(or the spend counters are unavailable)", details={"code": model})
    if has_sql(data):
        raise InvalidInput("SQL generation unavailable (no model route) — write SQL directly in the query console")
    attempts = []
    sql = str(data["sql"])
    escalated = False
    attempt = 0
    while True:
        if attempt:
            _check_budget(ctx)  # every attempt counts; over budget ends the loop (no repair)
        try:
            _stage(ctx, "execute", "Running it through the query gateway (read-only, within your access)")
            result = ctx.services.gateway.execute(ctx.scope, sql, actor=ask_actor(ctx.user.id), purpose=ASK_PURPOSE,
                                                  run_id=None, task_id=None)
            return {"status": "answered", "answered_by": "model", "sql": sql, "explanation": data.get("explanation"),
                    "chart": data.get("chart"), "model": model, "attempts": attempts, "result": _result(result),
                    "route": chosen["value"], "decisions": decisions}
        except (SQLRejected, AnalystOSError) as exc:
            attempts.append({"sql": sql, "error": exc.message})
            if attempt >= max_repairs:
                if escalated or not isinstance(exc, SQLRejected):
                    raise
                # Cheap first, escalate: the gateway still rejects the small tier's SQL after its repairs,
                # so the large tier writes it once more. The gateway validates that answer like any other.
                escalated = True
                better, better_model = llm_json(ctx, "sql_generation", "sql_generation.v1", generation,
                                                prompt_vars={"dialect": dialect}, validate=has_sql,
                                                escalate=f"sql_rejected after {attempt} repairs: {exc.message}"[:200],
                                                escalated_from=model)
                if has_sql(better):
                    raise
                _stage(ctx, "escalate", f"The gateway still refused the SQL ({exc.message[:120]}); a stronger model rewrites it",
                       model=better_model)
                data, model, sql = better, better_model, str(better["sql"])
                attempt += 1
                continue
            _stage(ctx, "repair", f"The gateway refused the SQL ({exc.message[:120]}); repairing it")
            fix, _ = llm_json(ctx, "sql_repair", "sql_repair.v1",
                              compile_for(ctx, "sql_repair", {"question": question, "dialect": dialect, "sql": sql,
                                                              "error": exc.message}, objective=question, catalog=catalog,
                                          reference_text=f"{sql}\n{exc.message}"), validate=has_sql)
            if has_sql(fix):
                raise
            sql = str(fix["sql"])
            attempt += 1


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
