"""A small order/customer fixture and a recipe that uses every node type (P6-04..P6-07 tests)."""
from __future__ import annotations

import copy
from typing import Any

SCHEMA = "src_fx"
CUSTOMERS = {
    "columns": ["customer_id", "name", "region", "signup_date"],
    "types": ["integer", "text", "text", "date"],
    "rows": [
        [1, "Ada", "north", "2024-01-03"],
        [2, "Bo", "south", "2024-02-11"],
        [3, "Cy", "north", "2024-03-05"],
        [4, "Di", None, "2024-03-09"],
    ],
}
ORDERS = {
    "columns": ["order_id", "customer_id", "order_date", "amount", "status"],
    "types": ["integer", "integer", "date", "numeric(10,2)", "text"],
    "rows": [
        [10, 1, "2024-04-01", 120.5, "paid"],
        [11, 1, "2024-04-02", 80.0, "paid"],
        [11, 1, "2024-04-02", 80.0, "paid"],  # a duplicate delivery of order 11
        [12, 2, "2024-04-03", 40.25, "refunded"],
        [13, 3, "2024-04-03", 300.0, "paid"],
        [14, 3, "2024-04-05", 15.75, "paid"],
        [15, 9, "2024-04-06", 99.0, "paid"],  # customer 9 is unknown
        [16, 2, "2024-04-07", None, "pending"],
    ],
}
LEGACY_ORDERS = {
    "columns": ["order_id", "customer_id", "order_date", "amount", "status"],
    "types": ["integer", "integer", "date", "numeric(10,2)", "text"],
    "rows": [[1, 4, "2023-12-30", 60.0, "paid"], [2, 2, "2023-12-31", 10.0, "paid"]],
}
TABLES = {f"{SCHEMA}.customers": CUSTOMERS, f"{SCHEMA}.orders": ORDERS, f"{SCHEMA}.legacy_orders": LEGACY_ORDERS}


def _schema(t: dict[str, Any]) -> list[dict[str, str]]:
    return [{"name": c, "type": ty} for c, ty in zip(t["columns"], t["types"], strict=True)]


RECIPE: dict[str, Any] = {
    "kind": "Recipe",
    "name": "customer_orders",
    "description": "paid order value per customer and region",
    "nodes": [
        {"op": "source", "id": "orders", "asset": f"{SCHEMA}.orders", "schema": _schema(ORDERS)},
        {"op": "source", "id": "legacy", "asset": f"{SCHEMA}.legacy_orders", "schema": _schema(LEGACY_ORDERS)},
        {"op": "source", "id": "customers", "asset": f"{SCHEMA}.customers", "schema": _schema(CUSTOMERS)},
        {"op": "union", "id": "all_orders", "inputs": ["orders", "legacy"]},
        {"op": "dedupe", "id": "unique_orders", "input": "all_orders", "keys": ["order_id"],
         "order": [{"column": "order_date", "desc": True}]},
        {"op": "filter", "id": "paid", "input": "unique_orders",
         "predicate": "status = 'paid' AND order_date >= CAST('2023-12-31' AS DATE)"},
        {"op": "derive", "id": "valued", "input": "paid", "columns": [
            {"name": "amount_eur", "expr": "CAST(amount * 0.9 AS DOUBLE)", "type": "double"},
            {"name": "order_month", "expr": "DATE_TRUNC('month', order_date)", "type": "date"}]},
        {"op": "rename", "id": "renamed_customers", "input": "customers", "mapping": {"name": "customer_name"}},
        {"op": "join", "id": "with_customer", "left": "valued", "right": "renamed_customers", "how": "left",
         "on": [{"left": "customer_id", "right": "customer_id"}], "expected_cardinality": "many_to_one"},
        {"op": "window", "id": "ranked", "input": "with_customer", "columns": [
            {"name": "order_rank", "func": "row_number", "partition_by": ["customer_id"],
             "order_by": [{"column": "amount_eur", "desc": True}, {"column": "order_id"}], "type": "bigint"}]},
        {"op": "aggregate", "id": "per_customer", "input": "ranked", "keys": ["customer_id", "region"], "measures": [
            {"name": "orders", "func": "count", "type": "bigint"},
            {"name": "revenue_eur", "func": "sum", "column": "amount_eur", "type": "double"},
            {"name": "best_rank", "func": "min", "column": "order_rank", "type": "bigint"}]},
        {"op": "cast", "id": "typed", "input": "per_customer", "casts": {"orders": "integer"}},
        {"op": "output", "id": "out", "input": "typed", "name": "customer_revenue", "grain": ["customer_id"],
         "keys": ["customer_id"], "schema_policy": "warn",
         "schema": [{"name": "customer_id", "type": "integer"}, {"name": "region", "type": "text"},
                    {"name": "orders", "type": "integer"}, {"name": "revenue_eur", "type": "double"},
                    {"name": "best_rank", "type": "bigint"}],
         "gates": [{"type": "accepted_values", "column": "region", "values": ["north", "south"], "severity": "warn"},
                   {"type": "range", "column": "revenue_eur", "min": 0, "severity": "fail"}]},
    ],
}

# Expected result, computed by hand from the fixture: paid orders after dedupe (11 once), legacy order 2
# (2023-12-31) included, order 1 (2023-12-30) excluded; customer 9 has no customer row (left join).
EXPECTED = {
    1: ["north", 2, (120.5 + 80.0) * 0.9, 1],
    2: ["south", 1, 10.0 * 0.9, 1],
    3: ["north", 2, (300.0 + 15.75) * 0.9, 1],
    9: [None, 1, 99.0 * 0.9, 1],
}


def recipe(**changes: Any) -> dict[str, Any]:
    out = copy.deepcopy(RECIPE)
    out.update(changes)
    return out


def scope_for(tables: dict[str, dict[str, Any]] = TABLES, source_id: str = "src_fx_1", dialect: str = "postgres"):
    from analystos.contracts.policy import DataScope

    return DataScope(workspace_id="ws_fx", user_id="u", role="analyst", source_ids=[source_id], assets=sorted(tables),
                     asset_sources={a: source_id for a in tables}, columns={a: t["columns"] for a, t in tables.items()},
                     source_dialects={source_id: dialect})
