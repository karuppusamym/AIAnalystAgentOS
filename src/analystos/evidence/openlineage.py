"""OpenLineage run events for governed queries (P4-K04), in the shape `build/lineage.py` uses for builds.

Every statement that ran through `QueryGateway.execute` has a `query_execution` row; this module
derives one START and one terminal event (COMPLETE, or FAIL with an error facet) per row. Events are
derived on demand, not stored: the audit row is the record and the derivation is deterministic (run
ids are UUIDv5 of the query id), so exporting twice gives identical events.

* job: namespace `analystos:<workspace>`, name `query.<fingerprint prefix>` (the same normalized
  statement is the same job), facets `jobType` (integration ANALYSTOS, jobType QUERY) and `sql`
  (the executed SQL and its dialect);
* run: `parent` points at the analysis run that issued it (when there is one);
* inputs: the referenced assets, namespace `analystos://source/<source id>`; outputs: none (reads).

Result rows and previews never appear in an event, only row counts in the audit.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.evidence.schemas import OL_RUN_EVENT, facet_schema_urls

PRODUCER = "https://github.com/context2ai/analystos"
INTEGRATION = "ANALYSTOS"


def facet(name: str, **fields: Any) -> dict[str, Any]:
    """A facet body stamped with this producer and the pinned `_schemaURL` of `name` (e.g. SQLJobFacet)."""
    return {"_producer": PRODUCER, "_schemaURL": facet_schema_urls()[name], **fields}


def run_uuid(kind: str, object_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"analystos:{kind}:{object_id}"))


def source_namespace(source_id: str | None) -> str:
    return f"analystos://source/{source_id or 'unknown'}"


def _dialect(session: Session, source_id: str | None, cache: dict[str, str | None]) -> str | None:
    if not source_id:
        return None
    if source_id not in cache:
        from analystos.connectors.kinds import dialect_for
        from analystos.db.models import Source

        src = session.get(Source, source_id)
        try:
            cache[source_id] = dialect_for(src.kind, src.execution_mode) if src is not None else None
        except Exception:  # noqa: BLE001 - an unknown kind only loses the optional dialect field
            cache[source_id] = None
    return cache[source_id]


def query_events(q: Any, *, dialect: str | None = None) -> list[dict[str, Any]]:
    """START + COMPLETE/FAIL for one `query_execution` row. A rejected statement never reached the
    source, so it has no events (the audit row and `query.rejected` event record it)."""
    if q.status == "rejected":
        return []
    ws = q.workspace_id
    job: dict[str, Any] = {"namespace": f"analystos:{ws}", "name": f"query.{(q.fingerprint or q.id)[:16]}",
                           "facets": {"jobType": facet("JobTypeJobFacet", processingType="BATCH", integration=INTEGRATION,
                                                       jobType="QUERY")}}
    sql = q.executed_sql or q.sql
    if sql:
        job["facets"]["sql"] = facet("SQLJobFacet", query=sql, **({"dialect": dialect} if dialect else {}))
    run_facets: dict[str, Any] = {}
    if q.run_id:
        run_facets["parent"] = facet("ParentRunFacet", run={"runId": run_uuid("analysis_run", q.run_id)},
                                     job={"namespace": f"analystos:{ws}", "name": f"analysis_run.{q.run_id}"})
    inputs = [{"namespace": source_namespace(q.source_id), "name": asset, "facets": {}}
              for asset in sorted(set(q.referenced_assets or []))]
    finished = q.created_at
    started = finished - timedelta(milliseconds=int(q.duration_ms or 0))
    run_id = run_uuid("query", q.id)
    ok = q.status == "ok"
    terminal_facets = dict(run_facets)
    if not ok:
        terminal_facets["errorMessage"] = facet("ErrorMessageRunFacet", message=(q.rejected_reason or q.status)[:2000],
                                                programmingLanguage="SQL")
    events = []
    for event_type, when, facets in (("START", started, run_facets), ("COMPLETE" if ok else "FAIL", finished, terminal_facets)):
        events.append({"eventType": event_type, "eventTime": when.isoformat(), "producer": PRODUCER, "schemaURL": OL_RUN_EVENT,
                       "run": {"runId": run_id, "facets": facets}, "job": job, "inputs": inputs, "outputs": []})
    return events


def events_for_run(session: Session, run_id: str, *, workspace_id: str | None = None) -> list[dict[str, Any]]:
    """OpenLineage events for every governed query of an analysis run, in execution order."""
    from analystos.db.models import QueryExecution

    stmt = select(QueryExecution).where(QueryExecution.run_id == run_id)
    if workspace_id is not None:
        stmt = stmt.where(QueryExecution.workspace_id == workspace_id)
    dialects: dict[str, str | None] = {}
    out: list[dict[str, Any]] = []
    for q in session.scalars(stmt.order_by(QueryExecution.created_at, QueryExecution.id)):
        out.extend(query_events(q, dialect=_dialect(session, q.source_id, dialects)))
    return out


def events_for_queries(session: Session, workspace_id: str, query_ids: list[str]) -> list[dict[str, Any]]:
    from analystos.db.models import QueryExecution

    dialects: dict[str, str | None] = {}
    out: list[dict[str, Any]] = []
    rows = session.scalars(select(QueryExecution).where(QueryExecution.workspace_id == workspace_id,
                                                        QueryExecution.id.in_(query_ids))
                           .order_by(QueryExecution.created_at, QueryExecution.id))
    for q in rows:
        out.extend(query_events(q, dialect=_dialect(session, q.source_id, dialects)))
    return out
