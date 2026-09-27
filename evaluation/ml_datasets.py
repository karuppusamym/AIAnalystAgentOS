"""Synthetic datasets for the governed-ML tests and the `ml` evaluation gate (P5-01..P5-05, P7-07).

Each generator returns (columns, rows, catalog types) shaped like a gateway snapshot. They are seeded, so
every run of a test or of the gate sees the same data."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import numpy as np

TYPES = {"customer_id": "text", "region": "text", "tenure_months": "integer", "monthly_spend": "double precision",
         "support_calls": "integer", "churned": "boolean", "refund_issued": "boolean", "snapshot_date": "date",
         "last_login": "date", "churn_date": "date", "coin_flip": "boolean", "spend_next": "double precision"}


def churn(n: int = 400, seed: int = 7, *, signal: bool = True, repeat: int = 1) -> tuple[list[str], list[list[Any]], dict]:
    """Customers (each `repeat` monthly snapshots) whose churn depends on support calls and tenure when `signal`.
    Leak columns: `refund_issued` (a copy of the outcome), `churn_date` (known only after it), `last_login` (may be
    after the snapshot cutoff when `late_logins`)."""
    rng = np.random.default_rng(seed)
    cols = ["customer_id", "region", "tenure_months", "monthly_spend", "support_calls", "snapshot_date", "last_login",
            "churn_date", "churned", "refund_issued", "coin_flip", "spend_next"]
    rows = []
    start = date(2025, 1, 1)
    for i in range(n):
        region = ["north", "south", "east", "west"][i % 4]
        tenure = int(rng.integers(1, 60))
        calls = int(rng.poisson(2))
        spend = float(round(rng.gamma(2.0, 30.0), 2))
        for m in range(repeat):
            snap = start + timedelta(days=30 * m + int(i % 28))
            logit = (1.1 * calls - 0.06 * tenure - 0.4) if signal else 0.0
            churned = bool(rng.random() < 1 / (1 + np.exp(-logit)))
            last_login = snap - timedelta(days=int(rng.integers(0, 20)))
            churn_date = (snap + timedelta(days=int(rng.integers(5, 40)))).isoformat() if churned else None
            rows.append([f"c{i:04d}", region, tenure, spend, calls, snap.isoformat(), last_login.isoformat(), churn_date,
                         churned, churned, bool(rng.random() < 0.5), float(round(spend * 1.1 + 5 * calls + rng.normal(0, 5), 2))])
    return cols, rows, dict(TYPES)


def spec(**over: Any) -> dict[str, Any]:
    base = {"task": "classify", "dataset": {"asset": "shop.customers"}, "target": "churned", "entity_keys": ["customer_id"],
            "features": [{"column": "region"}, {"column": "tenure_months"}, {"column": "monthly_spend"},
                         {"column": "support_calls"}],
            "split": {"strategy": "random", "independence_justification": "one row per customer"},
            "search": {"max_trials": 4, "max_seconds": 120}, "seed": 11}
    base.update(over)
    return base


def weekly_series(n: int = 90, seed: int = 3) -> tuple[list[str], list[list[Any]], dict]:
    rng = np.random.default_rng(seed)
    start = date(2023, 1, 2)
    rows = [[(start + timedelta(weeks=t)).isoformat(),
             float(round(200 + 1.5 * t + 25 * np.sin(2 * np.pi * t / 13) + rng.normal(0, 3), 3))] for t in range(n)]
    return ["week", "orders"], rows, {"week": "date", "orders": "double precision"}


def blobs(n: int = 300, seed: int = 5) -> tuple[list[str], list[list[Any]], dict]:
    rng = np.random.default_rng(seed)
    centers = [(0, 0), (6, 6), (0, 8)]
    rows = []
    for i in range(n):
        cx, cy = centers[i % 3]
        rows.append([f"p{i}", float(cx + rng.normal(0, 0.7)), float(cy + rng.normal(0, 0.7))])
    return ["id", "x", "y"], rows, {"id": "text", "x": "double precision", "y": "double precision"}


def sensor(n: int = 360, seed: int = 9) -> tuple[list[str], list[list[Any]], dict]:
    rng = np.random.default_rng(seed)
    start = date(2024, 1, 1)
    rows = []
    for t in range(n):
        v = float(10 * np.sin(2 * np.pi * t / 7) + rng.normal(0, 0.5))
        spike = t % 29 == 17
        rows.append([(start + timedelta(days=t)).isoformat(), v + (9.0 if spike else 0.0), spike])
    return ["day", "reading", "is_incident"], rows, {"day": "date", "reading": "double precision", "is_incident": "boolean"}
