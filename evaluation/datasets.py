"""Seeded synthetic datasets with known causal structure for the analytical benchmark (P4-V01).

Every dataset is one table whose columns are generated from an explicit graph: each node lists its
parents, and everything else is drawn independently. That graph is the ground truth: two variables
are associated exactly when they share an ancestor (a node is its own ancestor). A finding on a pair
that is not associated is a false discovery, whatever its p-value; a *planted* effect is one the
benchmark expects the platform to find, with the segment it should name.

Nodes are derivation-level, not raw columns, so a derived segment is its own variable:
  column / is_true / equals / bucket   -> the column name
  duration_hours(start, end)           -> "start->end"
  after_hours(ts)                      -> "after_hours(ts)"
  date_trunc / hour_of_day / ...(ts)   -> "time(ts)"

`effects=False` generates the same table with every planted effect set to zero (the global null):
the platform should then verify nothing beyond structural relationships.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Planted:
    id: str
    method: str
    outcome: str  # node ("" for pareto: a concentration has no outcome)
    segment: str  # node
    expect_top: str | None = None
    filtered: bool | None = None  # pareto: planted under a filter (e.g. priority 1 only)


@dataclass
class Dataset:
    domain: str
    table: str  # unqualified table name
    frame: pd.DataFrame
    parents: dict[str, list[str]]  # node -> parent nodes (structural + planted)
    planted: list[Planted]
    null_columns: list[str]
    control_specs: list[dict[str, Any]] = field(default_factory=list)  # known-null hypotheses, same pipeline
    trend: bool = False  # a planted volume trend
    effects: bool = True

    def ancestors(self, node: str) -> set[str]:
        seen, stack = set(), [node]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(self.parents.get(n, []))
        return seen

    def associated(self, a: str, b: str) -> bool:
        return bool(self.ancestors(a) & self.ancestors(b))


def node(d: dict[str, Any] | None) -> str | None:
    """The truth-graph node a spec derivation refers to."""
    if not d:
        return None
    t, col = d.get("type", "column"), d.get("column")
    if t == "duration_hours":
        return f"{col}->{d.get('end_column')}"
    if t == "after_hours":
        return f"after_hours({col})"
    if t in ("date_trunc", "hour_of_day", "day_of_week"):
        return f"time({col})"
    return col


def _after_hours(ts: pd.Series) -> np.ndarray:
    """Same definition as skills/sqlbuild `after_hours`: before 08:00, from 18:00, or a weekend."""
    return ((ts.dt.hour < 8) | (ts.dt.hour >= 18) | (ts.dt.dayofweek >= 5)).to_numpy()


def _timestamps(rng: np.random.Generator, n: int, start: datetime, days: int) -> pd.Series:
    """Uniform over `days` days and all 24 hours: no planted trend, no planted hour-of-day effect."""
    seconds = rng.integers(0, days * 86400, size=n)
    return pd.Series(pd.to_datetime([start + timedelta(seconds=int(s)) for s in seconds]))


# ----------------------------------------------------------------------------- ITSM
def itsm(seed: int, *, n: int = 6000, effects: bool = True) -> Dataset:
    """Incidents. Planted: 3+ reassignments breach SLA ~3x as often; after-hours incidents take ~1.8x
    longer to resolve; the Network group ~1.8x longer; priority-1 incidents concentrate on one CI.
    Null: contact_type, category (drawn independently of everything)."""
    rng = np.random.default_rng(seed)
    opened = _timestamps(rng, n, datetime(2025, 1, 1), 360)
    after = _after_hours(opened)
    groups = np.array(["Service Desk", "Network", "Database", "Applications", "End User Computing", "Security"])
    group = rng.choice(groups, size=n)
    priority = rng.choice([1, 2, 3, 4], size=n, p=[0.12, 0.23, 0.4, 0.25])
    cis = np.array(["Payments Gateway", "Email", "CRM", "ERP", "VPN", "Wi-Fi", "Laptop Fleet", "Data Warehouse", "HR Portal",
                    "Printing", "Telephony", "Intranet"])
    ci = rng.choice(cis, size=n)
    if effects:  # planted: a third of P1s land on the payments gateway (vs 1/12 at random)
        p1 = priority == 1
        ci[p1] = np.where(rng.random(p1.sum()) < 0.35, "Payments Gateway", rng.choice(cis[1:], size=p1.sum()))
    reassign = rng.choice([0, 1, 2, 3, 4, 5], size=n, p=[0.45, 0.25, 0.14, 0.08, 0.05, 0.03])
    base_hours = rng.lognormal(mean=np.log(10), sigma=0.7, size=n)
    mult = np.ones(n)
    if effects:
        mult *= np.where(after, 1.8, 1.0) * np.where(group == "Network", 1.8, 1.0)
    resolved = opened + pd.to_timedelta(base_hours * mult, unit="h")
    breach_p = np.where(reassign >= 3, 0.36, 0.12) if effects else np.full(n, 0.15)
    made_sla = rng.random(n) >= breach_p
    frame = pd.DataFrame({
        "number": [f"INC{i + 1:07d}" for i in range(n)],
        "opened_at": opened, "resolved_at": resolved,
        "priority": priority.astype(int),
        "assignment_group_name": group,
        "cmdb_ci_name": ci,
        "contact_type": rng.choice(["phone", "email", "self-service", "chat"], size=n),
        "category": rng.choice(["hardware", "software", "network", "access", "inquiry"], size=n),
        "reassignment_count": reassign.astype(int),
        "made_sla": made_sla,
    })
    duration = "opened_at->resolved_at"
    parents: dict[str, list[str]] = {duration: [], "made_sla": [], "cmdb_ci_name": []}
    planted: list[Planted] = []
    if effects:
        parents = {duration: ["after_hours(opened_at)", "assignment_group_name"], "made_sla": ["reassignment_count"],
                   "cmdb_ci_name": ["priority"]}
        planted = [
            Planted("itsm_reassignment_sla", "rate_by_segment", "made_sla", "reassignment_count", "3+"),
            Planted("itsm_after_hours_resolution", "numeric_by_segment", duration, "after_hours(opened_at)"),
            Planted("itsm_network_resolution", "numeric_by_segment", duration, "assignment_group_name", "Network"),
            Planted("itsm_p1_payments_gateway", "pareto", "", "cmdb_ci_name", "Payments Gateway", filtered=True),
        ]
    fq = "bench.incident"
    controls = [{"method": "rate_by_segment", "asset": fq, "outcome": {"type": "equals", "column": "made_sla", "value": False},
                 "segment": {"type": "column", "column": c}} for c in ("contact_type", "category")]
    controls += [{"method": "numeric_by_segment", "asset": fq,
                  "outcome": {"type": "duration_hours", "column": "opened_at", "end_column": "resolved_at"},
                  "segment": {"type": "column", "column": c}} for c in ("contact_type", "category")]
    return Dataset("itsm", "incident", frame, parents, planted, ["contact_type", "category"], controls, effects=effects)


# ----------------------------------------------------------------------------- sales
def sales(seed: int, *, n: int = 8000, effects: bool = True) -> Dataset:
    """Orders. Planted: marketplace orders are returned ~3x as often; Enterprise order value ~2.1x
    Consumer; APAC ships ~3 days slower. Structural: net amount follows quantity and discount.
    Null: payment_method, device_type."""
    rng = np.random.default_rng(seed)
    order_date = _timestamps(rng, n, datetime(2024, 1, 1), 540).dt.normalize()
    channel = rng.choice(["web", "mobile", "marketplace", "store"], size=n, p=[0.4, 0.3, 0.2, 0.1])
    region = rng.choice(["EMEA", "North America", "APAC", "LATAM"], size=n)
    segment = rng.choice(["Consumer", "Small Business", "Enterprise"], size=n, p=[0.6, 0.3, 0.1])
    qty = rng.integers(1, 6, size=n)
    discount = rng.choice([0.0, 0.0, 0.05, 0.1, 0.2], size=n)
    price = rng.uniform(8, 400, size=n)
    seg_mult = np.vectorize({"Consumer": 1.0, "Small Business": 1.3, "Enterprise": 2.1}.get)(segment) if effects else 1.0
    net = np.round(price * qty * (1 - discount) * seg_mult * rng.uniform(0.9, 1.1, size=n), 2)
    ship = np.maximum(1, np.round(rng.normal(4.0 + (np.where(region == "APAC", 3.0, 0.0) if effects else 0.0), 1.2))).astype(int)
    ret_p = np.where(channel == "marketplace", 0.18, 0.06) if effects else np.full(n, 0.08)
    frame = pd.DataFrame({
        "order_id": np.arange(1, n + 1), "order_date": order_date, "channel": channel, "sales_region": region,
        "customer_segment": segment, "quantity": qty.astype(int), "discount_pct": discount, "net_amount": net,
        "shipping_days": ship, "returned": rng.random(n) < ret_p,
        "payment_method": rng.choice(["card", "paypal", "invoice", "gift_card"], size=n),
        "device_type": rng.choice(["desktop", "phone", "tablet"], size=n),
    })
    parents: dict[str, list[str]] = {"net_amount": ["quantity", "discount_pct"]}
    planted: list[Planted] = []
    if effects:
        parents |= {"net_amount": ["quantity", "discount_pct", "customer_segment"], "returned": ["channel"],
                    "shipping_days": ["sales_region"]}
        planted = [
            Planted("sales_returns_by_channel", "rate_by_segment", "returned", "channel", "marketplace"),
            Planted("sales_value_by_segment", "numeric_by_segment", "net_amount", "customer_segment", "Enterprise"),
            Planted("sales_shipping_by_region", "numeric_by_segment", "shipping_days", "sales_region", "APAC"),
        ]
    fq = "bench.orders"
    controls = [{"method": "rate_by_segment", "asset": fq, "outcome": {"type": "is_true", "column": "returned"},
                 "segment": {"type": "column", "column": c}} for c in ("payment_method", "device_type")]
    controls += [{"method": "numeric_by_segment", "asset": fq, "outcome": {"type": "column", "column": o},
                  "segment": {"type": "column", "column": c}}
                 for o in ("net_amount", "shipping_days") for c in ("payment_method", "device_type")]
    return Dataset("sales", "orders", frame, parents, planted, ["payment_method", "device_type"], controls, effects=effects)


# ----------------------------------------------------------------------------- finance
def finance(seed: int, *, n: int = 6000, effects: bool = True) -> Dataset:
    """Accounts-payable invoices (no domain pack: the core role playbook only). Planted: IT-services
    invoices are ~2.5x larger; Retail pays late ~3x as often; Public Sector takes ~12 days longer to
    pay. Null: entry_channel, currency."""
    rng = np.random.default_rng(seed)
    invoice_date = _timestamps(rng, n, datetime(2025, 1, 1), 300).dt.normalize()
    bu = rng.choice(["Retail", "Wholesale", "Public Sector", "Corporate", "Online"], size=n)
    vendor = rng.choice(["IT services", "Facilities", "Logistics", "Marketing", "Professional services", "Utilities"], size=n)
    entry = rng.choice(["EDI", "email PDF", "portal"], size=n)
    currency = rng.choice(["EUR", "USD", "GBP"], size=n)
    amount = rng.lognormal(mean=np.log(2000), sigma=0.8, size=n) * (np.where(vendor == "IT services", 2.5, 1.0) if effects else 1.0)
    days = np.maximum(1, np.round(rng.normal(30 + (np.where(bu == "Public Sector", 12.0, 0.0) if effects else 0.0), 6))).astype(int)
    late_p = np.where(bu == "Retail", 0.24, 0.08) if effects else np.full(n, 0.1)
    frame = pd.DataFrame({
        "invoice_id": np.arange(1, n + 1), "invoice_date": invoice_date, "business_unit": bu, "vendor_category": vendor,
        "entry_channel": entry, "currency": currency, "invoice_amount": np.round(amount, 2), "days_to_pay": days,
        "paid_late": rng.random(n) < late_p,
    })
    parents: dict[str, list[str]] = {}
    planted: list[Planted] = []
    if effects:
        parents = {"invoice_amount": ["vendor_category"], "days_to_pay": ["business_unit"], "paid_late": ["business_unit"]}
        planted = [
            Planted("finance_amount_by_vendor", "numeric_by_segment", "invoice_amount", "vendor_category", "IT services"),
            Planted("finance_late_by_unit", "rate_by_segment", "paid_late", "business_unit", "Retail"),
            Planted("finance_days_by_unit", "numeric_by_segment", "days_to_pay", "business_unit", "Public Sector"),
        ]
    fq = "bench.ap_invoice"
    controls = [{"method": "rate_by_segment", "asset": fq, "outcome": {"type": "is_true", "column": "paid_late"},
                 "segment": {"type": "column", "column": "currency"}}]
    controls += [{"method": "numeric_by_segment", "asset": fq, "outcome": {"type": "column", "column": o},
                  "segment": {"type": "column", "column": "currency"}} for o in ("invoice_amount", "days_to_pay")]
    return Dataset("finance", "ap_invoice", frame, parents, planted, ["entry_channel", "currency"], controls, effects=effects)


GENERATORS = {"itsm": itsm, "sales": sales, "finance": finance}
DOMAINS = tuple(GENERATORS)


def build(domain: str, seed: int, *, effects: bool = True, n: int | None = None) -> Dataset:
    gen = GENERATORS[domain]
    return gen(seed, effects=effects) if n is None else gen(seed, n=n, effects=effects)
