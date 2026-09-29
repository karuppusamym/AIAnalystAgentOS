"""Index / partitioning / clustering advice for a workspace (N-11): advisory records, never applied.

`analyze` reads the workspace's audited query history for the caller's scope, asks the source for dry
plans of the slowest statements through `QueryGateway.explain` (validated like any query, read-only,
never ANALYZE, audited), joins crawler statistics, and stores the deterministic recommendations of
`skills/index_advice.py` as `IndexAdvice` rows. There is deliberately no apply path: the DDL is text for
a person, and the gateway validator rejects DDL on every route. Statuses only record a person's review.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.policy import DataScope
from analystos.core.errors import AnalystOSError, InvalidInput
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.db.models import IndexAdvice, QueryExecution, Source, SourceAsset, SourceColumn, User
from analystos.governance.audit import audit
from analystos.governance.policy import resolve_scope
from analystos.skills import index_advice as advice

REVIEW_STATUSES = {"open", "acknowledged", "dismissed"}
HISTORY_LIMIT = 5000
# Platform maintenance reads (crawler sampling, the advisor's own plans) are not workload.
_NOT_WORKLOAD = ("crawl", "index_advice", "federation.leg")


def analyze(session: Session, user: User, workspace_id: str, *, days: int = 30, min_ms: float = 500,
            min_table_rows: int = 10_000, explain: bool = True, max_explains: int = 10, limit: int = 25,
            gateway: Any = None) -> dict[str, Any]:
    """Analyse the caller's scope and store the advice; editors and above (it writes advisory rows)."""
    scope = resolve_scope(session, session.merge(user), workspace_id, minimum_role="editor")
    if gateway is None and explain:
        from analystos.runtime.context import default_gateway

        gateway = default_gateway()
    return advise(session, scope, actor=f"user:{user.id}", gateway=gateway if explain else None, days=days,
                  min_ms=min_ms, min_table_rows=min_table_rows, max_explains=max_explains, limit=limit)


def advise(session: Session, scope: DataScope, *, actor: str, gateway: Any = None, days: int = 30,
           min_ms: float = 500, min_table_rows: int = 10_000, partition_min_rows: int = 10_000_000,
           max_explains: int = 10, limit: int = 25) -> dict[str, Any]:
    """The analysis for an already-resolved scope. `gateway` (optional) is used for `explain` only."""
    if days < 1 or days > 365:
        raise InvalidInput("days must be between 1 and 365")
    stats = _history(session, scope, days=days, min_ms=min_ms)
    uses = {s.fingerprint: advice.column_uses(s.sql, s.dialect, scope.assets, scope.columns, set(scope.denied_columns))
            for s in stats}
    plans: dict[str, list[dict[str, Any]]] = {}
    plan_errors: list[dict[str, Any]] = []
    if gateway is not None:
        for s in sorted(stats, key=lambda s: -s.total_ms):
            if len(plans) + len(plan_errors) >= max_explains:
                break
            if s.dialect != "postgres" or not uses.get(s.fingerprint):
                continue
            try:
                out = gateway.explain(scope, s.sql, actor=actor, purpose="index_advice.explain")
            except AnalystOSError as exc:
                plan_errors.append({"fingerprint": s.fingerprint, "reason": exc.message[:300]})
                continue
            if out.get("available"):
                plans[s.fingerprint] = list(out.get("scans") or [])
            else:
                plan_errors.append({"fingerprint": s.fingerprint, "reason": str(out.get("reason") or "no plan")[:300]})
    recs, skipped = advice.recommend(stats, uses, plans, _asset_stats(session, scope), scope.source_dialects,
                                     scope.asset_sources, min_table_rows=min_table_rows,
                                     partition_min_rows=partition_min_rows, limit=limit)
    rows = _store(session, scope, recs, actor=actor)
    summary = {"queries_considered": sum(s.count for s in stats), "slow_fingerprints": len(stats),
               "explained": len(plans), "recommendations": len(rows), "skipped": len(skipped)}
    audit(actor, "index_advice.analyzed", workspace_id=scope.workspace_id,
          details={**summary, "days": days, "min_ms": min_ms}, session=session)
    from analystos.events.bus import emit

    emit(scope.workspace_id, "index_advice.generated", {**summary, "advice_ids": [r.id for r in rows][:50]},
         actor=actor, session=session)
    session.flush()
    return {"workspace_id": scope.workspace_id, "window_days": days, "min_ms": min_ms, **summary,
            "plan_errors": plan_errors, "items": [advice_out(r) for r in rows], "skipped_items": skipped[:100]}


def _history(session: Session, scope: DataScope, *, days: int, min_ms: float) -> list[advice.QueryStat]:
    """Slow fingerprints of the workspace's successful, uncached executions on the scope's sources."""
    if not scope.source_ids:
        return []
    since = utcnow() - timedelta(days=days)
    q = (select(QueryExecution.id, QueryExecution.fingerprint, QueryExecution.source_id, QueryExecution.sql,
                QueryExecution.duration_ms, QueryExecution.purpose)
         .where(QueryExecution.workspace_id == scope.workspace_id, QueryExecution.status == "ok",
                QueryExecution.cache_hit.is_(False), QueryExecution.created_at >= since,
                QueryExecution.source_id.in_(scope.source_ids))
         .order_by(QueryExecution.created_at.desc()).limit(HISTORY_LIMIT))
    groups: dict[str, list[Any]] = defaultdict(list)
    for r in session.execute(q):
        if (r.purpose or "").startswith(_NOT_WORKLOAD):
            continue
        groups[r.fingerprint or stable_hash([r.source_id, r.sql])].append(r)
    out = []
    for fp, items in groups.items():
        durations = [float(i.duration_ms or 0) for i in items]
        mean = sum(durations) / len(durations)
        if mean < min_ms:
            continue
        latest = items[0]
        out.append(advice.QueryStat(fingerprint=fp, source_id=latest.source_id,
                                    dialect=scope.source_dialects.get(latest.source_id, "postgres"), sql=latest.sql,
                                    count=len(items), avg_ms=mean, max_ms=max(durations),
                                    query_ids=[i.id for i in items[:5]]))
    return out


