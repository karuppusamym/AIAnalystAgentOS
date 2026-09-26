"""DEX-001: a governed analysis run through the DuckDB engine. The ITSM benchmark dataset is uploaded
as a DuckDB database file and registered with the pushdown opt-in (`execution_mode: pushdown`), so
nothing is staged: discovery reads the file's catalog and every statement of the investigation runs
in DuckDB on the file (read-only), through QueryGateway.execute and its validator in the duckdb
dialect. No model provider (the rule path), local orchestrator. The run is repeated and must reach
the same verified findings from the same results (deterministic end to end), and the planted
effects must be found with no false verified finding. Skips cleanly without the compose Postgres."""
from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def no_models(control_db, monkeypatch):
    from analystos.core.config import get_settings
    from analystos.runtime.context import default_router

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("ANALYSTOS_ORCHESTRATOR", os.environ.get("ANALYSTOS_ORCHESTRATOR", "local"))
    get_settings.cache_clear()
    default_router.cache_clear()
    yield
    get_settings.cache_clear()
    default_router.cache_clear()


def _evidence(score) -> dict:
    from sqlalchemy import select

    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, Insight, QueryExecution, Source, SourceAsset

    with session_scope() as s:
        run = s.get(AnalysisRun, score.run_id)
        sources = list(s.scalars(select(Source).where(Source.workspace_id == run.workspace_id)))
        assets = list(s.scalars(select(SourceAsset).where(SourceAsset.workspace_id == run.workspace_id)))
        queries = list(s.scalars(select(QueryExecution).where(QueryExecution.run_id == run.id).order_by(QueryExecution.created_at)))
        verified = list(s.scalars(select(Insight).where(Insight.run_id == run.id, Insight.status == "verified")))
        return {
            "run_id": run.id, "status": run.status, "workspace_id": run.workspace_id,
            "sources": [{"id": x.id, "kind": x.kind, "execution_mode": x.execution_mode, "staging_schema": x.staging_schema}
                        for x in sources],
            "assets": [{"asset": f"{a.schema_name}.{a.name}", "selected": a.selected, "row_count": a.row_count} for a in assets],
            "queries": len(queries), "queries_ok": sum(q.status == "ok" for q in queries),
            "queries_rejected": sum(q.status == "rejected" for q in queries),
            "query_sources": sorted({q.source_id for q in queries if q.source_id}),
            "referenced_assets": sorted({a for q in queries for a in q.referenced_assets or []}),
            "result_hashes": sorted(q.result_hash for q in queries if q.status == "ok" and q.result_hash),
            "verified": sorted(f"{i.code}: {i.title}" for i in verified),
            "findings": sorted((f.method, f.outcome or "", f.segment or "", f.truth) for f in score.findings),
            "true_positive": score.true_positive, "false_positive": score.false_positive, "planted": score.planted,
            "seconds": score.seconds,
        }


def test_an_investigation_runs_governed_and_deterministic_in_the_duckdb_engine(no_models):
    from evaluation.analytical import run_platform

    first, second = (_evidence(run_platform("itsm", 1, effects=True, source="duckdb")) for _ in range(2))
    for ev in (first, second):
        assert ev["status"] == "COMPLETED", ev
        (src,) = ev["sources"]
        # Registered with the pushdown opt-in: queried in place, never staged into the analytics plane.
        assert src["kind"] == "duckdb" and src["execution_mode"] == "pushdown" and src["staging_schema"] is None
        # Every statement of the run went through the gateway against that one DuckDB source.
        assert ev["queries_ok"] >= 5 and ev["query_sources"] == [src["id"]]
        assert ev["referenced_assets"] and all(a.split(".")[-1] == "incident" for a in ev["referenced_assets"])
        # The planted effects are found and nothing false is verified.
        assert ev["verified"] and ev["false_positive"] == 0 and all(ev["planted"].values()), ev
    # Deterministic end to end: the same statements return the same results and verify the same findings.
    assert first["result_hashes"] == second["result_hashes"]
    assert first["findings"] == second["findings"] and [v.split(": ", 1)[1] for v in first["verified"]] == \
        [v.split(": ", 1)[1] for v in second["verified"]]
    out = os.environ.get("ANALYSTOS_DEX001_EVIDENCE")
    if out:  # the dated evidence file is written from this record (docs/60-delivery/evidence)
        with open(out, "w") as fh:
            json.dump({"first": first, "second": second}, fh, indent=1, default=str)
