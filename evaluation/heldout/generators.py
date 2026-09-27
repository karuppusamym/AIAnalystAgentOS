"""Held-out dataset generators (P4-08). Disjoint from the development benchmarks on purpose.

Every table, column and planted effect here is new: none of the names in `evaluation/datasets.py`
(incident / orders / ap_invoice), `evaluation/ml_datasets.py` (churn customers) or the recipe fixtures
appear, and seeds live in 7000-7999 (the development suites use 1-10, 101-105, 3-23). The truth of a
task (planted effects, the truth graph, the expected output of an engineering task) is its rubric in
`corpus.yaml`, written with these generators and frozen before the first scored run; a unit test checks
the generated data still carries what the rubric claims.

Analysis generators return `evaluation.datasets.Dataset` (so the development scorer `classify` applies);
the rubric replaces its `parents` / `planted` before scoring. Engineering generators return source tables
plus an independent pandas reference; ML generators return (columns, rows, catalog types).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from evaluation.datasets import Dataset, _timestamps

SCHEMA = "bench"


def _dataset(domain: str, table: str, frame: pd.DataFrame, nulls: list[str], effects: bool,
             controls: list[dict[str, Any]] | None = None) -> Dataset:
    return Dataset(domain, table, frame, parents={}, planted=[], null_columns=nulls, control_specs=controls or [],
                   effects=effects)


def _null_controls(table: str, flag: str | None, measures: list[str], nulls: list[str]) -> list[dict[str, Any]]:
    """Known-null hypotheses tested through the same pipeline as the platform's own proposals."""
    fq = f"{SCHEMA}.{table}"
    out = [{"method": "rate_by_segment", "asset": fq, "outcome": {"type": "is_true", "column": flag},
            "segment": {"type": "column", "column": c}} for c in nulls if flag]
    out += [{"method": "numeric_by_segment", "asset": fq, "outcome": {"type": "column", "column": m},
             "segment": {"type": "column", "column": c}} for m in measures for c in nulls]
    return out


# =============================================================================== analysis: ITSM
def itsm_change_request(seed: int, *, n: int = 5000, effects: bool = True, degenerate: str | None = None) -> Dataset:
    """Change requests. Planted: emergency changes need a rollback ~3x as often; critical-risk changes
    cause ~2.4x the downtime. Structural: approvals follow the risk tier. Null: change_board, requester_org."""
    rng = np.random.default_rng(seed)
    raised = _timestamps(rng, n, datetime(2025, 3, 1), 300)
    risk = rng.choice(["low", "medium", "high", "critical"], size=n, p=[0.4, 0.33, 0.19, 0.08])
    model = rng.choice(["standard", "normal", "emergency"], size=n, p=[0.5, 0.38, 0.12])
    board = rng.choice(["CAB-North", "CAB-South", "CAB-Core", "CAB-Edge", "CAB-Apps"], size=n)
    org = rng.choice(["Retail IT", "Corporate IT", "Plant IT", "Digital"], size=n)
    approvals = rng.poisson(1.0, size=n) + np.vectorize({"low": 1, "medium": 2, "high": 3, "critical": 4}.get)(risk)
    downtime = rng.gamma(2.0, 12.0, size=n) * (np.where(risk == "critical", 2.4, 1.0) if effects else 1.0)
    rollback_p = np.where(model == "emergency", 0.27, 0.09) if effects else np.full(n, 0.11)
    frame = pd.DataFrame({
        "chg_ref": [f"CHG{i + 40001:06d}" for i in range(n)],
        "raised_on": raised,
        "implemented_on": raised + pd.to_timedelta(rng.gamma(3.0, 20.0, size=n), unit="h"),
        "risk_tier": risk, "change_model": model, "change_board": board, "requester_org": org,
        "approvals_required": approvals.astype(int),
        "downtime_minutes": np.round(downtime, 1),
        "rollback_needed": rng.random(n) < rollback_p,
    })
    if degenerate == "tiny":
        frame = frame.head(36).reset_index(drop=True)
    return _dataset("itsm", "change_request", frame, ["change_board", "requester_org"], effects,
                    _null_controls("change_request", "rollback_needed", ["downtime_minutes"], ["change_board", "requester_org"]))


