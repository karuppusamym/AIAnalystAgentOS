"""Data-model suggestion (Stream A): what deterministic code can say about the selected tables as a model,
measured before anyone relies on it, and turned into a proposal only through the existing review paths.

* `suggest`  reads what the platform already knows (catalog semantics, profiles, measured key checks, legacy
  relationships, queued and accepted relationship candidates, the approved structure) and proposes entities,
  primary keys, time columns, measures/dimensions, relationships, candidate metrics and star schemas, with the
  issues that stop a table from being modelled safely. No query runs and no model is asked.
* `validate` measures the suggestion through the gateway as the caller, bounded (MAX_QUERIES statements):
  primary-key uniqueness per table (rows vs distinct key tuples) and join fan-out per relationship (the
  referencing table's rows before and after a LEFT JOIN on the key). Results are persisted as evidence
  (`SourceAsset.stats.key_check`, `Relationship.evidence.fanout_check`, candidate evidence) so the next
  `suggest` shows them.
* `propose` turns the suggestion into a `proposed` structure version through `review.propose_structure` (entities,
  primary keys, grain) and queues unmeasured relationships through `review.propose_candidate` (measured before
  they are queued). Approval stays with another person (separation of duties); candidate metrics are never
  proposed or approved here.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.errors import AnalystOSError, Conflict
from analystos.core.ids import utcnow
from analystos.db.models import Relationship, SemanticRelationshipCandidate, SourceAsset, SourceColumn, User
from analystos.skills.profiling import column_is_sensitive

MAX_QUERIES = 40
FACT_ROLES = {"fact", "event"}
DIMENSION_ROLES = {"dimension", "reference"}
MEASURE_ROLES = {"measure", "amount", "percent", "duration"}
DIMENSION_COLUMN_ROLES = {"dimension", "code", "name", "geo", "flag"}
TIME_ROLES = {"timestamp", "date"}
_NAME = re.compile(r"[^A-Za-z0-9_]+")


def _fq(a: SourceAsset) -> str:
    return f"{a.schema_name}.{a.name}"


def _ident(text: str) -> str:
    s = _NAME.sub("_", text).strip("_").lower() or "x"
    return s if not s[0].isdigit() else f"m_{s}"


def _load(session: Session, workspace_id: str) -> tuple[list[SourceAsset], dict[str, list[SourceColumn]]]:
    assets = list(session.scalars(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id,
                                                            SourceAsset.selected.is_(True), SourceAsset.lifecycle == "active")
                                  .order_by(SourceAsset.schema_name, SourceAsset.name)))
    cols: dict[str, list[SourceColumn]] = defaultdict(list)
    if assets:
        for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id.in_([a.id for a in assets]))
                                 .order_by(SourceColumn.asset_id, SourceColumn.ordinal)):
            cols[c.asset_id].append(c)
    return assets, cols


def _approved_keys(session: Session, workspace_id: str) -> dict[str, list[str]]:
    from analystos.semantic.review import approved_model

    model = approved_model(session, workspace_id)
    out: dict[str, list[str]] = {}
    for d in (model.datasets if model else []) or []:
        src = str(d.get("source") or "").strip().lower()
        if d.get("primary_key") and " " not in src:
            out[src] = list(d["primary_key"])
    return out


def primary_key(asset: SourceAsset, cols: list[SourceColumn], approved: list[str] | None) -> dict[str, Any]:
    """The strongest key evidence for one table: approved model > declared > measured > profiled > none.
    `unique` is the measured (or profiled) answer for exactly those columns, None when nothing measured them."""
    stats = asset.stats or {}
    check = stats.get("key_check") if isinstance(stats.get("key_check"), dict) else None
    names = {c.name for c in cols}

    def uniqueness(columns: list[str]) -> bool | None:
        if check and sorted(check.get("columns") or []) == sorted(columns) and check.get("unique") is not None:
            return bool(check["unique"])
        for k in stats.get("candidate_keys") or []:
            if isinstance(k, dict) and sorted(k.get("columns") or []) == sorted(columns) and k.get("unique") is not None:
                return bool(k["unique"])
        return None

    if approved and set(approved) <= names:
        return {"columns": approved, "evidence": "approved", "unique": uniqueness(approved)}
    declared = [c.name for c in cols if c.is_key]
    if declared:
        return {"columns": declared, "evidence": "declared", "unique": uniqueness(declared)}
    if check and check.get("unique") is True and set(check.get("columns") or []) <= names:
        return {"columns": list(check["columns"]), "evidence": "measured_unique", "unique": True}
    for k in stats.get("candidate_keys") or []:
        if isinstance(k, dict) and k.get("unique") is True and k.get("columns") and set(k["columns"]) <= names:
            return {"columns": list(k["columns"]), "evidence": "profile_unique", "unique": True}
    return {"columns": [], "evidence": "none", "unique": None}


def _time_column(cols: list[SourceColumn]) -> str | None:
    """The best-covered date/time column (never a sensitive one), first by ordinal on a tie."""
    best: tuple[float, int, str] | None = None
    for c in cols:
        role = (c.semantics or {}).get("semantic_role")
        if (role not in TIME_ROLES and c.semantic_type != "datetime") or column_is_sensitive(c.tags, c.semantics):
            continue
        rate = (c.profile or {}).get("null_rate")
        coverage = 1.0 - float(rate) if isinstance(rate, int | float) else 0.5
        cand = (-coverage, c.ordinal, c.name)
        best = cand if best is None or cand < best else best
    return best[2] if best else None


def _table(asset: SourceAsset, cols: list[SourceColumn], approved: list[str] | None) -> dict[str, Any]:
    sem = asset.semantics or {}
    role = str(sem.get("role") or "unknown")
    pk = primary_key(asset, cols, approved)
    fks = {c.name for c in cols if (c.semantics or {}).get("semantic_role") == "foreign_key" or (c.profile or {}).get("references")}
    usable = [c for c in cols if not column_is_sensitive(c.tags, c.semantics)]
    measures = [c.name for c in usable if (c.semantics or {}).get("semantic_role") in MEASURE_ROLES
                and c.name not in fks and not c.is_key]
    dimensions = [c.name for c in usable if (c.semantics or {}).get("semantic_role") in DIMENSION_COLUMN_ROLES
                  and c.name not in fks]
    time_column = _time_column(cols)
    issues = []
    if pk["evidence"] == "none":
        issues.append({"code": "no_primary_key", "message": "No unique key: none is declared, measured or found in the profile"})
    elif pk["unique"] is False:
        issues.append({"code": "key_not_unique", "message": f"The key {', '.join(pk['columns'])} has duplicate or missing values"})
    if role in FACT_ROLES and not time_column:
        issues.append({"code": "fact_without_time", "message": "A fact table without a date or time column cannot be trended"})
    if not (asset.stats or {}).get("profile_meta"):
        issues.append({"code": "not_profiled", "message": "Not profiled yet: keys and ranges are not measured"})
    confidence = float(sem.get("confidence") or 0.0)
    if pk["unique"] is True:
        confidence = min(1.0, confidence + 0.05)
    elif pk["unique"] is False or pk["evidence"] == "none":
        confidence = max(0.0, confidence - 0.1)
    return {"asset_id": asset.id, "fq": _fq(asset), "name": asset.name, "business_name": asset.business_name,
            "role": role, "entity": sem.get("entity"), "grain": sem.get("grain"), "primary_key": pk,
            "time_column": time_column, "measures": measures, "dimensions": dimensions,
            "confidence": round(confidence, 2), "issues": issues}


def _side(asset: SourceAsset, columns: list[str]) -> dict[str, Any]:
    return {"asset_id": asset.id, "fq": _fq(asset), "columns": list(columns)}


def _clean_evidence(ev: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (ev or {}).items() if k not in ("sql",)}


def relationships(session: Session, workspace_id: str, assets: list[SourceAsset]) -> list[dict[str, Any]]:
    """Validated and proposed legacy relationships plus pending and accepted review candidates, one entry per
    column pair, strongest status first (validated > pending > proposed)."""
    by_id = {a.id: a for a in assets}
    by_fq = {_fq(a).lower(): a for a in assets}
    out: dict[tuple[str, tuple[str, ...], str, tuple[str, ...]], dict[str, Any]] = {}
    rank = {"validated": 3, "pending": 2, "proposed": 1}

    def put(key: tuple[str, tuple[str, ...], str, tuple[str, ...]], entry: dict[str, Any]) -> None:
        cur = out.get(key)
        if cur is None or rank[entry["status"]] > rank[cur["status"]]:
            if cur is not None and cur.get("candidate_id") and not entry.get("candidate_id"):
                entry["candidate_id"] = cur["candidate_id"]
            out[key] = entry

    ids = list(by_id)
    rels = session.scalars(select(Relationship).where(Relationship.workspace_id == workspace_id,
                                                      Relationship.from_asset_id.in_(ids), Relationship.to_asset_id.in_(ids))
                           .order_by(Relationship.id)) if ids else []
    for r in rels:
        f, t = by_id[r.from_asset_id], by_id[r.to_asset_id]
        ev = r.evidence or {}
        fcols = list(ev.get("from_columns") or [r.from_column])
        tcols = list(ev.get("to_columns") or [r.to_column])
        key = (f.id, tuple(fcols), t.id, tuple(tcols))
        put(key, {"from": _side(f, fcols), "to": _side(t, tcols), "cardinality": r.cardinality,
                  "status": "validated" if r.validated else "proposed", "confidence": round(float(r.confidence or 0), 3),
                  "evidence": {**_clean_evidence(ev), "origin": r.origin}, "candidate_id": ev.get("candidate_id"),
                  "relationship_id": r.id})
    for c in session.scalars(select(SemanticRelationshipCandidate).where(
            SemanticRelationshipCandidate.workspace_id == workspace_id,
            SemanticRelationshipCandidate.status.in_(("pending", "accepted")))
            .order_by(SemanticRelationshipCandidate.created_at, SemanticRelationshipCandidate.id)):
        f, t = by_fq.get(c.from_asset.lower()), by_fq.get(c.to_asset.lower())
        if f is None or t is None:
            continue
        key = (f.id, tuple(c.from_columns), t.id, tuple(c.to_columns))
        put(key, {"from": _side(f, c.from_columns), "to": _side(t, c.to_columns), "cardinality": c.cardinality,
                  "status": "validated" if c.status == "accepted" else "pending", "confidence": round(float(c.confidence or 0), 3),
                  "evidence": {**_clean_evidence(c.evidence), "containment": c.containment,
                               "assessment": (c.assessment or {}).get("outcome")}, "candidate_id": c.id,
                  "relationship_id": None})
    return sorted(out.values(), key=lambda r: (-rank[r["status"]], r["from"]["fq"], r["from"]["columns"], r["to"]["fq"]))


def candidate_metrics(tables: list[dict[str, Any]], cols: dict[str, list[SourceColumn]]) -> list[dict[str, Any]]:
    """Candidate metrics only (never proposed or approved here): a row count per fact entity, SUM of amounts and
    counts, AVG of percentages and durations."""
    out: list[dict[str, Any]] = []
    for t in tables:
        if t["role"] not in FACT_ROLES:
            continue
        entity = str(t.get("entity") or t["name"])
        out.append({"name": _ident(f"count_{entity}"), "label": f"Number of {entity} records", "expression": "COUNT(*)",
                    "table_fq": t["fq"], "reason": f"row count of the {t['role']} table ({t.get('grain') or 'one row per record'})"})
        by_name = {c.name: c for c in cols.get(t["asset_id"], [])}
        for m in t["measures"]:
            sem = by_name[m].semantics or {}
            role, unit = sem.get("semantic_role"), sem.get("unit")
            agg = "AVG" if role in ("percent", "duration") else "SUM"
            label = (sem.get("business_name") or by_name[m].business_name or m).strip()
            why = f"{role} column" + (f" in {unit}" if unit else "") + (" (averaged: a rate or duration does not add up)"
                                                                          if agg == "AVG" else " (additive)")
            out.append({"name": _ident(f"{agg.lower()}_{m}"), "label": f"{'Average' if agg == 'AVG' else 'Total'} {label}",
                        "expression": f"{agg}({m})", "table_fq": t["fq"], "reason": why})
    return out


def suggest(session: Session, workspace_id: str) -> dict[str, Any]:
    assets, cols = _load(session, workspace_id)
    approved = _approved_keys(session, workspace_id)
    tables = [_table(a, cols[a.id], approved.get(_fq(a).lower())) for a in assets]
    rels = relationships(session, workspace_id, assets)
    by_id = {t["asset_id"]: t for t in tables}
    issues: list[dict[str, Any]] = []
    def called(t: dict[str, Any]) -> str:  # the name a person knows the table by, not its staged schema
        return str(t.get("business_name") or t.get("name") or t["fq"])

    for t in tables:
        issues += [{**i, "message": f"{called(t)}: {i['message'][:1].lower()}{i['message'][1:]}", "asset_id": t["asset_id"]}
                   for i in t["issues"]]
    linked = {r["from"]["asset_id"] for r in rels} | {r["to"]["asset_id"] for r in rels}
    if len(tables) > 1:
        source_of = {a.id: a.source_id for a in assets}
        for t in tables:
            if t["asset_id"] not in linked:
                own = source_of.get(t["asset_id"])
                alone = sum(1 for x in tables if source_of.get(x["asset_id"]) == own) == 1
                message = (f"{called(t)} is the only selected table from its source; relationships stay within one source, "
                           "so combine it with the others in a recipe (Work → Prepare data)" if alone
                           else f"{called(t)} joins no other selected table")
                issues.append({"code": "orphan_table", "message": message, "asset_id": t["asset_id"]})
    pairs: dict[tuple[str, str], int] = defaultdict(int)
    for r in rels:
        pairs[tuple(sorted((r["from"]["asset_id"], r["to"]["asset_id"])))] += 1  # type: ignore[index]
        if r["cardinality"] == "many_to_many":
            issues.append({"code": "many_to_many_without_bridge",
                           "message": f"{called(by_id[r['from']['asset_id']])} and {called(by_id[r['to']['asset_id']])} join "
                                      "many-to-many: additive measures would "
                                      "be multiplied; join through a bridge table", "asset_id": r["from"]["asset_id"]})
    for (a, b), n in sorted(pairs.items()):
        if n > 1:
            issues.append({"code": "ambiguous_join", "message": f"{called(by_id[a])} and {called(by_id[b])} join on {n} different "
                                                                "column sets: choose one", "asset_id": a})
    stars = []
    for t in tables:
        if t["role"] not in FACT_ROLES:
            continue
        dims = sorted({r["to"]["asset_id"] for r in rels if r["from"]["asset_id"] == t["asset_id"]
                       and r["cardinality"] in ("many_to_one", "one_to_one")})
        dims += sorted({r["from"]["asset_id"] for r in rels if r["to"]["asset_id"] == t["asset_id"]
                        and r["cardinality"] == "one_to_many"} - set(dims))
        if dims:
            stars.append({"fact": t["asset_id"], "dimensions": dims})
    return {"generated_at": utcnow().isoformat(), "tables": tables, "relationships": rels,
            "metrics": candidate_metrics(tables, cols), "star_schemas": stars, "issues": issues,
            "summary": {"tables": len(tables), "facts": sum(1 for t in tables if t["role"] in FACT_ROLES),
                        "dimensions": sum(1 for t in tables if t["role"] in DIMENSION_ROLES),
                        "relationships_validated": sum(1 for r in rels if r["status"] == "validated"),
                        "relationships_pending": sum(1 for r in rels if r["status"] == "pending"),
                        "keys_measured": sum(1 for a in assets if isinstance((a.stats or {}).get("key_check"), dict))}}


# ------------------------------------------------------------------------------------ measured validation
class _Budget:
    def __init__(self, limit: int) -> None:
        self.limit, self.used = limit, 0

    def take(self, n: int) -> bool:
        if self.used + n > self.limit:
            return False
        self.used += n
        return True


def _count(run_sql: Any, sql: str, purpose: str) -> int:
    rows = run_sql(sql, purpose=purpose, max_rows=1).records()
    return int(next(iter(rows[0].values())) or 0) if rows else 0


KEY_SEARCH_QUERIES = 10  # statements one table's key search may use (within the validation budget)


class _Counting:
    """A gateway runner that counts its statements against the validation budget."""

    def __init__(self, inner: Any, budget: _Budget) -> None:
        self.inner, self.budget, self.dialect = inner, budget, getattr(inner, "dialect", "postgres")

    def __call__(self, sql: str, **kw: Any) -> Any:
        self.budget.used += 1
        return self.inner(sql, **kw)


def _search_key(session: Session, run_sql: Any, t: dict[str, Any], readable: Any, budget: _Budget) -> list[str]:
    """skills/relationships.discover_keys over the table's readable, non-sensitive columns, bounded by what is left
    of the budget (keeping two statements for the uniqueness check that follows)."""
    from analystos.skills.relationships import discover_keys

    left = budget.limit - budget.used - 2
    if left < 2:
        return []
    cols = [c for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id == t["asset_id"])
                                       .order_by(SourceColumn.ordinal))
            if readable(t["fq"], [c.name]) and not column_is_sensitive(c.tags, c.semantics)]
    if not cols:
        return []
    found = discover_keys(_Counting(run_sql, budget), {"asset": t["fq"], "columns": [{"name": c.name} for c in cols]},
                          max_queries=min(KEY_SEARCH_QUERIES, left))
    return list(found[0].columns) if found else []


def validate(session: Session, user: User, workspace_id: str, *, max_queries: int = MAX_QUERIES) -> dict[str, Any]:
    """Measure the suggestion's keys and joins through the gateway as the caller (bounded) and persist the results."""
    from sqlglot import exp

    from analystos.governance.policy import resolve_scope
    from analystos.runtime.context import default_gateway
    from analystos.skills.relationships import measure_distinct_tuples
    from analystos.skills.sqlbuild import and_all, col, count_star, ident, table, to_sql

    scope = resolve_scope(session, session.merge(user), workspace_id, minimum_role="editor")
    denied = {d.lower() for d in scope.denied_columns}
    doc = suggest(session, workspace_id)
    budget = _Budget(max_queries)
    runners: dict[str, Any] = {}

    def runner(fq: str) -> Any | None:
        src = scope.asset_sources.get(fq)
        if src is None:
            return None
        if src not in runners:
            runners[src] = default_gateway().run_sql_for(scope, actor=f"user:{user.id}", source_id=src)
        return runners[src]

    def readable(fq: str, columns: list[str]) -> bool:
        return fq in scope.assets and all(f"{fq}.{c}".lower() not in denied for c in columns)

    rows_of: dict[str, int] = {}
    tables_out, joins_out, skipped = [], [], []
    now = utcnow().isoformat()
    for t in doc["tables"]:
        cols = t["primary_key"]["columns"]
        run_sql = runner(t["fq"])
        if run_sql is None:
            continue
        if not cols:  # no key known: a bounded search for a minimal unique column set (composite keys too)
            cols = _search_key(session, run_sql, t, readable, budget)
            if not cols:
                continue
        if not readable(t["fq"], cols):
            continue
        if not budget.take(2):
            skipped.append({"kind": "key", "asset_id": t["asset_id"], "reason": "query budget spent"})
            continue
        dialect = getattr(run_sql, "dialect", "postgres")
        rows = _count(run_sql, to_sql(exp.select(count_star().as_(ident("n"))).from_(table(t["fq"])), dialect),
                      "semantic.model_check.rows")
        distinct, _ = measure_distinct_tuples(run_sql, dialect, t["fq"], cols)
        rows_of[t["fq"]] = rows
        unique = rows > 0 and distinct == rows
        tables_out.append({"asset_id": t["asset_id"], "rows": rows, "distinct_keys": distinct, "unique": unique})
        a = session.get(SourceAsset, t["asset_id"])
        a.stats = {**(a.stats or {}), "key_check": {"columns": cols, "rows": rows, "distinct_keys": distinct,
                                                    "unique": unique, "measured_at": now, "measured_by": user.id}}
    for r in doc["relationships"]:
        f, t = r["from"], r["to"]
        run_sql = runner(f["fq"])
        if run_sql is None or scope.asset_sources.get(t["fq"]) != scope.asset_sources.get(f["fq"]) \
                or not readable(f["fq"], f["columns"]) or not readable(t["fq"], t["columns"]):
            continue
        need = 1 if f["fq"] in rows_of else 2
        if not budget.take(need):
            skipped.append({"kind": "join", "from": f["fq"], "to": t["fq"], "reason": "query budget spent"})
            continue
        dialect = getattr(run_sql, "dialect", "postgres")
        if f["fq"] not in rows_of:
            rows_of[f["fq"]] = _count(run_sql, to_sql(exp.select(count_star().as_(ident("n"))).from_(table(f["fq"])), dialect),
                                      "semantic.model_check.rows")
        keys = [f"k{i}" for i in range(len(t["columns"]))]
        target = exp.select(*[col(c).as_(ident(k)) for c, k in zip(t["columns"], keys, strict=True)]).from_(table(t["fq"]))
        on = and_all([exp.EQ(this=exp.column(fc, table="f", quoted=True), expression=exp.column(k, table="p", quoted=True))
                      for fc, k in zip(f["columns"], keys, strict=True)])
        q = (exp.select(count_star().as_(ident("n"))).from_(table(f["fq"], alias="f"))
             .join(exp.Subquery(this=target, alias=exp.TableAlias(this=ident("p"))), on=on, join_type="left"))
        after = _count(run_sql, to_sql(q, dialect), "semantic.model_check.fanout")
        before = rows_of[f["fq"]]
        check = {"rows_before": before, "rows_after": after, "fans_out": after > before, "measured_at": now,
                 "measured_by": user.id}
        joins_out.append({"from": f["fq"], "to": t["fq"], "from_columns": f["columns"], "to_columns": t["columns"],
                          "rows_before": before, "rows_after": after, "fans_out": after > before})
        if r.get("relationship_id"):
            rel = session.get(Relationship, r["relationship_id"])
            if rel is not None:
                rel.evidence = {**(rel.evidence or {}), "fanout_check": check}
        if r.get("candidate_id"):
            cand = session.get(SemanticRelationshipCandidate, r["candidate_id"])
            if cand is not None:
                cand.evidence = {**(cand.evidence or {}), "fanout_check": check}
    from analystos.governance.audit import audit

    audit(f"user:{user.id}", "semantic.model.suggestion_validated", workspace_id=workspace_id,
          details={"queries": budget.used, "tables": len(tables_out), "joins": len(joins_out)}, session=session)
    return {"tables": tables_out, "joins": joins_out, "queries": budget.used, "skipped": skipped}


