"""Fixes found reviewing a downloaded workspace context (2026-09-28): a transactional table with display-name
columns is not a dimension, a custom-table prefix is not part of the entity, and a text column that names who owns
or did the work holds personal names."""
from __future__ import annotations

import pytest

from analystos.connectors.base import DiscoveredAsset, DiscoveredColumn
from analystos.skills import catalog as cat


def col(name: str, data_type: str = "text", **kw) -> DiscoveredColumn:
    return DiscoveredColumn(name=name, data_type=data_type, **kw)


def asset(name: str, cols: list[DiscoveredColumn]) -> DiscoveredAsset:
    return DiscoveredAsset(source_name=name, name=name, schema_name="src", columns=cols)


def test_a_transactional_table_with_display_name_columns_is_a_fact_not_a_dimension():
    change = asset("change_request", [
        col("sys_id", is_key=True), col("number"), col("type"), col("risk", "integer"), col("state", "integer"),
        col("start_date", "timestamp"), col("assignment_group", references="sys_user_group.sys_id"),
        col("assignment_group_name"), col("cmdb_ci", references="cmdb_ci.sys_id"), col("cmdb_ci_name"),
        col("close_code"), col("short_description"), col("sys_updated_on", "timestamp")])
    task = asset("sc_task", [
        col("sys_id", is_key=True), col("number"), col("short_description"), col("state", "integer"),
        col("assignment_group", references="sys_user_group.sys_id"), col("assignment_group_name"),
        col("assigned_to", references="sys_user.sys_id"), col("assigned_to_name"), col("opened_at", "timestamp"),
        col("reassignment_count", "integer")])
    assert cat.infer_table_semantics(change).role == "fact"
    assert cat.infer_table_semantics(task).role == "fact"


def test_a_table_with_its_own_name_column_is_still_a_dimension():
    customers = asset("dim_customer", [
        col("customer_key", "integer", is_key=True), col("customer_name"), col("city"), col("country_code"),
        col("segment"), col("created_at", "timestamp")])
    assert cat.infer_table_semantics(customers).role == "dimension"


@pytest.mark.parametrize(("table", "entity"), [
    ("u_task_activity", "task activity"), ("u_task_activity_cases", "task activity case"),
    ("dim_customers", "customer"), ("x_order", "order"), ("incident", "incident"), ("a", "a")])
def test_a_leading_single_letter_custom_prefix_is_not_part_of_the_entity(table, entity):
    assert cat._entity_from_table(table) == entity


@pytest.mark.parametrize("name", ["assigned_to", "opened_by", "sys_created_by", "requester", "resolved_by", "closed_by_name"])
def test_text_columns_that_name_who_did_or_owns_the_work_hold_personal_names(name):
    r = cat.classify_pii(name, "text")
    assert r.category == "person_name" and r.sensitivity == "confidential"


@pytest.mark.parametrize("name", ["assignee_id", "caller_id", "assigned_to_group", "assignment_group", "approved_by_date",
                                  "task_type", "manager", "resolved_by_count"])
def test_references_groups_dates_and_counts_are_not_personal_names(name):
    assert cat.classify_pii(name, "text").category is None


def test_a_declared_reference_or_a_number_is_a_key_not_a_name():
    assert cat.classify_pii("assigned_to", "text", references=True).category is None
    assert cat.classify_pii("assigned_to", "integer").category is None
    assert cat.classify_pii("opened_by", "bigint").category is None
