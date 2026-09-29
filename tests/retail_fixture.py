"""The retail orders generator of `scripts/e2e_full_journey.py` (the `rng = random.Random(7)` block), copied
so unit tests can use the same rows without the script's network calls. Keep it in step with the script.

Planted truth: returns are 30% for Electronics sold on the Marketplace and 6% otherwise (so the Marketplace
channel returns more), and late deliveries rise in the West from July 2026. `Status` is drawn at random
(95% Delivered / 5% Cancelled), independent of everything else: any price difference by status is chance.
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any


def retail_orders(seed: int = 7) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    regions, segments = ["North", "South", "East", "West"], ["Consumer", "Business"]
    cats = {"Electronics": (120, 900), "Home": (15, 200), "Clothing": (10, 120), "Sports": (20, 300)}
    channels = ["Web", "Store", "Marketplace"]
    customers = [{"Customer ID": f"C{i:04d}", "Customer Name": f"Customer {i}", "Email": f"customer{i}@example.com",
                  "Region": rng.choice(regions), "Segment": rng.choice(segments),
                  "Signup Date": (date(2024, 1, 1) + timedelta(days=rng.randint(0, 500))).isoformat()} for i in range(1, 401)]
    products = []
    for i in range(1, 61):
        cat = list(cats)[i % 4]
        lo, hi = cats[cat]
        products.append({"Product ID": f"P{i:03d}", "Product Name": f"{cat} item {i}", "Category": cat,
                         "List Price": round(rng.uniform(lo, hi), 2)})
    orders = []
    start = date(2025, 10, 1)
    for i in range(1, 6001):
        c, p = rng.choice(customers), rng.choice(products)
        od = start + timedelta(days=rng.randint(0, 364))
        ch = rng.choices(channels, weights=[5, 3, 2])[0]
        promised = od + timedelta(days=5)
        late_p = 0.35 if (c["Region"] == "West" and od >= date(2026, 7, 1)) else 0.08
        delivered = promised + timedelta(days=rng.randint(1, 6)) if rng.random() < late_p else promised - timedelta(days=rng.randint(0, 3))
        ret_p = 0.30 if (p["Category"] == "Electronics" and ch == "Marketplace") else 0.06
        qty = rng.randint(1, 4)
        disc = rng.choice([0, 0, 0, 5, 10, 15])
        orders.append({"Order ID": f"O{i:05d}", "Customer ID": c["Customer ID"], "Product ID": p["Product ID"],
                       "Order Date": od.isoformat(), "Quantity": qty, "Unit Price": p["List Price"], "Discount Pct": disc,
                       "Sales Channel": ch, "Status": rng.choices(["Delivered", "Cancelled"], weights=[95, 5])[0],
                       "Promised Date": promised.isoformat(), "Delivered Date": delivered.isoformat(),
                       "Returned": "Yes" if rng.random() < ret_p else "No"})
    return orders


ORDER_COLUMNS = ["Order ID", "Customer ID", "Product ID", "Order Date", "Quantity", "Unit Price", "Discount Pct",
                 "Sales Channel", "Status", "Promised Date", "Delivered Date", "Returned"]


def retail_duck(schema: str = "retail") -> Any:
    """An in-memory DuckDB with `<schema>.orders` and a recording RunSQL over it (`skills_fixtures.DuckRunSQL`)."""
    import duckdb
    import pandas as pd

    from skills_fixtures import DuckRunSQL

    con = duckdb.connect()
    con.execute(f'CREATE SCHEMA "{schema}"')
    con.register("_orders", pd.DataFrame(retail_orders()))
    con.execute(f'CREATE TABLE "{schema}".orders AS SELECT * FROM _orders')
    con.unregister("_orders")
    return DuckRunSQL(con)