def itsm_service_request(seed: int, *, n: int = 5000, effects: bool = True) -> Dataset:
    """Catalog service requests. Planted: laptop refreshes escalate ~3x as often; the Field Ops team
    spends ~2x the effort; requests from the Rotterdam site take ~2x as long to fulfil.
    Null: request_channel, cost_center."""
    rng = np.random.default_rng(seed)
    submitted = _timestamps(rng, n, datetime(2025, 2, 1), 320)
    item = rng.choice(["Laptop refresh", "Software licence", "Access badge", "Mobile phone", "Desk move", "VPN token"], size=n)
    team = rng.choice(["Service Desk", "Field Ops", "Identity", "Procurement"], size=n)
    site = rng.choice(["Rotterdam", "Lyon", "Leeds", "Porto", "Graz"], size=n)
    hours = rng.lognormal(np.log(20), 0.6, size=n) * (np.where(site == "Rotterdam", 2.0, 1.0) if effects else 1.0)
    effort = rng.gamma(2.0, 1.5, size=n) * (np.where(team == "Field Ops", 2.0, 1.0) if effects else 1.0)
    esc_p = np.where(item == "Laptop refresh", 0.24, 0.08) if effects else np.full(n, 0.1)
    frame = pd.DataFrame({
        "ticket_key": [f"RITM{i + 7001:07d}" for i in range(n)],
        "submitted_ts": submitted, "fulfilled_ts": submitted + pd.to_timedelta(hours, unit="h"),
        "catalog_item": item, "fulfilment_team": team, "site": site,
        "request_channel": rng.choice(["portal", "email", "phone", "chat"], size=n),
        "cost_center": rng.choice(["CC-100", "CC-200", "CC-300", "CC-400", "CC-500", "CC-600"], size=n),
        "effort_hours": np.round(effort, 2),
        "escalated": rng.random(n) < esc_p,
    })
    return _dataset("itsm", "service_request", frame, ["request_channel", "cost_center"], effects,
                    _null_controls("service_request", "escalated", ["effort_hours"], ["request_channel", "cost_center"]))


# =============================================================================== analysis: sales
def sales_subscription(seed: int, *, n: int = 6000, effects: bool = True, degenerate: str | None = None) -> Dataset:
    """SaaS subscriptions. Planted: monthly billing churns ~2.5x as often as annual; Enterprise MRR ~3x
    Starter. Structural: MRR follows seats. Null: acquisition_channel, country_group."""
    rng = np.random.default_rng(seed)
    start = _timestamps(rng, n, datetime(2024, 6, 1), 420).dt.normalize()
    tier = rng.choice(["Starter", "Growth", "Scale", "Enterprise"], size=n, p=[0.45, 0.3, 0.17, 0.08])
    cycle = rng.choice(["monthly", "annual"], size=n, p=[0.6, 0.4])
    seats = rng.integers(1, 40, size=n)
    tier_mult = np.vectorize({"Starter": 1.0, "Growth": 1.5, "Scale": 2.0, "Enterprise": 3.0}.get)(tier) if effects else 1.0
    mrr = np.round(seats * 12.0 * tier_mult * rng.uniform(0.85, 1.15, size=n), 2)
    churn_p = np.where(cycle == "monthly", 0.20, 0.08) if effects else np.full(n, 0.14)
    churned = rng.random(n) < churn_p
    frame = pd.DataFrame({
        "account_ref": [f"ACC-{i + 90001}" for i in range(n)], "start_month": start,
        "plan_tier": tier, "billing_cycle": cycle,
        "acquisition_channel": rng.choice(["organic", "paid_search", "partner", "outbound"], size=n),
        "country_group": rng.choice(["DACH", "Nordics", "Benelux", "Iberia", "UK&I"], size=n),
        "seats": seats.astype(int), "mrr_usd": mrr, "churned_90d": churned,
    })
    if degenerate == "flat":  # nothing varies: one price, nobody churned
        frame["mrr_usd"] = 49.0
        frame["churned_90d"] = False
        frame["seats"] = 1
    return _dataset("sales", "subscription", frame, ["acquisition_channel", "country_group"], effects,
                    _null_controls("subscription", "churned_90d", ["mrr_usd"], ["acquisition_channel", "country_group"]))


