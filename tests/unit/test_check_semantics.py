"""P7-02 / ADR-0019 §6: `analystos check-semantics` validates model files offline (names, expressions
parse for their dialect and read their dataset's columns, relationship references, cycles, fan-out),
and the same definition checks gate approval (P4-05)."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from analystos.cli import main as cli
from analystos.contracts.semantic import SemanticMetricDef
from analystos.semantic import service
from analystos.semantic.check import check_document, check_paths
from analystos.semantic.review import definition_problems

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "semantic" / "sales_governed.ossie.yaml"


@pytest.fixture
def doc():
    return yaml.safe_load(FIXTURE.read_text())


def _ext(obj: dict) -> dict:
    return json.loads(next(x for x in obj["custom_extensions"] if x["vendor_name"] == "COMMON")["data"])["analystos"]


def _set_ext(obj: dict, **fields) -> None:
    for x in obj["custom_extensions"]:
        if x["vendor_name"] == "COMMON":
            x["data"] = json.dumps({"analystos": {**_ext(obj), **fields}}, sort_keys=True)


def _model(doc):
    return doc["semantic_model"][0]


def _metric(doc, name):
    return next(m for m in _model(doc)["metrics"] if m["name"] == name)


def test_the_committed_governed_model_passes(doc):
    assert check_document(doc) == []
    assert cli(["check-semantics", str(FIXTURE.parent)]) == 0


def test_expression_must_parse_and_read_only_its_dataset(doc):
    bad = deepcopy(doc)
    _metric(bad, "revenue")["expression"]["dialects"][0]["expression"] = "SUM(discount)"
    _metric(bad, "orders_placed")["expression"]["dialects"][0]["expression"] = "COUNT(*"
    problems = check_document(bad)
    assert any("revenue" in p and "discount" in p for p in problems)
    assert any("orders_placed" in p for p in problems)


def test_names_references_and_cycles(doc):
    bad = deepcopy(doc)
    _model(bad)["datasets"][0]["fields"][0]["name"] = "order id"
    rel = _model(bad)["relationships"][0]
    rel["from_columns"] = ["customer_ref"]
    _model(bad)["relationships"].append({"name": "customers__orders_by_country", "from": "customers", "to": "orders",
                                         "from_columns": ["country"], "to_columns": ["sales_region"]})
    problems = check_document(bad)
    assert any(p.startswith("[Name]") and "order id" in p for p in problems)
    assert any(p.startswith("[Reference]") and "customer_ref" in p for p in problems)
    assert any(p.startswith("[Cycle]") and "customers__orders_by_country" in p for p in problems)


def test_fan_out_without_pre_aggregation_and_unvalidated_joins_are_problems(doc):
    bad = deepcopy(doc)
    _set_ext(_metric(bad, "revenue"), pre_aggregations=[])
    _set_ext(_model(bad)["relationships"][0], validated_at=None, validated_by=None)
    problems = check_document(bad)
    assert any("order_lines__orders__order_id" in p and "pre_aggregations" in p for p in problems)
    assert any("customers.customer_segment" in p and "no validated cardinality" in p for p in problems)


def test_a_non_ossie_file_named_explicitly_fails_and_directories_skip_it(tmp_path):
    (tmp_path / "notes.yaml").write_text("hello: world\n")
    assert check_paths([str(tmp_path)]) == {}
    assert check_paths([str(tmp_path / "notes.yaml")])[str(tmp_path / "notes.yaml")]
    assert cli(["check-semantics", str(tmp_path / "notes.yaml")]) == 1
    assert cli(["check-semantics", str(tmp_path)]) == 2  # nothing to check is not a pass


def _with(defn: SemanticMetricDef, changes: dict) -> SemanticMetricDef:
    return SemanticMetricDef.model_validate({**defn.model_dump(), **changes})


def test_definition_problems_gate_approval():
    datasets = [{"name": "orders", "source": "SELECT amount, region FROM s.orders", "fields": []}]
    ok = SemanticMetricDef(name="revenue", expressions=[{"expression": "SUM(amount)"}], dataset="orders", filters=["region <> 'x'"])
    assert definition_problems(ok, datasets, []) == []
    for defn, expected in [
        (_with(ok, {"expressions": [{"dialect": "TABLEAU", "expression": "SUM([amount])"}]}), "not SQL"),
        (_with(ok, {"filters": ["SUM(amount) > 1"]}), "row condition"),
        (_with(ok, {"dataset": "missing"}), "not in the semantic model"),
        (_with(ok, {"expressions": [{"expression": "SUM(secret)"}]}), "secret"),
        (_with(ok, {"dimensions": ["country"]}), "not a field"),
        (_with(ok, {"pre_aggregations": ["nope"]}), "not a relationship"),
    ]:
        assert any(expected in p for p in definition_problems(defn, datasets, [])), expected


def test_approval_refuses_a_definition_that_does_not_hold(monkeypatch):
    row = SimpleNamespace(workspace_id="w", name="revenue", version=2, definition={
        "name": "revenue", "expressions": [{"expression": "SUM(secret)"}], "dataset": "orders"})
    model = SimpleNamespace(datasets=[{"name": "orders", "source": "s.orders", "fields": [{"name": "amount"}]}], relationships=[])
    monkeypatch.setattr(service, "current_model", lambda *_: model)
    from analystos.core.errors import InvalidInput

    with pytest.raises(InvalidInput, match="cannot be approved") as refused:
        service._check_definition(None, row)
    assert refused.value.details["problems"][0]["message"].startswith("references secret")


def test_denominator_mismatch_is_a_conflict(monkeypatch):
    def row(name, expr, version=1):
        return SimpleNamespace(name=name, version=version, status="approved", expression=expr, workspace_id="w",
                               normalized_expression=expr.lower(),
                               definition={"name": name, "expressions": [{"expression": expr}], "dataset": "tickets"})

    rows = [row("resolution_rate", "SUM(resolved) / COUNT(*)"), row("resolved_share", "SUM(resolved) / COUNT(opened_at)"),
            row("same_rate", "SUM(resolved) / COUNT(*)"), row("volume", "COUNT(*)")]
    monkeypatch.setattr(service, "metric_rows", lambda *a, **k: rows)
    found = [c for c in service.conflicts(None, "w") if c.kind == "denominator_mismatch"]
    assert len(found) == 1 and found[0].names == ["resolution_rate", "resolved_share", "same_rate"]
    assert {m["denominator"] for m in found[0].metrics} == {"count(*)", "count(opened_at)"}