def _asset_stats(session: Session, scope: DataScope) -> dict[str, advice.AssetStats]:
    """Crawler statistics of the scope's assets: row count, distinct counts, declared types."""
    modes = dict(session.execute(select(Source.id, Source.execution_mode).where(Source.id.in_(scope.source_ids))).all())
    wanted = set(scope.assets)
    out: dict[str, advice.AssetStats] = {}
    by_id: dict[str, str] = {}
    for a in session.scalars(select(SourceAsset).where(SourceAsset.source_id.in_(scope.source_ids))):
        fq = f"{a.schema_name}.{a.name}"
        if fq in wanted:
            out[fq] = advice.AssetStats(row_count=a.row_count, execution_mode=modes.get(a.source_id) or "pushdown")
            by_id[a.id] = fq
    if by_id:
        for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id.in_(list(by_id)))):
            st = out[by_id[c.asset_id]]
            st.types[c.name] = c.data_type or ""
            distinct = (c.profile or {}).get("distinct")
            if isinstance(distinct, int):
                st.distinct[c.name] = distinct
    return out


def _store(session: Session, scope: DataScope, recs: list[dict[str, Any]], *, actor: str) -> list[IndexAdvice]:
    """Upsert by advice key. A person's dismissal or acknowledgement is kept; open advice on an analysed
    asset that this analysis no longer finds is superseded."""
    from analystos.artifacts.registry import link_queries

    existing = {a.advice_key: a for a in session.scalars(select(IndexAdvice).where(IndexAdvice.workspace_id == scope.workspace_id))}
    kept: list[IndexAdvice] = []
    for rec in recs:
        row = existing.get(rec["key"])
        fields = {k: rec[k] for k in ("source_id", "asset", "kind", "columns", "dialect", "ddl", "confidence",
                                      "estimated_benefit", "evidence", "notes")}
        if row is None:
            row = IndexAdvice(id=new_id("idx"), workspace_id=scope.workspace_id, advice_key=rec["key"], status="open",
                              created_by=actor, **fields)
            session.add(row)
        else:
            for k, v in fields.items():
                setattr(row, k, v)
            if row.status == "superseded":
                row.status = "open"
        session.flush()
        link_queries(session, scope.workspace_id, ("index_advice", row.id),
                     [q for item in rec["evidence"]["queries"] for q in item["query_ids"]], assets=[rec["asset"]])
        kept.append(row)
    found = {r.advice_key for r in kept}
    analysed = set(scope.assets)
    for key, row in existing.items():
        if key not in found and row.status == "open" and row.asset in analysed:
            row.status, row.status_by = "superseded", actor
    return kept


def advice_out(a: IndexAdvice) -> dict[str, Any]:
    return {"id": a.id, "workspace_id": a.workspace_id, "source_id": a.source_id, "asset": a.asset, "kind": a.kind,
            "columns": list(a.columns or []), "dialect": a.dialect, "ddl": a.ddl, "confidence": a.confidence,
            "estimated_benefit": a.estimated_benefit or {}, "evidence": a.evidence or {}, "notes": list(a.notes or []),
            "status": a.status, "status_note": a.status_note, "status_by": a.status_by, "created_by": a.created_by,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "updated_at": a.updated_at.isoformat() if a.updated_at else None, "applied_by_platform": False}


def list_advice(session: Session, user: User, workspace_id: str, status: str | None = None) -> list[dict[str, Any]]:
    """The workspace's advice, restricted to assets in the caller's scope (analysts and above)."""
    scope = resolve_scope(session, session.merge(user), workspace_id)
    q = select(IndexAdvice).where(IndexAdvice.workspace_id == workspace_id)
    if status:
        q = q.where(IndexAdvice.status == status)
    visible = set(scope.assets)
    rows = [a for a in session.scalars(q) if a.asset in visible]
    rows.sort(key=lambda a: (a.status != "open", -float((a.estimated_benefit or {}).get("saving_ms_in_window") or 0), a.asset))
    return [advice_out(a) for a in rows]


def review(session: Session, user: User, row: IndexAdvice, status: str, note: str | None = None) -> dict[str, Any]:
    """Record a person's review of advice already loaded in its workspace (acknowledged = a DBA will take it;
    dismissed = not wanted). Never applies it."""
    if status not in REVIEW_STATUSES:
        raise InvalidInput(f"status must be one of {sorted(REVIEW_STATUSES)}")
    row.status, row.status_note, row.status_by = status, (note or None), f"user:{user.id}"
    audit(f"user:{user.id}", "index_advice.reviewed", workspace_id=row.workspace_id, target=row.id,
          details={"status": status}, session=session)
    from analystos.events.bus import emit

    emit(row.workspace_id, "index_advice.reviewed", {"advice_id": row.id, "status": status}, actor=f"user:{user.id}",
         session=session)
    session.flush()
    return advice_out(row)
