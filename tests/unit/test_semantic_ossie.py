"""P4-K03 conformance (SEM-001..005): the pinned Apache Ossie 0.1.1 adapter and the dbt 1.12 interchange.

Fixtures are upstream files or real dbt output (tests/fixtures/ossie/PROVENANCE.yaml), never hand-written."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from analystos.contracts.bi import MetricDef, PublishBundle
from analystos.contracts.policy import WorkspacePolicyDoc
from analystos.contracts.semantic import DialectExpression, SemanticDataset, SemanticMetricDef, SemanticModelDoc
from analystos.core.errors import PolicyDenied
from analystos.semantic import dbt, ossie

FIXTURES = Path(__file__).parents[1] / "fixtures" / "ossie"
OFFICIAL = ["official_tpcds_semantic_model.yaml", "official_gooddata_osi_tpcds.yaml", "official_salesforce_osi_example.yaml"]
DBT_DOC = FIXTURES / "dbt_1_12_0_osi_document.json"


def _load(name: str) -> dict:
    return ossie.parse_text((FIXTURES / name).read_text())


def test_pinned_schema_is_the_upstream_file():
    pins = yaml.safe_load((ossie.SCHEMA_PATH.parent / "PROVENANCE.yaml").read_text())
    assert hashlib.sha256(ossie.SCHEMA_PATH.read_bytes()).hexdigest() == pins["ossie-0.1.1.json"]["sha256"]
    assert ossie.schema()["properties"]["version"]["const"] == ossie.OSSIE_VERSION == "0.1.1"


@pytest.mark.parametrize("name", OFFICIAL)
def test_official_examples_are_schema_valid(name):
    assert ossie.schema_problems(_load(name)) == []


@pytest.mark.parametrize("name", OFFICIAL[:2])
def test_official_examples_pass_every_upstream_check(name):
    """Schema + unique names + relationship references + SQL parse, as upstream validation/validate.py."""
    assert ossie.validate(_load(name)) == []


def test_upstream_salesforce_fixture_fails_upstreams_own_sql_rule():
    """Recorded, not hidden: the Salesforce converter fixture uses [bracket] identifiers under ANSI_SQL,
    which upstream validate.py's sqlglot check rejects too. It is schema-valid (above)."""
    problems = ossie.validate(_load("official_salesforce_osi_example.yaml"))
    assert problems and all(p.startswith("[SQL]") for p in problems)


@pytest.mark.parametrize("name", [*OFFICIAL, DBT_DOC.name])
def test_documents_round_trip_losslessly(name):
    doc = _load(name)
    assert ossie.to_ossie(ossie.from_ossie(doc))["semantic_model"] == doc["semantic_model"]


def test_real_dbt_osi_document_imports_and_flags_the_broken_derived_metric():
    models, issues = dbt.import_osi_document(DBT_DOC.read_text())
    (model,) = models
    assert model.name == "semantic_model"
    assert [d.name for d in model.datasets] == ["orders", "customers"]
    assert [(r.from_dataset, r.to) for r in model.relationships] == [("orders", "customers")]
    assert {m.name for m in model.metrics} == {"net_revenue", "orders", "returned", "return_rate", "average_order_value",
                                               "customers_total"}
    assert next(m for m in model.metrics if m.name == "net_revenue").expression == "SUM(orders.amount)"
    # metricflow substitutes the metric name `orders` inside `orders.amount`: kept as a proposal, but reported
    assert any(i.startswith("metric average_order_value:") for i in issues), issues


def test_dbt_round_trip_import_export_reimport_is_equal():
    models, _ = dbt.import_osi_document(DBT_DOC.read_text())
    files, issues = dbt.export_osi_folder(models)
    assert list(files) == ["osi/semantic_model.json"]
    exported = json.loads(files["osi/semantic_model.json"])
    assert ossie.schema_problems(exported) == []
    again, _ = dbt.import_osi_document(files["osi/semantic_model.json"])
    assert again == models
    assert dbt.export_osi_folder(again)[0] == files
    assert issues == []  # dbt's own sources are database.schema.alias


