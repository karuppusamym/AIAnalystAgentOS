"""Data shape (services/data_shape.py): patterns per table from the catalog's own measurements, the workspace's shape from
roles and joins, targets an experiment could predict, and the model's event-log proposal verified in code."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from analystos.db.models import SourceAsset, SourceColumn
from analystos.services import data_shape as ds


def col(name, dtype="text", role=None, *, key=False, tags=None, **profile) -> SourceColumn:
    return SourceColumn(name=name, data_type=dtype, is_key=key, tags=tags or [], profile=profile, semantics={"semantic_role": role})


def asset(name, rows=1000, role="fact") -> SourceAsset:
    return SourceAsset(id=f"a_{name}", name=name, schema_name="s", row_count=rows, semantics={"role": role})


def view(role="fact", time_column=None, measures=(), grain=None):
    return {"role": role, "time_column": time_column, "measures": list(measures), "grain": grain}


ORDERS = [col("order_id", "bigint", "identifier", key=True), col("customer_id", "bigint", "foreign_key"),
          col("amount", "double", "amount", distinct=900, null_rate=0.0), col("qty", "bigint", "measure", distinct=9),
          col("channel", "text", "dimension", distinct=4), col("late", "boolean", "flag", true_count=300, non_null=1000),
          col("ordered_at", "timestamp", "timestamp")]


def kinds(patterns):
    return [p["kind"] for p in patterns]


def test_a_fact_table_with_a_time_column_and_a_flag_is_a_trend_and_an_ml_candidate():
    p = ds.table_patterns(asset("orders"), ORDERS, view("fact", "ordered_at", ["amount", "qty"]), None, linked=2)
    assert set(kinds(p)) == {"fact", "time_series", "ml_candidate"}
    ml = next(x for x in p if x["kind"] == "ml_candidate")
    assert ("late", "classification") in {(t["column"], t["kind"]) for t in ml["detail"]["targets"]}
    assert ("amount", "regression") in {(t["column"], t["kind"]) for t in ml["detail"]["targets"]}
    assert all(x["next_step"] for x in p if x["kind"] != "dimension") and p[0]["confidence"] >= p[-1]["confidence"]


def test_too_few_rows_or_a_constant_flag_is_not_an_ml_candidate():
    assert "ml_candidate" not in kinds(ds.table_patterns(asset("orders", rows=50), ORDERS, view("fact", "ordered_at", ["amount"]), None, 0))
    constant = [c if c.name != "late" else col("late", "boolean", "flag", true_count=1000, non_null=1000) for c in ORDERS]
    targets = ds._targets(constant, 1000)
    assert "late" not in {t["column"] for t in targets}


def test_sensitive_and_key_columns_are_never_targets_or_features():
    cols = [*ORDERS, col("salary", "double", "amount", tags=["pii"]), col("ref", "text", "dimension", distinct=3, key=True)]
    assert "salary" not in {t["column"] for t in ds._targets(cols, 1000)} and "salary" not in ds._features(cols)
    assert "ref" not in {t["column"] for t in ds._targets(cols, 1000)}


def test_an_event_log_table_is_detected_from_names_and_profile():
    log = [col("task_ref", "text", "identifier", distinct=40), col("activity", "text", "dimension", distinct=6),
           col("activity_at", "timestamp", "timestamp"), col("sys_id", "text", "identifier", key=True, distinct=400)]
    p = ds.table_patterns(asset("task_activity", rows=400, role="event"), log, view("event"), None, 0)
    assert p[0]["kind"] == "event_log" and p[0]["detail"]["mapping"]["case_column"] == "task_ref" and p[0]["next_step"]["action"] == "process_analysis"


def test_columns_the_caller_may_not_read_are_not_used():
    p = ds.table_patterns(asset("orders"), ORDERS, view("fact", "ordered_at", ["amount"]), {"order_id", "amount", "ordered_at"}, 0)
    assert "ml_candidate" not in kinds(p)  # too few readable columns


def tbl(id_, name, role):
    return {"asset_id": id_, "name": name, "role": role}


def rel(a, b):
    return {"from": {"asset_id": a}, "to": {"asset_id": b}}


@pytest.mark.parametrize(("tables", "rels", "kind"), [
    ([tbl("o", "orders", "fact"), tbl("c", "customers", "dimension")], [rel("o", "c")], "star"),
    ([tbl("a", "a", "unknown"), tbl("b", "b", "unknown"), tbl("c", "c", "unknown")], [rel("a", "b"), rel("b", "c")], "normalized"),
    ([tbl("a", "a", "fact")], [], "flat"),
    ([tbl("a", "a", "fact"), tbl("b", "b", "fact")], [], "unlinked")])
def test_the_workspace_shape_reads_roles_and_joins(tables, rels, kind):
    assert [s["kind"] for s in ds.workspace_shape(tables, rels)] == [kind]


def test_an_empty_workspace_has_no_shape():
    assert ds.workspace_shape([], []) == []


def _cols(**over):
    base = {"case": col("case_ref", "text", "dimension", distinct=50), "act": col("step", "text", "dimension", distinct=8),
            "ts": col("happened", "timestamp", "timestamp"), "res": col("who", "text", "dimension", distinct=5)}
    base.update(over)
    return {c.name: c for c in base.values()}


PROPOSAL = {"case_column": "case_ref", "activity_column": "step", "timestamp_column": "happened", "resource_column": "who"}


def test_a_proposed_event_log_is_verified_against_the_profile():
    ok, _ = ds._verify_event_log(PROPOSAL, _cols(), 500)
    assert ok == PROPOSAL
    assert ds._verify_event_log({**PROPOSAL, "case_column": "nope"}, _cols(), 500)[0] is None
    assert ds._verify_event_log({**PROPOSAL, "timestamp_column": "step"}, _cols(), 500)[0] is None  # not a time type
    assert ds._verify_event_log(PROPOSAL, _cols(act=col("step", "text", distinct=900)), 5000)[0] is None  # too many activities
    assert ds._verify_event_log(PROPOSAL, _cols(act=col("step", "text")), 500)[0] is None  # not profiled
    assert ds._verify_event_log(PROPOSAL, _cols(case=col("case_ref", "text", distinct=500)), 500)[0] is None  # one event per case
    assert ds._verify_event_log(PROPOSAL, _cols(case=col("case_ref", "text", tags=["pii"], distinct=50)), 500)[0] is None
    assert ds._verify_event_log({**PROPOSAL, "resource_column": "ghost"}, _cols(), 500)[0]["resource_column"] is None


def test_the_model_purpose_is_registered_deterministic_first():
    from analystos.agents.prompts import prompt
    from analystos.contracts.platform import DETERMINISTIC_CAPABLE, LLMSettings
    from analystos.llm.router import load_models_config

    cfg = load_models_config()
    assert cfg.ladders[ds.PURPOSE] == ["cache", "rules", "llm_small"] and cfg.routing[ds.PURPOSE] == "low_cost"
    assert ds.PURPOSE in DETERMINISTIC_CAPABLE and ds.PURPOSE in LLMSettings().cacheable_purposes
    assert "untrusted" in prompt(ds.PROMPT).lower()


def test_marks_reject_unknown_patterns_and_decisions():
    from analystos.core.errors import InvalidInput

    with pytest.raises(InvalidInput):
        ds.mark(SimpleNamespace(), "ws", "a", "olap", "confirm", "u")
    with pytest.raises(InvalidInput):
        ds.mark(SimpleNamespace(), "ws", "a", "event_log", "maybe", "u")