def sales_opportunity(seed: int, *, n: int = 5000, effects: bool = True) -> Dataset:
    """B2B opportunities. Planted: partner-sourced deals are won ~2.4x as often; Manufacturing deals are
    ~2.2x larger. Null: sales_team, product_line; sales_cycle_days is independent of everything."""
    rng = np.random.default_rng(seed)
    created = _timestamps(rng, n, datetime(2024, 9, 1), 360).dt.normalize()
    source = rng.choice(["inbound", "event", "partner", "cold outreach"], size=n, p=[0.4, 0.2, 0.15, 0.25])
    industry = rng.choice(["Manufacturing", "Healthcare", "Public", "Retail", "Financial services"], size=n)
    size = rng.lognormal(np.log(18000), 0.7, size=n) * (np.where(industry == "Manufacturing", 2.2, 1.0) if effects else 1.0)
    win_p = np.where(source == "partner", 0.41, 0.17) if effects else np.full(n, 0.2)
    frame = pd.DataFrame({
        "opp_id": np.arange(500001, 500001 + n), "created_date": created,
        "lead_source": source, "industry": industry,
        "sales_team": rng.choice(["Team Atlas", "Team Boreal", "Team Cirrus", "Team Delta"], size=n),
        "product_line": rng.choice(["Core", "Analytics add-on", "Integrations", "Services"], size=n),
        "deal_size_eur": np.round(size, 2),
        "sales_cycle_days": np.maximum(5, np.round(rng.normal(75, 20, size=n))).astype(int),
        "won": rng.random(n) < win_p,
    })
    return _dataset("sales", "opportunity", frame, ["sales_team", "product_line"], effects,
                    _null_controls("opportunity", "won", ["deal_size_eur", "sales_cycle_days"], ["sales_team", "product_line"]))


# =============================================================================== analysis: finance
def finance_expense_claim(seed: int, *, n: int = 5000, effects: bool = True, degenerate: str | None = None) -> Dataset:
    """Employee expense claims. Planted: travel claims are ~2.2x larger; the Sales department breaches
    policy ~3x as often. Null: submission_channel, employee_band; approval_days is independent."""
    rng = np.random.default_rng(seed)
    submitted = _timestamps(rng, n, datetime(2025, 1, 6), 280).dt.normalize()
    dept = rng.choice(["Sales", "Engineering", "Finance", "Operations", "Marketing"], size=n)
    etype = rng.choice(["travel", "meals", "software", "training", "equipment"], size=n)
    amount = rng.lognormal(np.log(180), 0.8, size=n) * (np.where(etype == "travel", 2.2, 1.0) if effects else 1.0)
    breach_p = np.where(dept == "Sales", 0.21, 0.07) if effects else np.full(n, 0.09)
    frame = pd.DataFrame({
        "claim_no": [f"EXP{i + 300001}" for i in range(n)], "submitted_date": submitted,
        "department": dept, "expense_type": etype,
        "submission_channel": rng.choice(["mobile", "web", "email"], size=n),
        "employee_band": rng.choice(["B1", "B2", "B3", "B4"], size=n),
        "claim_amount": np.round(amount, 2),
        "approval_days": np.maximum(0, np.round(rng.normal(6, 2.5, size=n))).astype(int),
        "policy_breach": rng.random(n) < breach_p,
    })
    if degenerate == "sparse":  # the outcomes were mostly never recorded
        missing = rng.random(n) < 0.97
        frame["claim_amount"] = frame["claim_amount"].mask(missing)
        frame["policy_breach"] = frame["policy_breach"].astype(object).mask(missing)
        frame["approval_days"] = frame["approval_days"].astype("Int64").mask(missing)
    return _dataset("finance", "expense_claim", frame, ["submission_channel", "employee_band"], effects,
                    _null_controls("expense_claim", "policy_breach", ["claim_amount"], ["submission_channel", "employee_band"]))


