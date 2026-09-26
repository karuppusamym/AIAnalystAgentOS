"""Transformation recipes (ADR-0023, P6-04..P6-07): save, publish, compile, preview and run.

A run: validate the stored IR again (never trusted from storage) -> plan (pushdown through the
gateway, or snapshot + DuckDB on the compute queue, with the reason) -> join pre-flight (refuse a
violated cardinality) -> each output with its gate flags -> gates and schema policy -> the staging
loader writes the output (atomic swap) or keeps the last good one and quarantines the candidate ->
executed column lineage from the IR, OpenLineage events, lineage edges.

Outputs and quarantine land in the workspace's managed recipe-output source (kind `recipe`, staged,
one per workspace), so they are read like any staged asset: through `QueryGateway.execute` under the
workspace reader role. Column tags (pii, restricted, sensitive) follow the column lineage onto the
output columns, so a recipe cannot launder a PII column past the workspace policy.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pyarrow as pa
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.connectors.naming import sanitize_identifier, staging_schema_for
from analystos.contracts.recipe import Column, RecipeInvalid, canonical_type, same_type, validate_recipe
from analystos.core.config import get_settings
from analystos.core.errors import AnalystOSError, Conflict, InvalidInput, NotFound
from analystos.core.ids import new_id, utcnow
from analystos.db.base import session_scope
from analystos.db.models import RecipeRun, RecipeVersion, Source, SourceAsset, SourceColumn, User, Workspace
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import load_in_workspace, require_role, resolve_scope, scoped_loader

OUTPUT_SOURCE_KIND = "recipe"
QUARANTINE_SUFFIX = "_quarantine"
RUN_COLUMN, REASON_COLUMN = "aos_run_id", "aos_reason"
PREVIEW_ROWS = 50
PROPAGATED_TAGS = {"pii", "restricted", "sensitive"}


# ------------------------------------------------------------------------------------ versions
def save_recipe(session: Session, user: User, workspace_id: str, spec: dict[str, Any]) -> RecipeVersion:
    """Validate and store a recipe as a new draft version; unchanged content returns the latest version."""
    require_role(session, user, workspace_id, "editor")
    validated = validate_recipe(spec)
    name = validated.recipe.name
    latest = session.scalar(select(RecipeVersion).where(RecipeVersion.workspace_id == workspace_id,
                                                        RecipeVersion.name == name)
                            .order_by(RecipeVersion.version.desc()).limit(1).with_for_update())
    if latest is not None and latest.spec_hash == validated.hash:
        return latest
    row = RecipeVersion(id=new_id("rcp"), workspace_id=workspace_id, name=name, version=(latest.version + 1) if latest else 1,
                        status="draft", spec=validated.stamped(), spec_hash=validated.hash, created_by=user.id)
    session.add(row)
    session.flush()
    audit(f"user:{user.id}", "recipe.saved", workspace_id=workspace_id, target=row.id,
          details={"name": name, "version": row.version, "spec_hash": row.spec_hash}, session=session)
    emit(workspace_id, "recipe.saved", {"recipe_id": row.id, "name": name, "version": row.version},
         actor=f"user:{user.id}", session=session)
    return row


@scoped_loader
def get_recipe(session: Session, user: User, recipe_id: str, workspace_id: str | None = None,
               minimum: str = "viewer") -> RecipeVersion:
    return load_in_workspace(session, RecipeVersion, recipe_id, workspace_id, user=user, minimum=minimum, label="recipe")


@scoped_loader
def publish_recipe(session: Session, user: User, recipe_id: str, workspace_id: str | None = None) -> RecipeVersion:
    """Publish a version (ADR-0021): schedules run published versions; the previous published one is superseded."""
    row = get_recipe(session, user, recipe_id, workspace_id, minimum="editor")
    validate_recipe(row.spec)
    for other in session.scalars(select(RecipeVersion).where(RecipeVersion.workspace_id == row.workspace_id,
                                                             RecipeVersion.name == row.name,
                                                             RecipeVersion.status == "published",
                                                             RecipeVersion.id != row.id)):
        other.status = "superseded"
    row.status, row.published_at, row.published_by = "published", utcnow(), user.id
    audit(f"user:{user.id}", "recipe.published", workspace_id=row.workspace_id, target=row.id,
          details={"name": row.name, "version": row.version, "spec_hash": row.spec_hash}, session=session)
    emit(row.workspace_id, "recipe.published", {"recipe_id": row.id, "name": row.name, "version": row.version},
         actor=f"user:{user.id}", session=session)
    return row


def list_recipes(session: Session, user: User, workspace_id: str) -> list[RecipeVersion]:
    require_role(session, user, workspace_id, "viewer")
    return list(session.scalars(select(RecipeVersion).where(RecipeVersion.workspace_id == workspace_id)
                                .order_by(RecipeVersion.name, RecipeVersion.version.desc())))


def recipe_view(row: RecipeVersion) -> dict[str, Any]:
    return {"id": row.id, "workspace_id": row.workspace_id, "name": row.name, "version": row.version, "status": row.status,
            "spec": row.spec, "spec_hash": row.spec_hash, "created_by": row.created_by, "created_at": row.created_at,
            "published_at": row.published_at}


@scoped_loader
def compile_recipe(session: Session, user: User, recipe_id: str, workspace_id: str | None = None, *,
                   target: str = "sql", dialect: str | None = None) -> dict[str, Any]:
    """The compiled form: `sql` (in the dialect the planner would push down to, or `dialect`), `duckdb`, or
    `dbt` (a project that passes the BuildGateway's static guard)."""
    from analystos.recipes.compiler import compile_outputs
    from analystos.recipes.execute import plan_execution

    row = get_recipe(session, user, recipe_id, workspace_id, minimum="analyst")
    validated = validate_recipe(row.spec)
    if target == "dbt":
        from analystos.recipes.dbt import emit_project

        return {"target": "dbt", **emit_project(validated)}
    if target not in ("sql", "duckdb"):
        raise InvalidInput("target must be sql, duckdb or dbt")
    plan = None
    if target == "sql" and dialect is None:
        plan = plan_execution(validated, resolve_scope(session, user, row.workspace_id, minimum_role="analyst"))
        dialect = plan.dialect
    dialect = "duckdb" if target == "duckdb" else dialect
    return {"target": target, "dialect": dialect, "plan": plan.to_dict() if plan else None,
            "sql": compile_outputs(validated, dialect or "postgres", pretty=True)}


# ------------------------------------------------------------------------------------ output source
def ensure_output_source(session: Session, workspace_id: str) -> Source:
    """The workspace's managed source holding recipe outputs (created once, under a workspace row lock)."""
    session.get(Workspace, workspace_id, with_for_update=True)
    src = session.scalar(select(Source).where(Source.workspace_id == workspace_id, Source.kind == OUTPUT_SOURCE_KIND))
    if src is None:
        sid = new_id("src")
        src = Source(id=sid, workspace_id=workspace_id, kind=OUTPUT_SOURCE_KIND, name="Recipe outputs",
                     config={"managed": True}, status="ready", execution_mode="staged",
                     staging_schema=staging_schema_for(sid), last_discovered_at=utcnow())
        session.add(src)
        session.flush()
    return src


def _arrow_type(ctype: str) -> pa.DataType:
    if ctype.startswith("numeric"):
        p, s = (ctype[len("numeric("):-1].split(",") if "(" in ctype else ("38", "9"))
        return pa.decimal128(int(p), int(s))
    return {"text": pa.string(), "smallint": pa.int16(), "integer": pa.int32(), "bigint": pa.int64(), "double": pa.float64(),
            "boolean": pa.bool_(), "date": pa.date32(), "timestamp": pa.timestamp("us"),
            "timestamptz": pa.timestamp("us", tz="UTC")}[ctype]


def _value(v: Any, ctype: str) -> Any:
    """A JSON-safe value (as the gateway and the DuckDB engine return them) in the declared type."""
    if v is None:
        return None
    if ctype == "text":
        return v if isinstance(v, str) else str(v)
    if ctype in ("smallint", "integer", "bigint"):
        return int(v)
    if ctype == "double":
        return float(v)
    if ctype.startswith("numeric"):
        scale = int(ctype[len("numeric("):-1].split(",")[1]) if "(" in ctype else 9
        return Decimal(str(v)).quantize(Decimal(1).scaleb(-scale))
    if ctype == "boolean":
        return bool(v)
    if ctype == "date":
        return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])
    if ctype in ("timestamp", "timestamptz"):
        return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return v