def test_dbt_export_is_the_file_real_dbt_parsed():
    """Pins the export shape `dbt parse` (dbt-core 1.12.0) accepted: unquoted database.schema.alias
    sources and key columns named by field (dbt's importer matches field names)."""
    models, _ = dbt.import_osi_document(DBT_DOC.read_text())
    models[0].metrics = [m for m in models[0].metrics if m.name != "average_order_value"]
    files, _ = dbt.export_osi_folder(models)
    assert json.loads(files["osi/semantic_model.json"]) == json.loads((FIXTURES / "dbt_1_12_0_parsed_our_export.json").read_text())
    customers = next(d for d in models[0].datasets if d.name == "customers")
    assert customers.source == "shop.main.dim_customers" and customers.primary_key == ["customer"]


def test_dbt_reemission_of_our_export_is_equal_up_to_dbts_documented_losses():
    ours, _ = dbt.import_osi_document((FIXTURES / "dbt_1_12_0_parsed_our_export.json").read_text())
    back, _ = dbt.import_osi_document((FIXTURES / "dbt_1_12_0_reemitted_our_export.json").read_text())
    assert [d.name for d in back[0].datasets] == [d.name for d in ours[0].datasets]
    assert back[0].relationships == ours[0].relationships
    theirs = {m.name: ossie.normalize_expression(m.expression) for m in back[0].metrics}
    for m in ours[0].metrics:
        assert theirs[m.name] == ossie.normalize_expression(m.expression), m.name


def test_dbt_export_warns_about_metricflows_dot_truncation():
    model = SemanticModelDoc(name="ws", datasets=[SemanticDataset(name="orders", source="shop.main.fct_orders")], metrics=[
        SemanticMetricDef(name="return_rate", expressions=[DialectExpression(expression="AVG(CASE WHEN r THEN 1.0 ELSE 0.0 END)")]),
        SemanticMetricDef(name="revenue", expressions=[DialectExpression(expression="SUM(orders.amount)")])])
    _, issues = dbt.export_osi_folder([model])
    assert [i.split(":")[0] for i in issues] == ["metric return_rate"]


def test_dbt_import_conforms_newer_metricflow_output():
    """Newer metricflow (0.2-style) writes `datatype` and new dialects; 0.1.1 has no slot for them."""
    doc = json.loads(DBT_DOC.read_text())
    sm = doc["semantic_model"][0]
    sm["metrics"][0]["datatype"] = "Decimal"
    sm["datasets"][0]["fields"][0]["datatype"] = "Integer"
    sm["metrics"][1]["expression"]["dialects"].append({"dialect": "OSSIE_SQL_2026", "expression": "SUM(x)"})
    models, issues = dbt.import_osi_document(doc)
    assert models[0].metrics[0].datatype == "Decimal"
    assert any("dropped datatype" in i for i in issues)
    assert any("unsupported dialect OSSIE_SQL_2026" in i for i in issues)
    assert ossie.schema_problems(ossie.to_ossie(models)) == []


def test_dbt_import_refuses_versions_dbt_refuses():
    doc = json.loads(DBT_DOC.read_text())
    with pytest.raises(ossie.OssieError, match="unsupported Ossie version"):
        dbt.import_osi_document({**doc, "version": "0.2.0.dev0"})
    models, issues = dbt.import_osi_document({**doc, "version": "0.1.0"})
    assert models and any("0.1.0" in i for i in issues)


def test_analystos_fields_ride_in_one_common_extension():
    metric = SemanticMetricDef(name="sla_breach_rate", expressions=[DialectExpression(expression='AVG(CASE WHEN "breached" THEN 1.0 ELSE 0.0 END)')],
                               description="Share of incidents that breached SLA.", display_name="SLA breach rate", format="percent",
                               grain="record", dataset="incidents", dimensions=["priority"],
                               custom_extensions=[{"vendor_name": "SNOWFLAKE", "data": "{\"x\": 1}"}])
    model = SemanticModelDoc(name="ws", datasets=[SemanticDataset(name="incidents", source="analytics.src.incident")], metrics=[metric])
    doc = ossie.to_ossie([model])
    assert ossie.validate(doc) == []
    exts = doc["semantic_model"][0]["metrics"][0]["custom_extensions"]
    assert exts[0] == {"vendor_name": "SNOWFLAKE", "data": "{\"x\": 1}"}  # other vendors' data verbatim
    assert exts[1]["vendor_name"] == "COMMON" and json.loads(exts[1]["data"])["analystos"]["format"] == "percent"
    assert ossie.from_ossie(doc) == [model]


