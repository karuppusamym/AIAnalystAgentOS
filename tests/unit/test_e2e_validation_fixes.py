"""Gaps found by the end-to-end validation (2026-09-28, a retail workspace built from three CSV files on the compose
stack): container service URLs, Yes/No text flags, value lists that follow the data-samples policy, prices that are
averaged, key names files use, and domain-pack hints scoped to the workspace's data."""
from __future__ import annotations

import pytest

from analystos.capabilities import packs
from analystos.core.config import Settings, _to_service
from analystos.semantic.suggest import _is_per_unit
from analystos.skills import catalog as cat
from analystos.skills.relationships import _candidates


@pytest.mark.parametrize(("url", "service", "expected"), [
    ("redis://localhost:6379/0", "redis", "redis://redis:6379/0"),
    ("redis://:secret@127.0.0.1:6379/1", "redis", "redis://:secret@redis:6379/1"),
    ("http://localhost:8088", "superset", "http://superset:8088"),
    ("localhost:7233", "temporal", "temporal:7233"),
    ("redis://cache.internal:6379/0", "redis", "redis://cache.internal:6379/0"),
    ("not a url:port", "superset", "not a url:port"),
])
def test_a_loopback_service_url_names_the_compose_service(url, service, expected):
    assert _to_service(url, service) == expected


def test_only_a_compose_container_maps_loopback_urls():
    inside = Settings(in_container=True, profile="standard", redis_url="redis://localhost:6379/0")
    outside = Settings(profile="standard", redis_url="redis://localhost:6379/0")
    assert inside.redis_url == "redis://redis:6379/0" and outside.redis_url == "redis://localhost:6379/0"


@pytest.mark.parametrize(("values", "yes"), [(["No", "Yes"], "Yes"), (["N", "Y"], "Y"), (["true", "false"], "true"),
                                             (["Yes", "Maybe"], None), (["Yes"], None), (["A", "B"], None)])
def test_a_yes_no_text_column_is_a_flag(values, yes):
    assert cat.yes_no_flag({"values": values, "values_complete": True}) == yes


def test_value_lists_in_descriptions_follow_the_data_samples_policy():
    profile = {"non_null": 100, "null_count": 0, "distinct": 3, "values": ["Web", "Store", "Marketplace"], "values_complete": True}
    sem = {"semantic_role": "dimension"}
    assert "one of Web, Store, Marketplace" in cat.describe_column(sem, profile=profile)
    withheld = cat.describe_column(sem, profile=profile, values_allowed=False)
    assert "Web" not in withheld and "3 distinct values" in withheld


def test_a_text_flag_reports_its_yes_share():
    profile = {"non_null": 100, "null_count": 0, "distinct": 2, "values": ["No", "Yes"], "values_complete": True,
               "top_values": [{"value": "No", "count": 90}, {"value": "Yes", "count": 10}]}
    assert "yes in 10%" in cat.describe_column({"semantic_role": "flag", "flag_true": "Yes"}, profile=profile)


@pytest.mark.parametrize(("name", "per_unit"), [("unit_price", True), ("list_price", True), ("hourly_rate", True),
                                                ("unit_cost", True), ("amount", False), ("total_cost", False)])
def test_a_price_per_unit_is_averaged_not_summed(name, per_unit):
    assert _is_per_unit(name) is per_unit


def test_file_key_names_propose_joins_and_a_tables_own_key_does_not():
    assets = [{"asset": "s.orders", "columns": [{"name": "order_id"}, {"name": "customerid"}, {"name": "cust_no"},
                                                {"name": "valid"}]},
              {"asset": "s.customers", "columns": [{"name": "customerid"}, {"name": "cust_no"}, {"name": "name"}]}]
    found = {(c["from"]["asset"], c["col"]["name"], c["to"]["asset"]) for c in _candidates(assets)}
    assert found == {("s.orders", "customerid", "s.customers"), ("s.orders", "cust_no", "s.customers")}


