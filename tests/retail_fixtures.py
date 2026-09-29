"""The retail journey's generated data (`scripts/e2e_full_journey.py`, the `random.Random(7)` block) as three
DuckDB tables, plus the catalog view the investigator gets from the crawler (semantic types from the
platform's profiler, roles from its column rules, measured relationships).

Planted truth (identical generator, same seed): late delivery 35% for customers in the West with
order_date >= 2026-07-01 vs 8% otherwise; returns 30% for Electronics sold on Marketplace vs 6% otherwise.
Region lives in `customers` and category in `products`: both causes are one join away from `orders`.
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any

SCHEMA = "shop"


def generate() -> dict[str, list[dict[str, Any]]]:
    """The generator of `scripts/e2e_full_journey.py`, verbatim in logic and seed (column names snake_case,
    as the file import stages them)."""
    rng = random.Random(7)
    regions, segments = ["North", "South", "East", "West"], ["Consumer", "Business"]
    cats = {"Electronics": (120, 900), "Home": (15, 200), "Clothing": (10, 120), "Sports": (20, 300)}
    channels = ["Web", "Store", "Marketplace"]
    customers = [{"customer_id": f"C{i:04d}", "customer_name": f"Customer {i}", "email": f"customer{i}@example.com",
                  "region": rng.choice(regions), "segment": rng.choice(segments),
                  "signup_date": date(2024, 1, 1) + timedelta(days=rng.randint(0, 500))} for i in range(1, 401)]
    products = []
    for i in range(1, 61):
        cat = list(cats)[i % 4]
        lo, hi = cats[cat]
        products.append({"product_id": f"P{i:03d}", "product_name": f"{cat} item {i}", "category": cat,
                         "list_price": round(rng.uniform(lo, hi), 2)})
    orders = []
    start = date(2025, 10, 1)
    for i in range(1, 6001):
        c, p = rng.choice(customers), rng.choice(products)
        od = start + timedelta(days=rng.randint(0, 364))
        ch = rng.choices(channels, weights=[5, 3, 2])[0]
        promised = od + timedelta(days=5)
        late_p = 0.35 if (c["region"] == "West" and od >= date(2026, 7, 1)) else 0.08
        delivered = promised + timedelta(days=rng.randint(1, 6)) if rng.random() < late_p else promised - timedelta(days=rng.randint(0, 3))
        ret_p = 0.30 if (p["category"] == "Electronics" and ch == "Marketplace") else 0.06
        qty = rng.randint(1, 4)
        disc = rng.choice([0, 0, 0, 5, 10, 15])
        orders.append({"order_id": f"O{i:05d}", "customer_id": c["customer_id"], "product_id": p["product_id"],
                       "order_date": od, "quantity": qty, "unit_price": p["list_price"], "discount_pct": disc,
                       "sales_channel": ch, "status": rng.choices(["Delivered", "Cancelled"], weights=[95, 5])[0],
                       "promised_date": promised, "delivered_date": delivered,
                       "returned": "Yes" if rng.random() < ret_p else "No"})
    return {"customers": customers, "products": products, "orders": orders}


def truth(data: dict[str, list[dict[str, Any]]]) -> dict[str, float]:
    cust = {c["customer_id"]: c for c in data["customers"]}
    prod = {p["product_id"]: p for p in data["products"]}
    orders = data["orders"]

    def rate(rows: list[dict[str, Any]], pred) -> float:
        return sum(1 for o in rows if pred(o)) / len(rows)

    west = [o for o in orders if cust[o["customer_id"]]["region"] == "West"]
    rest = [o for o in orders if cust[o["customer_id"]]["region"] != "West"]
    elec = [o for o in orders if prod[o["product_id"]]["category"] == "Electronics"]
    other = [o for o in orders if prod[o["product_id"]]["category"] != "Electronics"]
    late = lambda o: o["delivered_date"] > o["promised_date"]  # noqa: E731
    ret = lambda o: o["returned"] == "Yes"  # noqa: E731
    return {"late_west": rate(west, late), "late_other": rate(rest, late),
            "returned_electronics": rate(elec, ret), "returned_other": rate(other, ret), "orders": len(orders)}


_DUCK_TYPES = {str: "VARCHAR", int: "BIGINT", float: "DOUBLE", date: "DATE"}


def column_types(rows: list[dict[str, Any]]) -> dict[str, str]:
    return {k: _DUCK_TYPES[type(v)] for k, v in rows[0].items()}


def load_duckdb(con: Any, data: dict[str, list[dict[str, Any]]], schema: str = SCHEMA) -> None:
    con.execute(f'CREATE SCHEMA "{schema}"')
    for name, rows in data.items():
        types = column_types(rows)
        con.execute(f'CREATE TABLE "{schema}"."{name}" ({", ".join(f"{chr(34)}{c}{chr(34)} {t}" for c, t in types.items())})')
        con.executemany(f'INSERT INTO "{schema}"."{name}" VALUES ({", ".join("?" for _ in types)})',
                        [list(r.values()) for r in rows])


def load_postgres(conn: Any, data: dict[str, list[dict[str, Any]]], schema: str = SCHEMA) -> None:
    pg = {"VARCHAR": "TEXT", "BIGINT": "BIGINT", "DOUBLE": "DOUBLE PRECISION", "DATE": "DATE"}
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        for name, rows in data.items():
            types = column_types(rows)
            cur.execute(f'CREATE TABLE "{schema}"."{name}" ({", ".join(f"{chr(34)}{c}{chr(34)} {pg[t]}" for c, t in types.items())})')
            with cur.copy(f'COPY "{schema}"."{name}" ({", ".join(chr(34) + c + chr(34) for c in types)}) FROM STDIN') as cp:
                for r in rows:
                    cp.write_row(list(r.values()))
    conn.commit()


def catalog(run_sql: Any, schema: str = SCHEMA) -> dict[str, Any]:
    """What the crawler records, computed by the platform's own skills through `run_sql`: per table the
    engine columns (`tmpl.Col`: profiler semantic type, rule role, profile, Yes/No flag value), the scope
    columns, the semantic types, and the measured many-to-one relationships as `Lookup`s."""
    from analystos.connectors.base import DiscoveredColumn
    from analystos.skills import catalog as cat
    from analystos.skills import hypothesis_templates as tmpl
    from analystos.skills.lookups import Lookup
    from analystos.skills.profiling import profile_asset
    from analystos.skills.relationships import discover_relationships

    tables = {name: column_types(rows) for name, rows in generate().items()}
    cols: dict[str, list[tmpl.Col]] = {}
    types: dict[str, dict[str, str]] = {}
    assets = []
    for name, ctypes in tables.items():
        fq = f"{schema}.{name}"
        columns = [{"name": c, "data_type": t} for c, t in ctypes.items()]
        prof = profile_asset(run_sql, fq, columns)
        out = []
        for c in columns:
            p = prof.column(c["name"]).model_dump(mode="json")
            role = cat.infer_column_semantics(DiscoveredColumn(name=c["name"], data_type=c["data_type"])).semantic_role
            yes = cat.yes_no_flag(p) if c["data_type"] == "VARCHAR" else None
            out.append(tmpl.Col(name=c["name"], semantic_type=p["semantic_type"], role="flag" if yes else role, profile=p,
                                flag_true=yes))
        cols[fq] = out
        types[fq] = {c.name: c.semantic_type for c in out}
        assets.append({"asset": fq, "columns": columns})
    rels = discover_relationships(run_sql, assets)
    lookups = [Lookup(r.from_asset, r.from_column, r.to_asset, r.to_column) for r in rels if r.cardinality == "many_to_one"]
    return {"cols": cols, "types": types, "lookups": lookups, "relationships": rels,
            "columns": {fq: [c.name for c in cs] for fq, cs in cols.items()}}