def _batches(columns: list[Column], rows: list[list[Any]], extra: dict[str, list[Any]] | None = None) -> list[pa.RecordBatch]:
    arrays, names = [], []
    for i, c in enumerate(columns):
        arrays.append(pa.array([_value(r[i], c.type) for r in rows], type=_arrow_type(c.type)))
        names.append(c.name)
    for name, values in (extra or {}).items():
        arrays.append(pa.array(values, type=pa.string()))
        names.append(name)
    return pa.Table.from_arrays(arrays, names=names).to_batches(max_chunksize=50_000) or [
        pa.RecordBatch.from_arrays(arrays, names=names)]


def _register_asset(session: Session, src: Source, info: dict[str, Any], *, recipe: str, output: str, role: str,
                    tags: dict[str, list[str]]) -> SourceAsset:
    schema, table = info["schema"], info["table"]
    asset = session.scalar(select(SourceAsset).where(SourceAsset.source_id == src.id, SourceAsset.schema_name == schema,
                                                     SourceAsset.name == table))
    if asset is None:
        asset = SourceAsset(id=new_id("ast"), source_id=src.id, workspace_id=src.workspace_id, schema_name=schema, name=table,
                            source_name=table, kind="table", selected=True, stats={})
        session.add(asset)
        session.flush()
    asset.selected, asset.lifecycle, asset.row_count, asset.freshness_at = True, "active", info["row_count"], utcnow()
    asset.stats = {**(asset.stats or {}), "recipe": recipe, "output": output, "role": role}
    asset.description = f"{'Quarantine of' if role == 'quarantine' else 'Output'} {output} of recipe {recipe}"
    asset.snapshot = {"sampling_method": "full", "rows_staged": info["row_count"], "truncated": False,
                      "source_total_rows": info["row_count"], "total_rows_basis": "count", "row_cap": None,
                      "staged_at": utcnow().isoformat(), "load_id": new_id("load"),
                      "content_fingerprint": info["content_fingerprint"]}
    existing = {c.name: c for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id == asset.id))}
    wanted = [c["name"] for c in info["columns"]]
    for name, col in existing.items():
        if name not in wanted:
            session.delete(col)
    for i, c in enumerate(info["columns"]):
        col = existing.get(c["name"])
        if col is None:
            col = SourceColumn(asset_id=asset.id, name=c["name"], tags=[], profile={}, semantics={})
            session.add(col)
        col.ordinal, col.data_type = i, c["type"]
        # tags only tighten: the lineage's tags are added, a person's tags stay
        col.tags = sorted(set(col.tags or []) | set(tags.get(c["name"], [])))
    session.flush()
    return asset


