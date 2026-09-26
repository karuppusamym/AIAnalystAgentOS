"""P4-K04: evidence in open formats, validated against the pinned upstream schemas (no services)."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from analystos.build import lineage
from analystos.contracts.bi import DatasetDef, MetricDef
from analystos.evidence import odcs
from analystos.evidence.openlineage import query_events
from analystos.evidence.schemas import SCHEMA_DIR, facet_schema_urls, validate_odcs, validate_openlineage
from analystos.knowledge import okf
from analystos.knowledge.attested import (
    AttestedComputation,
    Attestor,
    QueryReceipt,
    Statistics,
    check_attested,
    validate,
)
from analystos.knowledge.drafts import is_curated, tighten_tags

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def test_pinned_schema_files_match_their_provenance():
    prov = yaml.safe_load((SCHEMA_DIR / "PROVENANCE.yaml").read_text())
    pinned = {k: v for k, v in prov.items() if isinstance(v, dict) and "sha256" in v}
    assert len(pinned) == 8
    for rel, meta in pinned.items():
        assert hashlib.sha256((SCHEMA_DIR / rel).read_bytes()).hexdigest() == meta["sha256"], rel
    assert set(facet_schema_urls()) == {"SQLJobFacet", "JobTypeJobFacet", "SchemaDatasetFacet", "ParentRunFacet",
                                        "ErrorMessageRunFacet", "ColumnLineageDatasetFacet"}


# ------------------------------------------------------------------------------------ ODCS
def test_official_odcs_example_validates_and_a_broken_one_does_not():
    official = yaml.safe_load((FIXTURES / "odcs" / "official-full-example-v3.2.0.odcs.yaml").read_text())
    assert validate_odcs(official) == []
    broken = {**official, "kind": "Contract"}
    broken.pop("apiVersion")
    problems = validate_odcs(broken)
    assert any("apiVersion" in p for p in problems) and any("kind" in p for p in problems)


def _dataset() -> tuple[DatasetDef, list[MetricDef]]:
    ds = DatasetDef(name="incident.analysis", description="Incidents with SLA outcome.",
                    sql="SELECT priority, made_sla, resolution_hours FROM src_x.incident", time_column=None,
                    columns=[{"name": "priority", "type": "text", "semantic_type": "category", "business_name": "Priority"},
                             {"name": "made_sla", "type": "boolean"}, {"name": "opened_at", "semantic_type": "datetime"},
                             {"name": "resolution_hours", "type": "double precision", "tags": ["pii"]}],
                    source_assets=["src_x.incident"])
    metrics = [MetricDef(name="avg_resolution_hours", display_name="Average resolution (h)", definition="Mean hours.",
                         sql_expression="AVG(resolution_hours)", format="hours", source_columns=["resolution_hours"],
                         status="approved")]
    return ds, metrics


def test_generated_odcs_contract_validates_and_carries_the_dataset():
    ds, metrics = _dataset()
    c = odcs.contract_for_dataset(ds, workspace_id="ws_1", metrics=metrics, run_id="run_1", destination="superset",
                                  external_id=42, external_url="http://superset/explore/?datasource=42",
                                  bundle_hash="abc", physical_name="aos_ws_1_incident_analysis", created_at=NOW)
    assert validate_odcs(c) == []
    assert c["apiVersion"] == "v3.2.0" and c["kind"] == "DataContract"
    props = {p["name"]: p for p in c["schema"][0]["properties"]}
    assert props["resolution_hours"]["logicalType"] == "number" and props["resolution_hours"]["classification"] == "restricted"
    assert props["avg_resolution_hours"]["semanticType"] == "measure" and props["opened_at"]["logicalType"] == "timestamp"
    assert props["avg_resolution_hours"]["transformLogic"] == "AVG(resolution_hours)"
    assert odcs.contract_path(ds.name) == "contracts/incident-analysis.odcs.yaml"
    assert okf.check_path(odcs.contract_path(ds.name)) is None
    # deterministic id; a schema change bumps the minor version, anything else the patch, nothing -> None
    again = odcs.contract_for_dataset(ds, workspace_id="ws_1", metrics=metrics, run_id="run_1", destination="superset",
                                      external_id=42, external_url="http://superset/explore/?datasource=42",
                                      bundle_hash="abc", physical_name="aos_ws_1_incident_analysis")
    assert again["id"] == c["id"] and odcs.next_version(c, again) is None
    assert odcs.next_version(c, {**again, "customProperties": []}) == "1.0.1"
    changed = odcs.contract_for_dataset(ds.model_copy(update={"columns": ds.columns[:2]}), workspace_id="ws_1", metrics=metrics)
    assert odcs.next_version(c, changed) == "1.1.0" and odcs.next_version(None, c) == "1.0.0"
    assert validate_odcs(yaml.safe_load(odcs.render(c))) == []


def test_invalid_odcs_contract_is_reported():
    ds, _ = _dataset()
    c = odcs.contract_for_dataset(ds, workspace_id="ws_1")
    c["schema"][0]["properties"][0]["logicalType"] = "varchar"  # not in the ODCS enum
    c["schema"][0]["id"] = "has.dot"  # StableId forbids '.'
    problems = validate_odcs(c)
    assert any("logicalType" in p or "varchar" in p for p in problems) and any("has.dot" in p for p in problems)


# ------------------------------------------------------------------------------------ OpenLineage
def _query(**kw):
    base = {"id": "qry_1", "workspace_id": "ws_1", "source_id": "src_1", "run_id": "run_1", "status": "ok",
            "fingerprint": "f" * 64, "sql": "SELECT 1", "executed_sql": "SELECT 1 LIMIT 1000", "referenced_assets": ["src_x.incident"],
            "created_at": NOW, "duration_ms": 120, "rejected_reason": None}
    return SimpleNamespace(**{**base, **kw})


def test_query_events_validate_against_openlineage_and_their_facets():
    events = query_events(_query(), dialect="postgres")
    assert [e["eventType"] for e in events] == ["START", "COMPLETE"]
    for e in events:
        assert validate_openlineage(e) == [], validate_openlineage(e)
    done = events[1]
    assert done["job"]["facets"]["sql"]["query"] == "SELECT 1 LIMIT 1000" and done["job"]["facets"]["sql"]["dialect"] == "postgres"
    assert done["inputs"] == [{"namespace": "analystos://source/src_1", "name": "src_x.incident", "facets": {}}]
    assert done["run"]["facets"]["parent"]["job"]["name"] == "analysis_run.run_1"
    assert events[0]["run"]["runId"] == done["run"]["runId"] == query_events(_query())[0]["run"]["runId"]  # deterministic
    failed = query_events(_query(status="timeout", rejected_reason="statement timeout"))
    assert failed[1]["eventType"] == "FAIL" and failed[1]["run"]["facets"]["errorMessage"]["message"] == "statement timeout"
    assert all(validate_openlineage(e) == [] for e in failed)
    assert query_events(_query(status="rejected")) == []  # never reached the source


def test_build_events_validate_against_openlineage():
    manifest = {"nodes": {"model.p.ds": {"resource_type": "model", "name": "ds", "schema": "mart",
                                         "depends_on": {"nodes": ["source.p.s.incident"]}, "columns": {"number": {}},
                                         "compiled_code": "select 1"}},
                "sources": {"source.p.s.incident": {"schema": "s", "name": "incident"}}}
    results = {"results": [{"unique_id": "model.p.ds", "status": "error"}]}
    events = lineage.openlineage_events(job_id="bld_1", workspace_id="ws", manifest=manifest, run_results=results,
                                        namespace="postgres://h:5432", database="analytics", started_at=NOW, finished_at=NOW)
    assert [e["eventType"] for e in events] == ["START", "FAIL"]
    for e in events:
        assert validate_openlineage(e) == []


def test_openlineage_validation_catches_bad_events_and_unpinned_facets():
    good = query_events(_query())[1]
    no_run = {k: v for k, v in good.items() if k != "run"}
    assert any("run" in p for p in validate_openlineage(no_run))
    bad_type = {**good, "eventType": "DONE"}
    assert any("DONE" in p for p in validate_openlineage(bad_type))
    sql = dict(good["job"]["facets"]["sql"])
    sql.pop("query")
    bad_facet = {**good, "job": {**good["job"], "facets": {**good["job"]["facets"], "sql": sql}}}
    assert any("query" in p for p in validate_openlineage(bad_facet))
    unpinned = {**good, "run": {"runId": good["run"]["runId"], "facets": {"x": {"_producer": "p", "_schemaURL": "https://x/y.json"}}}}
    assert any("not pinned" in p for p in validate_openlineage(unpinned))
    bad_uuid = {**good, "run": {**good["run"], "runId": "not-a-uuid"}}
    assert any("uuid" in p for p in validate_openlineage(bad_uuid))


# ------------------------------------------------------------------------------------ Attested Computation
def _attested(**kw) -> AttestedComputation:
    base = dict(insight_id="ins_1", code="I1", workspace_id="ws_1", run_id="run_1", title="P1 incidents miss SLA more often",
                claim="Priority 1 incidents miss the SLA more often than others. Association only.", runtime="postgres",
                computation="SELECT priority, count(*) FROM src_x.incident GROUP BY 1", method="rate_by_segment",
                params={"method": "rate_by_segment", "asset": "src_x.incident"}, spec_hash="s" * 64, plan_hash="p" * 64,
                query_hash="q" * 64, result_hash="r" * 64,
                queries=[QueryReceipt(query_id="qry_1", query_hash="q" * 64, result_hash="r" * 64, source_id="src_1", row_count=4),
                         QueryReceipt(query_id="qry_2", role="verification", query_hash="v" * 64, result_hash="w" * 64)],
                statistics=Statistics(test="chi_square", n=500, p_value=0.0001, q_value=0.0004, effect_size=0.31,
                                      effect_label="cramers_v"),
                confidence=0.8, checks=[{"check": "reproducible_rerun", "passed": True}],
                verified_by=[Attestor(by="process:analystos-rev", at=NOW)], caveats=["Observational."],
                assets=["src_x.incident"], generated_at=NOW, stale_after=NOW + timedelta(days=90))
    return AttestedComputation(**{**base, **kw})


def test_attested_computation_is_a_conformant_okf_document_carrying_the_evidence():
    ac = _attested()
    text = ac.render()
    doc = okf.parse_document(ac.path, text.encode())
    assert doc.type == "Attested Computation" and ac.path == "findings/ins-1.md"
    assert validate(doc) == []  # the K08 minimal shape holds for every full document
    assert okf.check_conformance({ac.path: text.encode(), "index.md": b"# Index\n"}) == []
    fm = doc.frontmatter
    assert check_attested(fm) == []
    att = fm["analystos"]["attestation"]
    assert att["query_hash"] == "q" * 64 and att["result_hash"] == "r" * 64
    assert att["q_value"] == 0.0004 and att["effect_size"] == 0.31
    assert [a["by"] for a in att["verified_by"]] == ["process:analystos-rev"] and att["spec_hash"] == "s" * 64 and att["plan_hash"] == "p" * 64
    assert okf.parse_instant(fm["stale_after"]) == NOW + timedelta(days=90)
    assert fm["runtime"] == "postgres" and fm["executor"]["receipt"] == ["query_id", "executed_sql", "result_hash"]
    assert doc.trust_tier == "machine-confirmed" and doc.status == "draft"
    assert not okf.is_stale(fm, NOW) and okf.is_stale(fm, NOW + timedelta(days=91))
    assert [s.anchor for s in doc.sections][:3] == ["claim", "computation", "evidence"]
    assert AttestedComputation.from_document(text) == ac  # lossless round trip


def test_attested_with_a_human_approver_is_stable_and_human_reviewed():
    ac = _attested(status="stable", approved_by=[Attestor(by="human:usr_9", at=NOW, basis="approved")])
    doc = okf.parse_document(ac.path, ac.render().encode())
    assert doc.trust_tier == "human-reviewed" and doc.frontmatter["analystos"]["attestation"]["approved_by"][0]["by"] == "human:usr_9"
    assert AttestedComputation.from_document(ac.render()) == ac


def test_check_attested_names_every_missing_field():
    fm = _attested().frontmatter()
    fm.pop("runtime")
    fm.pop("stale_after")
    del fm["analystos"]["attestation"]["q_value"]
    fm["analystos"]["attestation"]["verified_by"] = []
    problems = check_attested(fm)
    assert any("runtime" in p for p in problems) and any("stale_after" in p for p in problems)
    assert any("q_value" in p for p in problems) and any("verified_by" in p for p in problems)
    assert check_attested(None) == ["frontmatter missing"]
    with pytest.raises(Exception, match="Attested Computation"):
        AttestedComputation.from_document(okf.render_document(fm, "# Claim\n\nx"))


# ------------------------------------------------------------------------------------ drafts
def test_curated_documents_and_tags_only_tighten():
    human = okf.render_document({"type": "Table", "generated": {"by": "human:ann"}, "tags": ["pii"]}, "x").encode()
    reviewed = okf.render_document({"type": "Table", "verified": {"by": "human:bob", "at": "2026-01-01T00:00:00Z"}}, "x").encode()
    machine = okf.render_document({"type": "Table", "generated": {"by": "process:analystos-crawler"}, "tags": ["pii", "owner-x"]},
                                  "x").encode()
    from analystos.knowledge.drafts import _frontmatter

    assert is_curated(_frontmatter(human)) and is_curated(_frontmatter(reviewed)) and not is_curated(_frontmatter(machine))
    assert is_curated({"type": "Table", "analystos": {"origin": "user"}}) and not is_curated(None)
    new = okf.render_document({"type": "Table", "tags": ["table"]}, "y")
    tightened = okf.parse_document("t.md", tighten_tags(new, machine).encode()).frontmatter
    assert tightened["tags"] == ["owner-x", "pii", "table"]
    assert tighten_tags(new, None) == new
