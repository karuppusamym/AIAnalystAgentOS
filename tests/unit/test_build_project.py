"""P4-E04/E06 without services: dbt project generation is deterministic and hash-stable, the static
guard refuses anything the builder would not generate, targets never shadow sources, the build
identity is its own login, and a dbt run is harvested into OpenLineage events and lineage."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from analystos.build import lineage
from analystos.build import project as P
from analystos.build.targets import check_identities, check_schema_name
from analystos.contracts.bi import MetricDef
from analystos.core.errors import Forbidden, InvalidInput, PolicyDenied

DATASET = {
    "name": "aos_sla_breaches_abc123", "description": "Analytical dataset",
    "sql": 'SELECT t."number", t."opened_at", DATE_TRUNC(\'month\', t."opened_at") AS "opened_at_month", t."priority", '
           'j0."name" AS assigned_name FROM src_s1.incident t LEFT JOIN src_s1.sys_user j0 ON t."assigned_to" = j0."id"',
    "columns": [{"name": "number", "semantic_type": "text"}, {"name": "opened_at", "semantic_type": "datetime"},
                {"name": "opened_at_month", "semantic_type": "datetime", "derived": True},
                {"name": "priority", "semantic_type": "categorical"}, {"name": "assigned_name", "semantic_type": "text"}],
    "time_column": "opened_at_month",
}
ALLOWED = {"src_s1.incident", "src_s1.sys_user"}
METRICS = [
    MetricDef(name="incident_count", display_name="Incidents", definition="Records", sql_expression="COUNT(*)"),
    MetricDef(name="sla_rate", display_name="SLA", definition="Share", sql_expression='AVG(CASE WHEN "made_sla" THEN 1.0 ELSE 0.0 END)'),
    MetricDef(name="median_hours", display_name="Median", definition="m", sql_expression='PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "h")'),
    MetricDef(name="ratio", display_name="Ratio", definition="r", sql_expression="SUM(a) / COUNT(*)"),
]


def _project():
    tests = P.candidate_tests(DATASET)
    return P.generate(DATASET, METRICS, tests)


def test_generation_is_deterministic_and_hash_bound():
    a, b = _project(), _project()
    assert a.files == b.files and a.hash == b.hash
    assert set(a.files) == {"dbt_project.yml", "models/aos_sla_breaches_abc123.sql", "models/metricflow_time_spine.sql",
                            "models/schema.yml", "models/sources.yml"}
    model = a.files["models/aos_sla_breaches_abc123.sql"]
    assert "{{ source('src_s1', 'incident') }}" in model and "{{ source('src_s1', 'sys_user') }}" in model
    assert "src_s1.incident" not in model
    changed = dict(a.files)
    changed["models/aos_sla_breaches_abc123.sql"] += "-- tampered\n"
    assert P.project_hash(changed) != a.hash
    assert a.relations("mart") == ["mart.aos_sla_breaches_abc123", "mart.metricflow_time_spine"]


def test_tests_keys_and_metrics():
    tests = P.candidate_tests(DATASET)
    assert {"column": "number", "test": "unique"} in tests and {"column": "opened_at_month", "test": "not_null"} in tests
    probe = P.test_probe_sql(DATASET["sql"], tests)
    assert probe.startswith("SELECT COUNT(*) AS n, COUNT(\"number\") AS c0, COUNT(DISTINCT \"number\") AS c1")
    # 10 rows, key has 10 non-null but 9 distinct values: unique is dropped, not_null kept
    assert P.passing_tests(tests, [10, 10, 9, 10]) == [tests[0], tests[2]]
    p = _project()
    assert [m["agg"] for m in p.metrics] == ["count", "average", "median"]
    assert p.skipped_metrics == [{"metric": "ratio", "reason": "not a single aggregate (Div)"}]
    assert "semantic_models:" in p.files["models/schema.yml"] and "agg_time_dimension: opened_at_month" in p.files["models/schema.yml"]


def test_no_time_column_means_no_semantic_layer():
    ds = {**DATASET, "time_column": None}
    p = P.generate(ds, METRICS[:1], [])
    assert "models/metricflow_time_spine.sql" not in p.files and "semantic_models" not in p.files["models/schema.yml"]
    assert p.skipped_metrics[0]["reason"].startswith("the dataset has no time column")


def test_static_guard_accepts_generated_and_refuses_tampering():
    p = _project()
    P.check_files(p.files, allowed_sources=ALLOWED)
    cases = {
        "models/evil.sql": "SELECT 1",  # fine on its own: a new SELECT model is shape-valid
        "macros/x.sql": "{% macro x() %}drop table y{% endmacro %}",
        "packages.yml": "packages: [{package: dbt-labs/dbt_utils}]",
    }
    P.check_files({**p.files, "models/evil.sql": cases["models/evil.sql"]}, allowed_sources=ALLOWED)
    for path in ("macros/x.sql", "packages.yml"):
        with pytest.raises(PolicyDenied, match="not a file the builder generates"):
            P.check_files({**p.files, path: cases[path]}, allowed_sources=ALLOWED)
    model = "models/aos_sla_breaches_abc123.sql"
    refusals = {
        "{{ config(materialized='table', post_hook='grant all on src_s1.incident to public') }} SELECT 1": "Jinja expression",
        "{% set x = run_query('drop table src_s1.incident') %} SELECT 1": "Jinja statements",
        "DELETE FROM {{ source('src_s1', 'incident') }}": "exactly one SELECT",
        "SELECT * FROM {{ source('src_s2', 'secrets') }}": "outside the run's scope",
        "SELECT pg_read_file('/etc/passwd')": "not allowed",
        "SELECT 1; DROP TABLE mart.x": "exactly one SELECT",
    }
    for text, reason in refusals.items():
        with pytest.raises(PolicyDenied, match=reason):
            P.check_files({**p.files, model: text}, allowed_sources=ALLOWED)
    project_yml = p.files["dbt_project.yml"]
    for extra in ("on-run-start: ['drop schema src_s1 cascade']\n", "vars: {x: 1}\n"):
        with pytest.raises(PolicyDenied, match="not allowed"):
            P.check_files({**p.files, "dbt_project.yml": project_yml + extra}, allowed_sources=ALLOWED)
    with pytest.raises(PolicyDenied, match="not allowed"):
        P.check_files({**p.files, "dbt_project.yml": project_yml.replace("+materialized: table", "+schema: src_s1")},
                      allowed_sources=ALLOWED)
    with pytest.raises(PolicyDenied, match="key schema is not allowed"):
        P.check_files({**p.files, "models/schema.yml": p.files["models/schema.yml"] + "  config: {}\n  schema: src_s1\n"},
                      allowed_sources=ALLOWED)


def test_manifest_guard_checks_what_dbt_resolved():
    node = {"resource_type": "model", "name": "m", "schema": "mart", "config": {"materialized": "table"}, "depends_on": {"nodes": []}}
    ok = {"nodes": {"model.p.m": node}, "sources": {"source.p.src_s1.incident": {"schema": "src_s1", "name": "incident"}},
          "macros": {"macro.dbt.x": {}}, "metadata": {"dbt_version": "1.12.5"}}
    assert P.check_manifest(ok, project="p", target_schema="mart", allowed_sources=ALLOWED)["dbt_version"] == "1.12.5"
    bad = [
        {**ok, "nodes": {"model.p.m": {**node, "schema": "src_s1"}}},
        {**ok, "nodes": {"model.p.m": {**node, "config": {"materialized": "table", "post-hook": ["grant ..."]}}}},
        {**ok, "nodes": {"model.p.m": {**node, "config": {"materialized": "incremental"}}}},
        {**ok, "macros": {"macro.p.drop_everything": {}}},
        {**ok, "sources": {"source.p.src_s9.t": {"schema": "src_s9", "name": "t"}}},
        {**ok, "nodes": {"seed.p.s": {"resource_type": "seed", "config": {}}}},
    ]
    for manifest in bad:
        with pytest.raises(PolicyDenied):
            P.check_manifest(manifest, project="p", target_schema="mart", allowed_sources=ALLOWED)


def test_targets_never_shadow_sources_or_system_schemas():
    check_schema_name("analytics_mart")
    for schema in ("src_s1", "public", "pg_temp", "information_schema"):
        with pytest.raises(Forbidden):
            check_schema_name(schema)
    with pytest.raises(Forbidden):
        check_schema_name("mart", source_schemas={"mart"})
    with pytest.raises(InvalidInput):
        check_schema_name("Mart; drop")


def test_build_identity_is_its_own_login():
    base = dict(database_url="postgresql+psycopg://analystos:x@h:5432/analystos",
                analytics_loader_url="postgresql+psycopg://loader:x@h:5432/analytics",
                analytics_reader_url="postgresql+psycopg://reader:x@h:5432/analytics")
    check_identities(SimpleNamespace(**base, analytics_builder_url="postgresql+psycopg://builder:x@h:5432/analytics"))
    for user in ("reader", "loader", "analystos"):
        with pytest.raises(InvalidInput):
            check_identities(SimpleNamespace(**base, analytics_builder_url=f"postgresql+psycopg://{user}:x@h:5432/analytics"))


def test_harvest_to_openlineage_and_lineage():
    manifest = {"metadata": {"dbt_version": "1.12.5"},
                "nodes": {"model.p.ds": {"resource_type": "model", "name": "ds", "schema": "mart", "config": {"materialized": "table"},
                                         "depends_on": {"nodes": ["source.p.src_s1.incident"]}, "columns": {"number": {}},
                                         "compiled_code": "select 1"},
                          "test.p.nn": {"resource_type": "test", "name": "nn"}},
                "sources": {"source.p.src_s1.incident": {"schema": "src_s1", "name": "incident"}}}
    results = {"elapsed_time": 1.2, "results": [{"unique_id": "model.p.ds", "status": "success", "execution_time": 0.5,
                                                 "adapter_response": {"rows_affected": 42}},
                                                {"unique_id": "test.p.nn", "status": "pass", "execution_time": 0.1}]}
    now = datetime(2026, 9, 25, tzinfo=UTC)
    events = lineage.openlineage_events(job_id="bld_1", workspace_id="ws", manifest=manifest, run_results=results,
                                        namespace="postgres://h:5432", database="analytics", started_at=now, finished_at=now)
    assert [e["eventType"] for e in events] == ["START", "COMPLETE"]
    done = events[1]
    assert done["inputs"][0]["name"] == "analytics.src_s1.incident" and done["outputs"][0]["name"] == "analytics.mart.ds"
    assert done["job"]["facets"]["sql"]["query"] == "select <NUM>" and done["run"]["runId"] == events[0]["run"]["runId"]
    assert {"eventType", "eventTime", "producer", "schemaURL", "run", "job"} <= set(done)
    summary = lineage.run_results_summary(results)
    assert summary["counts"] == {"success": 1, "pass": 1} and summary["results"][0]["rows_affected"] == 42
    assert lineage.manifest_summary(manifest)["models"][0]["relation"] is None