def finance_receivable(seed: int, *, n: int = 5000, effects: bool = True) -> Dataset:
    """Accounts receivable. Planted: NET90 terms run ~20 days more overdue; Small customers are written
    off ~3x as often. Null: collector, invoice_currency."""
    rng = np.random.default_rng(seed)
    issued = _timestamps(rng, n, datetime(2024, 11, 1), 330).dt.normalize()
    tier = rng.choice(["Key", "Standard", "Small"], size=n, p=[0.2, 0.5, 0.3])
    terms = rng.choice(["NET15", "NET30", "NET60", "NET90"], size=n)
    overdue = np.maximum(0, np.round(rng.normal(12 + (np.where(terms == "NET90", 20.0, 0.0) if effects else 0.0), 7))).astype(int)
    wo_p = np.where(tier == "Small", 0.12, 0.04) if effects else np.full(n, 0.06)
    frame = pd.DataFrame({
        "receivable_id": np.arange(800001, 800001 + n), "issue_date": issued,
        "customer_tier": tier, "payment_terms": terms,
        "collector": rng.choice(["M. Okafor", "J. Lindqvist", "R. Haddad", "S. Moreau"], size=n),
        "invoice_currency": rng.choice(["EUR", "CHF", "SEK", "PLN"], size=n),
        "outstanding_amount": np.round(rng.lognormal(np.log(4200), 0.9, size=n), 2),
        "days_overdue": overdue,
        "written_off": rng.random(n) < wo_p,
    })
    return _dataset("finance", "receivable", frame, ["collector", "invoice_currency"], effects,
                    _null_controls("receivable", "written_off", ["days_overdue", "outstanding_amount"],
                                   ["collector", "invoice_currency"]))


ANALYSIS = {"itsm_change_request": itsm_change_request, "itsm_service_request": itsm_service_request,
            "sales_subscription": sales_subscription, "sales_opportunity": sales_opportunity,
            "finance_expense_claim": finance_expense_claim, "finance_receivable": finance_receivable}


