"""Seeded retail benchmark for pack.sales: orders with planted effects and a null control.

Planted (see benchmark.yaml): marketplace orders are returned ~3x as often (18% vs 6%); Enterprise
customers' order value is ~2.1x Consumer; APAC orders ship ~3 days slower. Null control: the
payment method has no effect on anything.

`shop_tables` returns the rows; `build_shop_db` writes them as the SQLite file the live evidence
script (scripts/e2e_increment3.py) uploads. Same seed, same data, in both.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

SEED = 20260925
SCHEMA = {
    "region": [("id", "INTEGER PRIMARY KEY"), ("name", "VARCHAR(40) NOT NULL"), ("country", "VARCHAR(60)")],
    "product": [("id", "INTEGER PRIMARY KEY"), ("name", "VARCHAR(80)"), ("category", "VARCHAR(40)"), ("list_price", "NUMERIC(10,2)")],
    "customer": [("id", "INTEGER PRIMARY KEY"), ("customer_name", "VARCHAR(80)"), ("email", "VARCHAR(120)"),
                 ("segment", "VARCHAR(30)"), ("region_id", "INTEGER REFERENCES region(id)"), ("signup_date", "DATE")],
    "orders": [("order_id", "INTEGER PRIMARY KEY"), ("customer_id", "INTEGER REFERENCES customer(id)"),
               ("product_id", "INTEGER REFERENCES product(id)"), ("order_date", "DATE"), ("channel", "VARCHAR(20)"),
               ("sales_region", "VARCHAR(40)"), ("customer_segment", "VARCHAR(30)"), ("quantity", "INTEGER"),
               ("discount_pct", "NUMERIC(4,2)"), ("net_amount", "NUMERIC(12,2)"), ("shipping_days", "INTEGER"),
               ("returned", "BOOLEAN"), ("payment_method", "VARCHAR(20)")],
}


def shop_tables(*, drift: bool = False, seed: int = SEED, n_orders: int = 24000) -> dict[str, dict[str, Any]]:
    """{table: {"columns": [(name, sql_type)], "rows": [tuple]}}; `drift=True` adds orders.coupon_code."""
    rng = np.random.default_rng(seed)
    regions = [(1, "EMEA", "Germany"), (2, "North America", "United States"), (3, "APAC", "Singapore"), (4, "LATAM", "Brazil")]
    categories = ["Electronics", "Home", "Apparel", "Beauty", "Sports"]
    products = [(i + 1, f"Product {i + 1:03d}", categories[i % 5], round(float(rng.uniform(8, 400)), 2)) for i in range(60)]
    segments = ["Consumer", "Small Business", "Enterprise"]
    customers = []
    for i in range(2500):
        first, last = rng.choice(["Ana", "Ben", "Chen", "Dara", "Eli", "Fay", "Gus", "Hana"]), rng.choice(["Lee", "Ng", "Diaz", "Khan", "Moss"])
        customers.append((i + 1, f"{first} {last}", f"{first.lower()}.{last.lower()}{i}@example.com",
                          str(rng.choice(segments, p=[0.6, 0.3, 0.1])), int(rng.integers(1, 5)),
                          str(date(2022, 1, 1) + timedelta(days=int(rng.integers(0, 900))))))
    channels, payments = ["web", "mobile", "marketplace", "store"], ["card", "paypal", "invoice", "gift_card"]
    orders = []
    start = date(2024, 1, 1)
    for i in range(n_orders):
        c = customers[int(rng.integers(0, len(customers)))]
        p = products[int(rng.integers(0, len(products)))]
        day = int(min(max(rng.normal(300, 190), 0), 630))  # growth + a little seasonality through density
        channel = str(rng.choice(channels, p=[0.4, 0.3, 0.2, 0.1]))
        qty = int(rng.integers(1, 6))
        discount = float(rng.choice([0, 0, 0.05, 0.1, 0.2]))
        seg_mult = {"Consumer": 1.0, "Small Business": 1.3, "Enterprise": 2.1}[c[3]]  # planted: Enterprise ~2x order value
        net = round(p[3] * qty * (1 - discount) * seg_mult * float(rng.uniform(0.9, 1.1)), 2)
        region = next(r for r in regions if r[0] == c[4])[1]
        ship = max(1, int(round(rng.normal(4.0 + (3.0 if region == "APAC" else 0.0), 1.2))))  # planted: APAC +3 days
        ret_p = 0.18 if channel == "marketplace" else 0.06  # planted: marketplace ~3x returns
        returned = int(rng.random() < ret_p)
        payment = str(rng.choice(payments))  # null control: no effect
        row = (i + 1, c[0], p[0], str(start + timedelta(days=day)), channel, region, c[3], qty, discount, net, ship, returned, payment)
        orders.append(row + ((str(rng.choice(["", "SPRING10", "VIP"])),) if drift else ()))
    order_cols = SCHEMA["orders"] + ([("coupon_code", "VARCHAR(20)")] if drift else [])
    return {"region": {"columns": SCHEMA["region"], "rows": regions},
            "product": {"columns": SCHEMA["product"], "rows": products},
            "customer": {"columns": SCHEMA["customer"], "rows": customers},
            "orders": {"columns": order_cols, "rows": orders}}


def build_shop_db(path: Path, *, drift: bool = False, seed: int = SEED) -> dict:
    """Write the benchmark as a SQLite database (replacing `path`); returns row counts."""
    tables = shop_tables(drift=drift, seed=seed)
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    for name, t in tables.items():
        con.execute(f"CREATE TABLE {name} ({', '.join(f'{c} {ty}' for c, ty in t['columns'])})")
        con.executemany(f"INSERT INTO {name} VALUES ({','.join('?' * len(t['columns']))})", t["rows"])
    con.commit()
    con.close()
    return {"orders": len(tables["orders"]["rows"]), "customers": len(tables["customer"]["rows"]),
            "products": len(tables["product"]["rows"])}