# ------------------------------------------------------------------------------------ proposal
def propose(session: Session, user: User, workspace_id: str) -> dict[str, Any]:
    """The suggestion's entities, keys and grain as a `proposed` structure version (review.propose_structure), and
    its unmeasured relationships queued as measured candidates (review.propose_candidate)."""
    from analystos.contracts.semantic import DialectExpression, SemanticDataset, SemanticField
    from analystos.semantic import review
    from analystos.semantic.service import current_model

    doc = suggest(session, workspace_id)
    _, cols = _load(session, workspace_id)
    current = current_model(session, workspace_id)
    existing = {str(d.get("source") or "").strip().lower(): d["name"] for d in (current.datasets if current else []) or []}
    taken = set(existing.values())
    datasets = []
    for t in doc["tables"]:
        name = existing.get(t["fq"].lower())
        if name is None:
            name = _ident(t["name"])
            if name in taken:
                name = _ident(t["fq"].replace(".", "_"))
            taken.add(name)
        ai = {k: v for k, v in (("grain", t.get("grain")), ("entity", t.get("entity")), ("role", t.get("role"))) if v}
        fields = [SemanticField(name=c.name, expressions=[DialectExpression(expression=c.name)],
                                dimension={"is_time": True} if c.name == t["time_column"] else None,
                                label=c.business_name) for c in cols[t["asset_id"]]]
        datasets.append(SemanticDataset(name=name, source=t["fq"], primary_key=t["primary_key"]["columns"] or None,
                                        description=t.get("grain"), ai_context=ai or None, fields=fields))
    version, status = (current.version if current else None), "unchanged"
    if datasets:
        try:
            row = review.propose_structure(session, workspace_id, user, datasets,
                                           description="suggested from the catalog, profiles and measured relationships")
            version, status = row.version, row.status
        except Conflict:
            pass
    queued = 0
    for r in doc["relationships"]:
        if r["status"] != "proposed":
            continue
        try:
            cand = review.propose_candidate(session, user, workspace_id, from_asset=r["from"]["fq"],
                                            from_columns=r["from"]["columns"], to_asset=r["to"]["fq"],
                                            to_columns=r["to"]["columns"], origin="user")
        except AnalystOSError:
            continue  # out of scope, unreadable or empty: stays a proposal in the suggestion
        queued += int(cand.status == "pending")
    return {"model_version": version, "status": status, "candidates_queued": queued}