# =============================================================================== engineering
def _table(columns: list[tuple[str, str]], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": [c for c, _ in columns], "types": [t for _, t in columns], "rows": rows}


def _schema(t: dict[str, Any]) -> list[dict[str, str]]:
    return [{"name": c, "type": ty} for c, ty in zip(t["columns"], t["types"], strict=True)]


def shipments(seed: int, *, n: int = 400, duplicate_share: float = 0.08, negative: int = 0,
              unknown_carrier: int = 0) -> dict[str, dict[str, Any]]:
    """Warehouse shipments delivered twice by a flaky feed (a later `loaded_at` copy of the same shipment
    with a corrected weight), a carrier dimension (code 5 has no carrier row) and a late-arrivals batch.
    Optional defects, planted on delivered in-window shipments before the feed duplicates them: negative
    weights, and shipments with no carrier code."""
    rng = np.random.default_rng(seed)
    carriers = [[1, "Nordfrakt", "road"], [2, "BlueSea", "sea"], [3, "AirSwift", "air"], [4, "RailOne", "rail"]]
    base = date(2025, 4, 1)
    rows = []
    for i in range(n):
        ship_date = base + timedelta(days=int(rng.integers(0, 60)))
        rows.append([10_000 + i, int(rng.integers(1, 6)), ship_date.isoformat(), float(round(rng.gamma(2.0, 40.0), 2)),
                     str(rng.choice(["delivered", "in_transit", "returned"], p=[0.8, 0.15, 0.05])), f"{ship_date.isoformat()} 06:00:00"])
    defects = [(i, "negative") for i in range(negative)] + [(n - 1 - i, "unknown_carrier") for i in range(unknown_carrier)]
    for i, kind in defects:
        rows[i][2], rows[i][4], rows[i][5] = "2025-05-10", "delivered", "2025-05-10 06:00:00"
        if kind == "negative":
            rows[i][3] = -abs(rows[i][3])
        else:
            rows[i][1] = None
    dup_idx = rng.choice(n, size=int(n * duplicate_share), replace=False)
    for i in dup_idx:  # a later redelivery of the same shipment with a corrected weight
        r = list(rows[i])
        r[3] = float(round(r[3] * 1.05, 2))
        r[5] = r[5].replace("06:00:00", "18:30:00")
        rows.append(r)
    late = []
    for i in range(max(1, n // 20)):
        ship_date = base + timedelta(days=int(rng.integers(0, 60)))
        late.append([20_000 + i, int(rng.integers(1, 6)), ship_date.isoformat(), float(round(rng.gamma(2.0, 40.0), 2)),
                     "delivered", f"{(ship_date + timedelta(days=9)).isoformat()} 07:15:00"])
    cols = [("shipment_no", "integer"), ("carrier_code", "integer"), ("ship_date", "date"), ("weight_kg", "double"),
            ("shipment_state", "text"), ("loaded_at", "timestamp")]
    return {f"{SCHEMA}.shipments": _table(cols, rows), f"{SCHEMA}.shipments_late": _table(cols, late),
            f"{SCHEMA}.carriers": _table([("carrier_code", "integer"), ("carrier_name", "text"), ("transport_mode", "text")],
                                        carriers)}


def shipments_recipe(tables: dict[str, dict[str, Any]], *, variant: str) -> dict[str, Any]:
    """The engineering tasks' recipes over `shipments`: dedupe the redeliveries (latest load wins), add the
    late batch and keep delivered shipments in the window. `clean` and `undeclared_column` then join the
    carrier and total weight per carrier and mode; the per-shipment variants publish one row per shipment
    behind a row gate (`null_key_drop`: drop and count shipments without a carrier code;
    `negative_weight_fail`: block the output on a negative weight)."""
    sh, late, car = (f"{SCHEMA}.shipments", f"{SCHEMA}.shipments_late", f"{SCHEMA}.carriers")
    nodes: list[dict[str, Any]] = [
        {"op": "source", "id": "feed", "asset": sh, "schema": _schema(tables[sh])},
        {"op": "source", "id": "late", "asset": late, "schema": _schema(tables[late])},
        {"op": "union", "id": "all_rows", "inputs": ["feed", "late"]},
        {"op": "dedupe", "id": "latest", "input": "all_rows", "keys": ["shipment_no"],
         "order": [{"column": "loaded_at", "desc": True}]},
        {"op": "filter", "id": "delivered", "input": "latest",
         "predicate": "shipment_state = 'delivered' AND ship_date >= CAST('2025-04-15' AS DATE)"},
    ]
    if variant in ("null_key_drop", "negative_weight_fail"):
        gate = ({"type": "not_null", "column": "carrier_code", "severity": "drop"} if variant == "null_key_drop" else
                {"type": "range", "column": "weight_kg", "min": 0, "severity": "fail"})
        nodes += [{"op": "select", "id": "picked", "input": "delivered",
                   "columns": ["shipment_no", "carrier_code", "ship_date", "weight_kg"]},
                  {"op": "output", "id": "out", "input": "picked", "name": "delivered_shipments", "grain": ["shipment_no"],
                   "keys": ["shipment_no"], "schema_policy": "warn",
                   "schema": [{"name": "shipment_no", "type": "integer"}, {"name": "carrier_code", "type": "integer"},
                              {"name": "ship_date", "type": "date"}, {"name": "weight_kg", "type": "double"}],
                   "gates": [gate]}]
    else:
        if variant == "undeclared_column":
            nodes[4]["predicate"] = "shipment_status = 'delivered'"  # the column is shipment_state
        nodes.insert(2, {"op": "source", "id": "carriers", "asset": car, "schema": _schema(tables[car])})
        nodes += [
            {"op": "join", "id": "with_carrier", "left": "delivered", "right": "carriers", "how": "inner",
             "on": [{"left": "carrier_code", "right": "carrier_code"}], "expected_cardinality": "many_to_one"},
            {"op": "aggregate", "id": "per_carrier", "input": "with_carrier", "keys": ["carrier_name", "transport_mode"],
             "measures": [{"name": "shipments", "func": "count", "type": "bigint"},
                          {"name": "total_weight_kg", "func": "sum", "column": "weight_kg", "type": "double"}]},
            {"op": "output", "id": "out", "input": "per_carrier", "name": "carrier_weight",
             "grain": ["carrier_name", "transport_mode"], "keys": ["carrier_name", "transport_mode"], "schema_policy": "warn",
             "schema": [{"name": "carrier_name", "type": "text"}, {"name": "transport_mode", "type": "text"},
                        {"name": "shipments", "type": "bigint"}, {"name": "total_weight_kg", "type": "double"}],
             "gates": []},
        ]
    return {"kind": "Recipe", "name": f"heldout_{variant}", "description": f"held-out engineering task ({variant})",
            "nodes": nodes}


def shipments_reference(tables: dict[str, dict[str, Any]], *, variant: str) -> dict[str, Any]:
    """The expected output, computed with pandas independently of the recipe compilers."""
    sh = pd.DataFrame(tables[f"{SCHEMA}.shipments"]["rows"], columns=tables[f"{SCHEMA}.shipments"]["columns"])
    late = pd.DataFrame(tables[f"{SCHEMA}.shipments_late"]["rows"], columns=tables[f"{SCHEMA}.shipments_late"]["columns"])
    car = pd.DataFrame(tables[f"{SCHEMA}.carriers"]["rows"], columns=tables[f"{SCHEMA}.carriers"]["columns"])
    rows = pd.concat([sh, late], ignore_index=True)
    rows["loaded_ts"] = pd.to_datetime(rows["loaded_at"])
    rows = rows.sort_values("loaded_ts", ascending=False, kind="mergesort")
    latest = rows.drop_duplicates("shipment_no", keep="first")
    delivered = latest[(latest["shipment_state"] == "delivered") & (pd.to_datetime(latest["ship_date"]) >= "2025-04-15")]
    if variant == "null_key_drop":
        kept = delivered[delivered["carrier_code"].notna()]
        out = kept[["shipment_no", "carrier_code", "ship_date", "weight_kg"]].copy()
        out["carrier_code"] = out["carrier_code"].astype(int)
        return {"columns": list(out.columns), "rows": sorted(out.values.tolist(), key=lambda r: r[0]),
                "dropped_rows": int(delivered["carrier_code"].isna().sum())}
    joined = delivered.merge(car, on="carrier_code", how="inner")
    agg = joined.groupby(["carrier_name", "transport_mode"], as_index=False).agg(
        shipments=("shipment_no", "size"), total_weight_kg=("weight_kg", "sum"))
    return {"columns": list(agg.columns), "rows": sorted(agg.values.tolist(), key=lambda r: (r[0], r[1])), "dropped_rows": 0}


def meter_readings(seed: int, *, n_meters: int = 120, days: int = 40, duplicate_tariffs: bool = False) -> dict[str, dict[str, Any]]:
    """Daily smart-meter readings and a tariff dimension (one row per tariff code, or two when
    `duplicate_tariffs`: a many-to-many join that would double the energy)."""
    rng = np.random.default_rng(seed)
    tariffs = [["T-HOME", "household", 0.31], ["T-SME", "business", 0.27], ["T-EV", "household", 0.22]]
    if duplicate_tariffs:
        tariffs.append(["T-SME", "business", 0.25])  # an overlapping validity period copied in twice
    meters = [[f"M{m:05d}", str(rng.choice(["T-HOME", "T-SME", "T-EV"]))] for m in range(n_meters)]
    readings = []
    start = date(2025, 5, 1)
    for m, tariff in meters:
        for d in range(days):
            if rng.random() < 0.03:
                continue  # a missed reading
            readings.append([m, (start + timedelta(days=d)).isoformat(), float(round(rng.gamma(3.0, 3.2), 3)), tariff])
    return {f"{SCHEMA}.meter_readings": _table([("meter_id", "text"), ("reading_date", "date"), ("kwh", "double"),
                                                ("tariff_code", "text")], readings),
            f"{SCHEMA}.tariffs": _table([("tariff_code", "text"), ("segment", "text"), ("eur_per_kwh", "double")], tariffs)}


def meter_recipe(tables: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rd, tf = f"{SCHEMA}.meter_readings", f"{SCHEMA}.tariffs"
    return {"kind": "Recipe", "name": "heldout_meter_cost", "description": "energy and cost per segment and month",
            "nodes": [
                {"op": "source", "id": "readings", "asset": rd, "schema": _schema(tables[rd])},
                {"op": "source", "id": "tariffs", "asset": tf, "schema": _schema(tables[tf])},
                {"op": "join", "id": "priced", "left": "readings", "right": "tariffs", "how": "left",
                 "on": [{"left": "tariff_code", "right": "tariff_code"}], "expected_cardinality": "many_to_one"},
                {"op": "derive", "id": "costed", "input": "priced", "columns": [
                    {"name": "cost_eur", "expr": "CAST(kwh * eur_per_kwh AS DOUBLE)", "type": "double"},
                    {"name": "reading_month", "expr": "DATE_TRUNC('month', reading_date)", "type": "date"}]},
                {"op": "aggregate", "id": "monthly", "input": "costed", "keys": ["segment", "reading_month"], "measures": [
                    {"name": "readings", "func": "count", "type": "bigint"},
                    {"name": "kwh_total", "func": "sum", "column": "kwh", "type": "double"},
                    {"name": "cost_total_eur", "func": "sum", "column": "cost_eur", "type": "double"}]},
                {"op": "output", "id": "out", "input": "monthly", "name": "segment_energy_monthly",
                 "grain": ["segment", "reading_month"], "keys": ["segment", "reading_month"], "schema_policy": "warn",
                 "schema": [{"name": "segment", "type": "text"}, {"name": "reading_month", "type": "date"},
                            {"name": "readings", "type": "bigint"}, {"name": "kwh_total", "type": "double"},
                            {"name": "cost_total_eur", "type": "double"}],
                 "gates": [{"type": "not_null", "column": "segment", "severity": "fail"}]},
            ]}


def meter_reference(tables: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rd = pd.DataFrame(tables[f"{SCHEMA}.meter_readings"]["rows"], columns=tables[f"{SCHEMA}.meter_readings"]["columns"])
    tf = pd.DataFrame(tables[f"{SCHEMA}.tariffs"]["rows"], columns=tables[f"{SCHEMA}.tariffs"]["columns"])
    j = rd.merge(tf, on="tariff_code", how="left")
    j["cost_eur"] = j["kwh"] * j["eur_per_kwh"]
    j["reading_month"] = pd.to_datetime(j["reading_date"]).dt.to_period("M").dt.to_timestamp().dt.date.astype(str)
    agg = j.groupby(["segment", "reading_month"], as_index=False).agg(
        readings=("kwh", "size"), kwh_total=("kwh", "sum"), cost_total_eur=("cost_eur", "sum"))
    return {"columns": list(agg.columns), "rows": sorted(agg.values.tolist(), key=lambda r: (r[0], r[1])), "dropped_rows": 0}


def engineering(generator: str, seed: int, variant: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any] | None]:
    """(source tables, recipe spec, reference output or None when the rubric expects a refusal)."""
    if generator == "de_shipments":
        tables = shipments(seed, negative=5 if variant == "negative_weight_fail" else 0,
                           unknown_carrier=6 if variant == "null_key_drop" else 0)
        ref = shipments_reference(tables, variant=variant) if variant in ("clean", "null_key_drop") else None
        return tables, shipments_recipe(tables, variant=variant), ref
    if generator == "de_meters":
        tables = meter_readings(seed, duplicate_tariffs=variant == "fanout")
        return tables, meter_recipe(tables), meter_reference(tables) if variant == "clean" else None
    raise KeyError(generator)


# =============================================================================== ML
ML_TYPES = {"asset_tag": "text", "site": "text", "age_years": "double precision", "vibration_mm_s": "double precision",
            "bearing_temp_c": "double precision", "load_pct": "double precision", "inspected_on": "date",
            "failed_30d": "boolean", "work_order_raised": "boolean", "noise_flag": "boolean",
            "repair_cost_next_eur": "double precision", "failure_date": "date"}


def equipment(n: int = 450, seed: int = 7301, *, signal: bool = True, repeat: int = 1) -> tuple[list[str], list[list[Any]], dict]:
    """Pumps in several plants: failure within 30 days depends on vibration and bearing temperature when
    `signal`; repair cost follows age and load. Leaks: `work_order_raised` (raised because it failed) and
    `failure_date` (known only after the outcome)."""
    rng = np.random.default_rng(seed)
    cols = ["asset_tag", "site", "age_years", "vibration_mm_s", "bearing_temp_c", "load_pct", "inspected_on",
            "failure_date", "failed_30d", "work_order_raised", "noise_flag", "repair_cost_next_eur"]
    rows = []
    start = date(2025, 2, 3)
    for i in range(n):
        site = ["Antwerp", "Gdansk", "Bilbao", "Tampere", "Linz"][i % 5]
        age = float(round(rng.uniform(0.5, 18), 2))
        load = float(round(rng.uniform(30, 98), 1))
        for m in range(repeat):
            vib = float(round(rng.gamma(2.2, 1.6), 3))
            temp = float(round(rng.normal(62, 7), 2))
            logit = (1.3 * (vib - 3.5) + 0.12 * (temp - 62) - 1.2) if signal else 0.0
            failed = bool(rng.random() < 1 / (1 + np.exp(-logit)))
            inspected = start + timedelta(days=21 * m + int(i % 19))
            fdate = (inspected + timedelta(days=int(rng.integers(2, 29)))).isoformat() if failed else None
            cost = float(round(150 + 40 * age + 6.5 * load + rng.normal(0, 60), 2))
            rows.append([f"P-{i:04d}", site, age, vib, temp, load, inspected.isoformat(), fdate, failed, failed,
                         bool(rng.random() < 0.5), cost])
    return cols, rows, dict(ML_TYPES)


def ml_spec(**over: Any) -> dict[str, Any]:
    base = {"task": "classify", "dataset": {"asset": "plant.pumps"}, "target": "failed_30d", "entity_keys": ["asset_tag"],
            "features": [{"column": "site"}, {"column": "age_years"}, {"column": "vibration_mm_s"},
                         {"column": "bearing_temp_c"}, {"column": "load_pct"}],
            "split": {"strategy": "random", "independence_justification": "one inspection per pump"},
            "search": {"max_trials": 4, "max_seconds": 120}, "seed": 29}
    base.update(over)
    return base


def daily_calls(n: int = 196, seed: int = 7311) -> tuple[list[str], list[list[Any]], dict]:
    """Contact-centre calls per day: a weekly cycle (quiet weekends) on a slow upward drift."""
    rng = np.random.default_rng(seed)
    start = date(2024, 9, 2)
    weekly = [1.0, 0.95, 0.92, 0.97, 1.08, 0.45, 0.35]
    rows = [[(start + timedelta(days=t)).isoformat(), float(round((800 + 0.9 * t) * weekly[t % 7] + rng.normal(0, 18), 2))]
            for t in range(n)]
    return ["call_day", "calls"], rows, {"call_day": "date", "calls": "double precision"}


def ml_task(generator: str, seed: int, variant: str) -> tuple[tuple[list[str], list[list[Any]], dict], dict[str, Any]]:
    """(data, MLSpec dict) of one ML task."""
    if generator == "ml_pumps":
        if variant == "classify":
            return equipment(seed=seed), ml_spec()
        if variant == "regress":
            return equipment(seed=seed), ml_spec(task="regress", target="repair_cost_next_eur", features=[
                {"column": "age_years"}, {"column": "load_pct"}, {"column": "site"}])
        if variant == "null_target":
            return equipment(seed=seed, signal=False), ml_spec(target="noise_flag")
        if variant == "leak_copy":
            return equipment(seed=seed), ml_spec(features=[{"column": "vibration_mm_s"}, {"column": "work_order_raised"}])
        if variant == "leak_post_outcome":
            return equipment(seed=seed), ml_spec(features=[{"column": "vibration_mm_s"},
                                                           {"column": "failure_date", "type": "datetime"}],
                                                 cutoff_column="inspected_on", outcome_time_column="failure_date")
        if variant == "repeated_entities":
            return equipment(n=150, seed=seed, repeat=3), ml_spec()
        if variant == "tiny":
            return equipment(n=18, seed=seed), ml_spec()
    if generator == "ml_calls" and variant == "forecast":
        return daily_calls(seed=seed), {"task": "forecast", "dataset": {"asset": "cc.calls"}, "target": "calls",
                                        "time_column": "call_day", "horizon": 14, "season_length": 7, "seed": 5}
    raise KeyError(f"{generator}:{variant}")
