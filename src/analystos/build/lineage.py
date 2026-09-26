"""Harvest a dbt run into lineage (P4-E04, TRN-001..003): a manifest and run-results summary,
OpenLineage run events per model, and the platform's own lineage edges.

dbt Core does not emit OpenLineage by itself (that is the `openlineage-dbt` wrapper), so the events
are derived from the artifacts dbt writes: one START and one COMPLETE/FAIL `RunEvent` per model,
with its source inputs, its output relation and the output's schema facet. They validate against the
pinned OpenLineage 2-0-2 `RunEvent` and facet schemas (`evidence/schema`, P4-K04) and are stored on
the build job for export.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from analystos.artifacts.registry import link
from analystos.evidence.schemas import OL_RUN_EVENT as OL_SCHEMA
from analystos.evidence.schemas import facet_schema_urls

PRODUCER = "https://github.com/context2ai/analystos/build"


def _facet(name: str, **fields: Any) -> dict[str, Any]:
    """A facet stamped with the `_schemaURL` of its pinned schema (evidence/schema, P4-K04)."""
    return {"_producer": PRODUCER, "_schemaURL": facet_schema_urls()[name], **fields}


def manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    meta = manifest.get("metadata") or {}
    models = []
    for uid, node in sorted((manifest.get("nodes") or {}).items()):
        if node.get("resource_type") != "model":
            continue
        models.append({"unique_id": uid, "name": node.get("name"), "schema": node.get("schema"),
                       "relation": node.get("relation_name"), "materialized": (node.get("config") or {}).get("materialized"),
                       "checksum": (node.get("checksum") or {}).get("checksum"),
                       "depends_on": sorted((node.get("depends_on") or {}).get("nodes") or []),
                       "columns": sorted((node.get("columns") or {}).keys())})
    sources = [{"unique_id": uid, "schema": s.get("schema"), "name": s.get("name")}
               for uid, s in sorted((manifest.get("sources") or {}).items())]
    return {"dbt_version": meta.get("dbt_version"), "schema_version": meta.get("dbt_schema_version"),
            "invocation_id": meta.get("invocation_id"), "models": models, "sources": sources,
            "tests": sorted(u for u, n in (manifest.get("nodes") or {}).items() if n.get("resource_type") == "test"),
            "semantic_models": sorted(manifest.get("semantic_models") or {}), "metrics": sorted(manifest.get("metrics") or {})}


def run_results_summary(run_results: dict[str, Any]) -> dict[str, Any]:
    results = [{"unique_id": r.get("unique_id"), "status": r.get("status"), "execution_time": round(r.get("execution_time") or 0, 3),
                "message": (r.get("message") or "")[:300], "failures": r.get("failures"),
                "rows_affected": (r.get("adapter_response") or {}).get("rows_affected")}
               for r in run_results.get("results") or []]
    counts: dict[str, int] = {}
    for r in results:
        counts[str(r["status"])] = counts.get(str(r["status"]), 0) + 1
    return {"elapsed_time": round(run_results.get("elapsed_time") or 0, 3), "counts": counts, "results": results,
            "invocation_id": (run_results.get("metadata") or {}).get("invocation_id")}


def _ol_dataset(namespace: str, database: str, schema: str, name: str, columns: list[str] | None = None) -> dict[str, Any]:
    ds: dict[str, Any] = {"namespace": namespace, "name": f"{database}.{schema}.{name}", "facets": {}}
    if columns:
        ds["facets"]["schema"] = _facet("SchemaDatasetFacet", fields=[{"name": c} for c in columns])
    return ds


def openlineage_events(*, job_id: str, workspace_id: str, manifest: dict[str, Any], run_results: dict[str, Any],
                       namespace: str, database: str, started_at: datetime, finished_at: datetime) -> list[dict[str, Any]]:
    nodes = manifest.get("nodes") or {}
    sources = manifest.get("sources") or {}
    status = {r.get("unique_id"): r.get("status") for r in run_results.get("results") or []}
    events: list[dict[str, Any]] = []
    for uid, node in sorted(nodes.items()):
        if node.get("resource_type") != "model":
            continue
        run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"analystos:{job_id}:{uid}"))
        job = {"namespace": f"analystos:{workspace_id}", "name": f"{job_id}.{node.get('name')}",
               "facets": {"jobType": _facet("JobTypeJobFacet", processingType="BATCH", integration="DBT", jobType="MODEL")}}
        code = node.get("compiled_code")
        if code:
            job["facets"]["sql"] = _facet("SQLJobFacet", query=code)
        inputs = []
        for dep in (node.get("depends_on") or {}).get("nodes") or []:
            if dep in sources:
                inputs.append(_ol_dataset(namespace, database, sources[dep].get("schema"), sources[dep].get("name")))
            elif dep in nodes:
                inputs.append(_ol_dataset(namespace, database, nodes[dep].get("schema"), nodes[dep].get("name")))
        output = _ol_dataset(namespace, database, node.get("schema"), node.get("alias") or node.get("name"),
                             sorted((node.get("columns") or {}).keys()))
        ok = status.get(uid) == "success"
        for event_type, when in (("START", started_at), ("COMPLETE" if ok else "FAIL", finished_at)):
            events.append({"eventType": event_type, "eventTime": when.isoformat(), "producer": PRODUCER, "schemaURL": OL_SCHEMA,
                           "run": {"runId": run_id, "facets": {}}, "job": job, "inputs": inputs,
                           "outputs": [output] if event_type != "START" else []})
    return events


def record_lineage(session: Session, *, workspace_id: str, run_id: str, job_id: str, approval_id: str | None,
                   manifest: dict[str, Any], run_results: dict[str, Any]) -> list[str]:
    """build_job -> produced -> table (target.model); source table -> transformed_into -> table.
    Returns the relations the run built successfully."""
    nodes = manifest.get("nodes") or {}
    sources = manifest.get("sources") or {}
    ok = {r.get("unique_id") for r in run_results.get("results") or [] if r.get("status") == "success"}
    built = []
    if approval_id:
        link(session, workspace_id, ("approval", approval_id), "authorized", ("build_job", job_id), run_id=run_id)
    for uid, node in sorted(nodes.items()):
        if node.get("resource_type") != "model" or uid not in ok:
            continue
        rel = f"{node.get('schema')}.{node.get('alias') or node.get('name')}"
        built.append(rel)
        link(session, workspace_id, ("build_job", job_id), "produced", ("table", rel), run_id=run_id)
        for dep in (node.get("depends_on") or {}).get("nodes") or []:
            src = sources.get(dep) or nodes.get(dep)
            if src:
                link(session, workspace_id, ("table", f"{src.get('schema')}.{src.get('alias') or src.get('name')}"),
                     "transformed_into", ("table", rel), run_id=run_id)
    return built
