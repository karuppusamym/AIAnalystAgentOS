"""Relationship discovery: declared ServiceNow references, x_id heuristics, SQL validation."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.skills.relationships import discover_relationships  # noqa: E402
from skills_fixtures import INCIDENT_COLUMNS, TsqlViaDuckRunSQL, duck_dataset  # noqa: E402

GROUP = {"asset": "itsm.sys_user_group", "columns": [{"name": "sys_id", "data_type": "VARCHAR", "is_key": True},
                                                      {"name": "name", "data_type": "VARCHAR"}]}
CUSTOMER = {"asset": "crm.customer", "columns": [{"name": "id", "data_type": "BIGINT", "is_key": True},
                                                  {"name": "name", "data_type": "VARCHAR"}]}
ORDERS = {"asset": "crm.orders", "columns": [{"name": "id", "data_type": "BIGINT", "is_key": True},
                                              {"name": "customer_id", "data_type": "BIGINT"},
                                              {"name": "amount", "data_type": "DOUBLE"},
                                              {"name": "unrelated_code", "data_type": "BIGINT"}]}
INCIDENT = {"asset": "itsm.incident", "columns": INCIDENT_COLUMNS}


@pytest.fixture(scope="module")
def duck():
    return duck_dataset()


def test_detects_declared_and_heuristic_fks(duck):
    rels = discover_relationships(duck, [INCIDENT, GROUP, CUSTOMER, ORDERS])
    by = {(r.from_asset, r.from_column): r for r in rels}
    ag = by[("itsm.incident", "assignment_group")]
    assert (ag.to_asset, ag.to_column, ag.cardinality) == ("itsm.sys_user_group", "sys_id", "many_to_one")
    assert ag.evidence["source"] == "declared" and ag.evidence["orphan_rows"] == 5
    assert ag.evidence["containment"] == pytest.approx(5995 / 6000)
    oc = by[("crm.orders", "customer_id")]
    assert (oc.to_asset, oc.to_column, oc.cardinality) == ("crm.customer", "id", "many_to_one")
    assert oc.evidence["source"] == "name_heuristic" and oc.evidence["containment"] == 1.0
    assert oc.confidence > 0.8
    assert ("crm.orders", "unrelated_code") not in by
    assert len(rels) == 2


def test_only_given_assets_are_targets(duck):
    rels = discover_relationships(duck, [INCIDENT, ORDERS])
    assert rels == []  # sys_user_group and customer not in the list


def test_declared_reference_forms_and_low_containment(duck):
    inc = {"asset": "itsm.incident", "columns": [
        {"name": "assignment_group", "data_type": "VARCHAR", "references": "itsm.sys_user_group.sys_id"},
        {"name": "app", "data_type": "VARCHAR", "references": "sys_user_group"}]}
    rels = discover_relationships(duck, [inc, GROUP])
    by = {r.from_column: r for r in rels}
    assert by["assignment_group"].to_column == "sys_id"
    # declared but broken: kept, with zero containment and low confidence
    assert by["app"].evidence["containment"] == 0.0 and by["app"].confidence < 0.5


def test_same_name_key_and_type_cast(duck):
    duck.con.execute("CREATE TABLE crm.customer_txt AS SELECT CAST(id AS VARCHAR) AS customer_id, name FROM crm.customer")
    ct = {"asset": "crm.customer_txt", "columns": [{"name": "customer_id", "data_type": "VARCHAR", "is_key": True}]}
    rels = discover_relationships(duck, [ORDERS, ct])
    r = next(r for r in rels if r.to_asset == "crm.customer_txt")
    assert r.evidence["source"] == "same_name_key" and r.evidence["type_cast"] is True and r.evidence["containment"] == 1.0


def test_tsql_relationship_sql_runs(duck):
    rels = discover_relationships(TsqlViaDuckRunSQL(duck), [INCIDENT, GROUP, CUSTOMER, ORDERS])
    assert {(r.from_column, r.to_column) for r in rels} == {("assignment_group", "sys_id"), ("customer_id", "id")}