def test_pack_hints_can_be_scoped_to_the_packs_that_fit_the_data():
    everything = packs.hints()
    with packs.only([]):
        assert packs.hints().key_columns == () and not packs.hints().domain_keywords
    with packs.only(None):
        assert packs.hints() == everything
    assert packs.hints() == everything


def test_a_note_after_a_url_in_the_env_file_is_ignored():
    s = Settings(in_container=True, profile="standard", superset_url="http://localhost:8088   (docker compose --profile bi up -d)")
    assert s.superset_url == "http://superset:8088"


def test_a_compared_value_that_differs_only_in_case_is_matched_to_the_data():
    from analystos.skills.literals import check_literals

    known = {"returned": {"No", "Yes"}}
    out = check_literals("SELECT COUNT(*) FILTER (WHERE o.returned = 'yes') FROM s.orders o", "postgres", known)
    assert out.rewrites == ["returned: 'yes' read as 'Yes'"] and "'Yes'" in out.sql
    same = "SELECT COUNT(*) FROM s.orders WHERE returned = 'Yes' AND region = 'west'"
    assert check_literals(same, "postgres", known).sql == same  # unknown columns and exact values are left alone


def test_a_compared_value_the_column_never_holds_is_refused_for_repair():
    from analystos.core.errors import SQLRejected
    from analystos.skills.literals import check_literals

    with pytest.raises(SQLRejected, match="'Y' is not a value of column returned"):
        check_literals("SELECT 1 FROM s.orders WHERE returned IN ('Y')", "postgres", {"returned": {"No", "Yes"}})


def test_an_ml_proposal_on_dated_file_rows_splits_by_time_and_keeps_identifiers_out():
    from analystos.ml.propose import propose

    cols = [{"name": "order_id", "data_type": "text", "semantic_type": "id", "distinct": 6000},
            {"name": "customer_id", "data_type": "text", "semantic_type": "id", "distinct": 400},
            {"name": "order_date", "data_type": "date", "semantic_type": "datetime", "distinct": 365},
            {"name": "quantity", "data_type": "integer", "semantic_type": "numeric", "distinct": 4},
            {"name": "sales_channel", "data_type": "text", "semantic_type": "categorical", "distinct": 3},
            {"name": "returned", "data_type": "text", "semantic_type": "categorical", "distinct": 2}]
    spec = propose(cols, asset="s.orders", row_count=6000, target="returned", task="classify")["proposal"]
    assert spec["time_column"] == "order_date" and spec["entity_keys"] == ["order_id"]
    assert {f["column"] for f in spec["features"]} == {"quantity", "sales_channel"}


def test_approving_a_kpi_with_its_publication_is_not_a_change_to_the_approved_bundle():
    from types import SimpleNamespace

    from analystos.agents.publisher import _comparable, pending_metrics

    proposed = {"datasets": [{"name": "d"}], "metrics": [{"name": "rate", "sql_expression": "AVG(x)", "status": "proposed"}]}
    approved = {"datasets": [{"name": "d"}], "metrics": [{"name": "rate", "sql_expression": "AVG(x)", "status": "approved",
                                                          "display_name": "Rate", "owner": "u1"}]}
    changed = {"datasets": [{"name": "d"}], "metrics": [{"name": "rate", "sql_expression": "SUM(x)", "status": "proposed"}]}
    assert _comparable(proposed) == _comparable(approved) and _comparable(proposed) != _comparable(changed)
    bundle = SimpleNamespace(metrics=[SimpleNamespace(name="rate", status="proposed"), SimpleNamespace(name="n", status="approved")])
    assert pending_metrics(SimpleNamespace(policy=SimpleNamespace(require_approved_metrics=True)), bundle) == ["rate"]
    assert pending_metrics(SimpleNamespace(policy=SimpleNamespace(require_approved_metrics=False)), bundle) == []