def test_dbt_export_reports_what_dbt_would_refuse():
    model = SemanticModelDoc(name="ws", datasets=[SemanticDataset(name="incidents", source="SELECT * FROM src.incident",
                                                                  primary_key=["a", "b"])])
    _, issues = dbt.export_osi_folder([model])
    assert any("database.schema.alias" in i for i in issues) and any("composite primary key" in i for i in issues)


def test_duplicate_detection_ignores_quoting_case_and_spacing():
    assert ossie.normalize_expression('AVG("resolution_hours")') == ossie.normalize_expression("avg( resolution_hours )")
    assert ossie.normalize_expression("SUM(a)") != ossie.normalize_expression("SUM(b)")


@pytest.mark.parametrize("expr, problem", [("SUM(amount)", None), ("amount", "not an aggregate expression"),
                                           ("SUM((SELECT 1))", "subqueries are not allowed in metric expressions"),
                                           ("SUM(", "unparseable")])
def test_metric_shape_rule(expr, problem):
    got = ossie.metric_problem(SemanticMetricDef(name="m", expressions=[DialectExpression(expression=expr)]))
    assert (got is None) if problem is None else got.startswith(problem)


# ------------------------------------------------------------------------------------ publish gate
def _bundle(*metrics: MetricDef) -> PublishBundle:
    return PublishBundle(workspace_id="ws_1", destination="preview", datasets=[], metrics=list(metrics), charts=[], dashboards=[])


def _metric(name: str, expr: str) -> MetricDef:
    return MetricDef(name=name, display_name=name, definition="", sql_expression=expr, status="validated")


def _approved(monkeypatch, **defs: str):
    from analystos.semantic import service

    rows = {n: SimpleNamespace(name=n, status="approved", owner_id="usr_owner", normalized_expression=ossie.normalize_expression(e),
                               definition=SemanticMetricDef(name=n, expressions=[DialectExpression(expression=e)],
                                                            display_name=n.title(), format="percent").model_dump())
            for n, e in defs.items()}
    monkeypatch.setattr(service, "approved_metrics", lambda session, ws: rows)
    return service


def test_gate_refuses_unapproved_kpis_with_a_remedy(monkeypatch):
    service = _approved(monkeypatch, breach_rate="AVG(b)")
    bundle = _bundle(_metric("breach_rate", 'AVG("b")'), _metric("record_count", "COUNT(*)"))
    with pytest.raises(PolicyDenied) as err:
        service.gate_bundle(None, "ws_1", WorkspacePolicyDoc(require_approved_metrics=True), bundle)
    assert err.value.details["unapproved_metrics"] == ["record_count"]
    assert "/semantic/metrics/<name>/approve" in err.value.message and "require_approved_metrics" in err.value.message


def test_gate_stamps_approved_definitions(monkeypatch):
    service = _approved(monkeypatch, breach_rate="AVG(b)", record_count="COUNT(*)")
    out = service.gate_bundle(None, "ws_1", WorkspacePolicyDoc(require_approved_metrics=True),
                              _bundle(_metric("breach_rate", 'AVG("b")'), _metric("record_count", "count(*)")))
    assert {m.status for m in out.metrics} == {"approved"}
    assert out.metrics[0].display_name == "Breach_Rate" and out.metrics[0].format == "percent" and out.metrics[0].owner == "usr_owner"


def test_gate_does_not_accept_a_variant_under_an_approved_name(monkeypatch):
    service = _approved(monkeypatch, breach_rate="AVG(b)")
    with pytest.raises(PolicyDenied):
        service.gate_bundle(None, "ws_1", WorkspacePolicyDoc(require_approved_metrics=True), _bundle(_metric("breach_rate", "MAX(b)")))
    out = service.gate_bundle(None, "ws_1", WorkspacePolicyDoc(require_approved_metrics=False), _bundle(_metric("breach_rate", "MAX(b)")))
    assert out.metrics[0].status == "validated"  # policy off: published, but not presented as approved


def test_superset_marks_approved_metrics_certified():
    from analystos.publishing.superset import superset_metric

    approved = superset_metric(_metric("r", "AVG(b)").model_copy(update={"status": "approved", "format": "percent"}))
    assert json.loads(approved["extra"])["certification"]["certified_by"] == "AnalystOS semantic layer"
    assert approved["d3format"] == ".1%"
    assert "extra" not in superset_metric(_metric("r", "AVG(b)"))


def test_existing_policies_keep_publishing_new_workspaces_require_approval():
    assert WorkspacePolicyDoc.model_validate({}).require_approved_metrics is False