def _source_tags(session: Session, workspace_id: str, assets: list[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for fq in assets:
        schema, name = fq.split(".", 1)
        for col_name, tags in session.execute(
                select(SourceColumn.name, SourceColumn.tags).join(SourceAsset, SourceAsset.id == SourceColumn.asset_id)
                .where(SourceAsset.workspace_id == workspace_id, SourceAsset.schema_name == schema, SourceAsset.name == name)):
            out[f"{fq}.{col_name}"] = set(tags or []) & PROPAGATED_TAGS
    return out


def _upstream_drift(session: Session, workspace_id: str, validated: Any) -> list[dict[str, Any]]:
    """Declared source types that differ from the catalog's (an upstream type change)."""
    changes = []
    for node in validated.sources():
        schema, name = node.asset.split(".", 1)
        catalog = dict(session.execute(
            select(SourceColumn.name, SourceColumn.data_type).join(SourceAsset, SourceAsset.id == SourceColumn.asset_id)
            .where(SourceAsset.workspace_id == workspace_id, SourceAsset.schema_name == schema,
                   SourceAsset.name == name)).all())
        for c in node.output_schema or []:
            now = canonical_type(catalog.get(c.name))
            if now is not None and not (same_type(c.type, now) or same_type(now, c.type)):
                changes.append({"change": "type_changed", "asset": node.asset, "column": c.name, "from": c.type, "to": now})
    return changes


# ------------------------------------------------------------------------------------ runs
@scoped_loader
def get_run(session: Session, user: User, run_id: str, workspace_id: str | None = None) -> RecipeRun:
    return load_in_workspace(session, RecipeRun, run_id, workspace_id, user=user, minimum="viewer", label="recipe run")


def run_view(row: RecipeRun, *, with_events: bool = False) -> dict[str, Any]:
    out = {k: getattr(row, k) for k in ("id", "workspace_id", "recipe_id", "recipe_name", "recipe_version", "spec_hash",
                                        "mode", "engine", "status", "plan", "preflight", "snapshots", "gates",
                                        "schema_changes", "outputs", "lineage", "query_ids", "error", "created_by",
                                        "created_at", "finished_at")}
    if with_events:
        out["openlineage"] = row.openlineage
    return out


def _finish(run_id: str, status: str, **fields: Any) -> dict[str, Any]:
    with session_scope() as s:
        row = s.get(RecipeRun, run_id)
        for k, v in fields.items():
            setattr(row, k, v)
        row.status, row.finished_at = status, utcnow()
        event = {"succeeded": "recipe.run.completed", "blocked": "recipe.run.blocked", "refused": "recipe.run.refused"}.get(
            status, "recipe.run.failed")
        emit(row.workspace_id, event, {"recipe_run_id": row.id, "recipe_id": row.recipe_id, "status": status,
                                       "mode": row.mode, "engine": row.engine, "error": (row.error or "")[:300] or None},
             actor=row.created_by, session=s)
        return run_view(row)


@scoped_loader
def run_recipe(user: User, recipe_id: str, workspace_id: str | None = None, *, mode: str = "materialize",
               engine: str | None = None, limit: int = PREVIEW_ROWS) -> dict[str, Any]:
    """Run a recipe version. `preview` returns rows and gate results and writes nothing; `materialize`
    writes each output (or keeps the last good one and quarantines the candidate)."""
    from analystos.artifacts.registry import link
    from analystos.recipes.execute import RecipeExecutor, default_store, plan_execution
    from analystos.runtime.context import default_gateway
    from analystos.workflows.orchestrator import run_recipe_compute

    if mode not in ("preview", "materialize"):
        raise InvalidInput("mode must be preview or materialize")
    settings = get_settings()
    with session_scope() as s:
        row = get_recipe(s, user, recipe_id, workspace_id, minimum="editor" if mode == "materialize" else "analyst")
        ws, name, version, spec = row.workspace_id, row.name, row.version, dict(row.spec)
        validated = validate_recipe(spec)
        scope = resolve_scope(s, user, ws, minimum_role="analyst")
        upstream = _upstream_drift(s, ws, validated)
        # The last good output of each output name: the newest materialized run that wrote it.
        previous_outputs: dict[str, Any] = {}
        for prev in s.scalars(select(RecipeRun).where(RecipeRun.workspace_id == ws, RecipeRun.recipe_name == name,
                                                      RecipeRun.mode == "materialize",
                                                      RecipeRun.status.in_(("succeeded", "blocked")))
                              .order_by(RecipeRun.created_at.desc()).limit(50)):
            for out_name, entry in (prev.outputs or {}).items():
                if out_name not in previous_outputs and not entry.get("blocked") and entry.get("table"):
                    previous_outputs[out_name] = entry
        run = RecipeRun(id=new_id("rrun"), workspace_id=ws, recipe_id=row.id, recipe_name=name, recipe_version=version,
                        spec_hash=validated.hash, mode=mode, status="running", plan={}, preflight=[], snapshots={}, gates={},
                        schema_changes={}, outputs={}, lineage=[], openlineage=[], query_ids=[], created_by=f"user:{user.id}")
        s.add(run)
        s.flush()
        link(s, ws, ("recipe", row.id), "ran", ("recipe_run", run.id))
        emit(ws, "recipe.run.started", {"recipe_run_id": run.id, "recipe_id": row.id, "mode": mode},
             actor=f"user:{user.id}", session=s)
        run_id = run.id
    started = utcnow()
    executor = None
    try:
        plan = plan_execution(validated, scope, prefer=engine)
        executor = RecipeExecutor(default_gateway(), scope, validated, plan, actor=f"user:{user.id}",
                                  store=default_store(settings), compute=run_recipe_compute)
        with session_scope() as s:
            r = s.get(RecipeRun, run_id)
            r.plan, r.engine = plan.to_dict(), plan.engine
        needed = {nid for o in validated.outputs() for nid in validated.ancestors(o.id)}
        preflight = [executor.preflight(n.id) for n in validated.recipe.nodes if n.op == "join" and n.id in needed]
        violated = [p for p in preflight if not p["ok"]]
        if violated:
            v = violated[0]
            message = (f"join {v['join']} is declared {v['declared']} but the data is {v['observed']} (left keys "
                       f"{v['left_keys']}/{v['left_key_rows']} rows, right keys {v['right_keys']}/{v['right_key_rows']} rows, "
                       f"row multiplication {v['row_multiplication']}); the recipe was not run")
            _finish(run_id, "refused", preflight=preflight, error=message, query_ids=executor.query_ids,
                    snapshots=executor.snapshots)
            raise InvalidInput(message, details={"recipe_run_id": run_id, "preflight": violated})
        result = _run_outputs(user, run_id, ws, name, version, validated, executor, plan, mode=mode, limit=limit,
                              upstream=upstream, previous_outputs=previous_outputs, started=started, preflight=preflight)
        return result
    except RecipeInvalid:
        raise
    except AnalystOSError as exc:
        with session_scope() as s:
            status = s.get(RecipeRun, run_id).status
        if status == "running":
            _finish(run_id, "failed", error=exc.message[:2000], query_ids=executor.query_ids if executor else [])
        raise


def _run_outputs(user: User, run_id: str, ws: str, name: str, version: int, validated: Any, executor: Any, plan: Any, *,
                 mode: str, limit: int, upstream: list[dict[str, Any]], previous_outputs: dict[str, Any],
                 preflight: list[dict[str, Any]], started: datetime) -> dict[str, Any]:
    from analystos.evidence.lineage.sql import redact_literals
    from analystos.recipes.gates import evaluate, row_gates, schema_policy
    from analystos.recipes.lineage import column_lineage, openlineage_events
    from analystos.staging.loader import MAX_TABLE_NAME, StagingLoader

    lineage = column_lineage(validated)
    gates_out: dict[str, Any] = {}
    schema_out: dict[str, Any] = {}
    outputs: dict[str, Any] = {}
    preview: dict[str, Any] = {}
    blocked_any = False
    compiled: dict[str, str] = {}
    for out in validated.outputs():
        gates = row_gates(out)
        tree = executor.compiler.output_query(out.id, gates=gates)
        compiled[out.name] = redact_literals(executor.compiler.sql(tree), plan.dialect)
        res = executor.query(tree, purpose=f"recipe.{mode}:{run_id}")
        if res.truncated:
            message = (f"output {out.name} has more than {executor.max_rows} rows, the row cap of this workspace; a partial "
                       "output is never written. Aggregate further or raise the workspace limit.")
            if mode == "materialize":
                raise InvalidInput(message)
        prev = previous_outputs.get(out.name) or {}
        outcome = evaluate(out, res.columns, res.rows, previous_rows=prev.get("row_count"))
        policy = schema_policy(out.schema_policy, out.output_schema or [], prev.get("columns"), upstream)
        blocked = outcome.blocked or policy["action"] == "block"
        blocked_any = blocked_any or blocked
        gates_out[out.name] = outcome.summary()
        schema_out[out.name] = policy
        if mode == "preview":
            preview[out.name] = {"columns": outcome.columns, "rows": outcome.kept[:limit], "row_count": len(outcome.kept),
                                 "truncated": res.truncated, "dropped_rows": len(outcome.dropped),
                                 "would_block": blocked}
            continue
        outputs[out.name] = _materialize(user, run_id, ws, name, out, outcome, blocked, previous=prev,
                                         lineage=next(entry for entry in lineage if entry["output"] == out.name),
                                         loader=StagingLoader(get_settings()), max_table=MAX_TABLE_NAME)
    status = "blocked" if blocked_any and mode == "materialize" else "succeeded"
    sources = dict(plan.sources)
    targets = {n: {"namespace": f"analystos://source/{o['source_id']}", "name": o.get("table") or n,
                   "columns": o.get("columns") or []} for n, o in outputs.items()}
    events = openlineage_events(workspace_id=ws, recipe_name=name, recipe_version=version, recipe_run_id=run_id,
                                lineage=lineage, sources=sources, outputs=targets, sql=compiled, dialect=plan.dialect,
                                started_at=started, finished_at=utcnow(), status=status,
                                error="a fail gate or the strict schema policy blocked the output" if status == "blocked" else None)
    view = _finish(run_id, status, preflight=preflight, gates=gates_out, schema_changes=schema_out, outputs=outputs,
                   lineage=lineage, openlineage=events, query_ids=executor.query_ids,
                   snapshots={a: {k: v for k, v in s.items() if k != "columns"} for a, s in executor.snapshots.items()})
    if mode == "preview":
        view["preview"] = preview
    return view


def _materialize(user: User, run_id: str, ws: str, recipe: str, out: Any, outcome: Any, blocked: bool, *,
                 previous: dict[str, Any], lineage: dict[str, Any], loader: Any, max_table: int) -> dict[str, Any]:
    """Write one output through the loader, or keep the last good output and quarantine the candidate."""
    from analystos.artifacts.registry import link

    declared = list(out.output_schema or [])
    table = sanitize_identifier(f"{recipe}_{out.name}", max_length=max_table - len(QUARANTINE_SUFFIX), fallback="output")
    q_table = f"{table}{QUARANTINE_SUFFIX}"
    with session_scope() as s:
        src = ensure_output_source(s, ws)
        source_id = src.id
        owner = s.scalar(select(SourceAsset).where(SourceAsset.source_id == source_id, SourceAsset.name == table))
        if owner is not None and (owner.stats or {}).get("recipe") not in (None, recipe):
            raise Conflict(f"output table {table} belongs to recipe {(owner.stats or {}).get('recipe')}; rename the output")
        tags_by_source = _source_tags(s, ws, sorted({r["asset"] for refs in lineage["columns"].values() for r in refs}))
    tags = {col: sorted(set().union(*[tags_by_source.get(f"{r['asset']}.{r['column']}", set()) for r in refs]))
            for col, refs in lineage["columns"].items()}
    result: dict[str, Any] = {"source_id": source_id, "blocked": blocked, "kept_rows": len(outcome.kept),
                              "dropped_rows": len(outcome.dropped), "columns": [c.model_dump() for c in declared]}
    quarantine_rows: list[list[Any]] = []
    reasons: list[str] = []
    if blocked:
        failed = [g["gate"] for g in outcome.results if g["status"] == "failed"] or ["schema_policy"]
        quarantine_rows = [*outcome.kept, *[r for r, _ in outcome.dropped]]
        reasons = ["candidate: " + ", ".join(failed)] * len(outcome.kept) + [
            "drop: " + ", ".join(why) for _, why in outcome.dropped]
        result.update(table=previous.get("table"), kept_previous=bool(previous.get("table")),
                      row_count=previous.get("row_count"), content_fingerprint=previous.get("content_fingerprint"))
    else:
        info = loader.load(source_id, table, _batches(declared, outcome.kept), workspace_id=ws, fingerprint="table")
        result.update(table=f"{info['schema']}.{info['table']}", row_count=info["row_count"],
                      content_fingerprint=info["content_fingerprint"], kept_previous=False)
        with session_scope() as s:
            src = s.get(Source, source_id)
            _register_asset(s, src, info, recipe=recipe, output=out.name, role="output", tags=tags)
            src.last_discovered_at = utcnow()  # bumps the gateway cache's source version
        if outcome.dropped:
            quarantine_rows = [r for r, _ in outcome.dropped]
            reasons = ["drop: " + ", ".join(why) for _, why in outcome.dropped]
    if quarantine_rows:
        extra = {RUN_COLUMN: [run_id] * len(quarantine_rows), REASON_COLUMN: reasons}
        batches = _batches(declared, quarantine_rows, extra)
        try:
            qinfo = loader.load(source_id, q_table, batches, workspace_id=ws, mode="append", fingerprint="table")
        except InvalidInput:  # the output's shape changed since the last quarantine: start a new one
            qinfo = loader.load(source_id, q_table, _batches(declared, quarantine_rows, extra), workspace_id=ws,
                                fingerprint="table")
        with session_scope() as s:
            src = s.get(Source, source_id)
            _register_asset(s, src, qinfo, recipe=recipe, output=out.name, role="quarantine",
                            tags={**tags, RUN_COLUMN: [], REASON_COLUMN: []})
            src.last_discovered_at = utcnow()
        result.update(quarantine=f"{qinfo['schema']}.{qinfo['table']}", quarantined_rows=len(quarantine_rows))
    with session_scope() as s:
        if result.get("table") and not blocked:
            link(s, ws, ("recipe_run", run_id), "produced", ("table", result["table"]))
            for asset in sorted({r["asset"] for refs in lineage["columns"].values() for r in refs} |
                                {r["asset"] for r in lineage["dataset"]}):
                link(s, ws, ("table", asset), "transformed_into", ("table", result["table"]))
        if result.get("quarantine"):
            link(s, ws, ("recipe_run", run_id), "quarantined", ("table", result["quarantine"]))
        audit(f"user:{user.id}", "recipe.output.blocked" if blocked else "recipe.output.written", workspace_id=ws,
              target=result.get("table") or table,
              details={k: result.get(k) for k in ("row_count", "dropped_rows", "quarantined_rows", "kept_previous")},
              session=s)
    return result


@scoped_loader
def quarantine_rows(user: User, run_id: str, workspace_id: str | None = None, *, output: str,
                    limit: int = 200) -> dict[str, Any]:
    """The rows a run quarantined for one output, read through the gateway (never the loader identity)."""
    from sqlglot import exp

    from analystos.runtime.context import default_gateway

    with session_scope() as s:
        run = get_run(s, user, run_id, workspace_id)
        entry = (run.outputs or {}).get(output)
        if not entry or not entry.get("quarantine"):
            raise NotFound(f"run {run_id} quarantined nothing for output {output}")
        scope = resolve_scope(s, s.merge(user), run.workspace_id, minimum_role="analyst")
        source_id, fq = entry["source_id"], entry["quarantine"]
    schema, table = fq.split(".", 1)
    stmt = exp.select("*").from_(exp.Table(this=exp.to_identifier(table, quoted=True), db=exp.to_identifier(schema, quoted=True))) \
        .where(exp.EQ(this=exp.column(RUN_COLUMN, quoted=True), expression=exp.Literal.string(run_id)))
    runner = default_gateway().run_sql_for(scope, actor=f"user:{user.id}", source_id=source_id)
    res = runner(stmt.sql(dialect="postgres"), purpose=f"recipe.quarantine:{run_id}", max_rows=min(max(limit, 1), 5000))
    count = runner(exp.select(exp.Count(this=exp.Star()).as_("n")).from_(
        exp.Table(this=exp.to_identifier(table, quoted=True), db=exp.to_identifier(schema, quoted=True)))
        .where(exp.EQ(this=exp.column(RUN_COLUMN, quoted=True), expression=exp.Literal.string(run_id)))
        .sql(dialect="postgres"), purpose=f"recipe.quarantine:{run_id}", max_rows=1)
    return {"output": output, "table": fq, "columns": res.columns, "rows": res.rows,
            "row_count": int(count.rows[0][0]) if count.rows else len(res.rows), "truncated": res.truncated,
            "query_id": res.query_id}


def recipe_runs(session: Session, user: User, workspace_id: str, recipe_name: str | None = None) -> list[RecipeRun]:
    require_role(session, user, workspace_id, "viewer")
    stmt = select(RecipeRun).where(RecipeRun.workspace_id == workspace_id)
    if recipe_name:
        stmt = stmt.where(RecipeRun.recipe_name == recipe_name)
    return list(session.scalars(stmt.order_by(RecipeRun.created_at.desc()).limit(200)))
