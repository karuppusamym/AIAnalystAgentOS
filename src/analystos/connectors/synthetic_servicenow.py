"""Deterministic, ServiceNow-shaped synthetic ITSM data with known ground truth.

The generator produces the tables a ServiceNow instance would expose through the Table API
(`incident`, `change_request`, `sys_user_group`, `cmdb_ci`) for the 12 months ending
2026-09-01. Field names follow the real ServiceNow dictionary. Known analytical patterns are
embedded on purpose and described in ``GROUND_TRUTH`` so benchmark tests can check whether the
agent rediscovers them; known data-quality defects are injected and described in
``DATA_QUALITY``.

All generation is vectorised with numpy and seeded, so the same seed always yields byte-identical
tables. ``generate_servicenow_data()`` is cached per (seed, sizes).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

DEFAULT_SEED = 20260901
PERIOD_END = datetime(2026, 9, 1)
PERIOD_START = datetime(2025, 9, 1)

GROUPS: list[str] = [
    "Network Operations",
    "Service Desk",
    "Database Administration",
    "Application Support",
    "Linux Operations",
    "Windows Operations",
    "Security Operations",
    "Cloud Platform",
    "Storage and Backup",
    "Identity and Access",
    "End User Computing",
    "Payments Engineering",
]

# Mean reassignment (Poisson lambda) per assignment group. Network Operations is highest (GT e).
GROUP_REASSIGN_LAMBDA: dict[str, float] = {
    "Network Operations": 2.6,
    "Service Desk": 1.2,
    "Database Administration": 1.0,
    "Application Support": 1.3,
    "Linux Operations": 0.8,
    "Windows Operations": 0.8,
    "Security Operations": 0.9,
    "Cloud Platform": 1.1,
    "Storage and Backup": 0.7,
    "Identity and Access": 0.6,
    "End User Computing": 0.9,
    "Payments Engineering": 1.4,
}

CATEGORIES = ["network", "software", "hardware", "database", "inquiry"]
CATEGORY_P = [0.22, 0.34, 0.16, 0.12, 0.16]
# Which groups handle which category (first entry is the primary owner).
CATEGORY_GROUPS: dict[str, list[str]] = {
    "network": ["Network Operations", "Cloud Platform", "Security Operations"],
    "software": ["Application Support", "Payments Engineering", "End User Computing", "Cloud Platform"],
    "hardware": ["End User Computing", "Windows Operations", "Linux Operations", "Storage and Backup"],
    "database": ["Database Administration", "Storage and Backup", "Linux Operations"],
    "inquiry": ["Service Desk", "Identity and Access", "End User Computing"],
}
CATEGORY_GROUP_P: dict[str, list[float]] = {
    "network": [0.7, 0.2, 0.1],
    "software": [0.5, 0.2, 0.2, 0.1],
    "hardware": [0.4, 0.25, 0.25, 0.1],
    "database": [0.7, 0.15, 0.15],
    "inquiry": [0.6, 0.25, 0.15],
}
SHORT_DESCRIPTIONS: dict[str, list[str]] = {
    "network": ["VPN connection drops", "Packet loss on core switch", "Wi-Fi unavailable on floor 3", "DNS resolution failures"],
    "software": ["Application error on login", "Payment page times out", "Report generation fails", "Service returns HTTP 500"],
    "hardware": ["Laptop does not boot", "Printer jammed", "Disk failure alert", "Monitor flickering"],
    "database": ["Slow query performance", "Replication lag alert", "Tablespace almost full", "Deadlocks reported"],
    "inquiry": ["How do I reset my password", "Request for software access", "Question about VPN setup", "Account locked"],
}
CONTACT_TYPES = ["phone", "email", "self-service", "walk-in", "monitoring"]
CONTACT_P = [0.3, 0.25, 0.3, 0.05, 0.1]

PAYMENTS_GATEWAY = "Payments Gateway"
CI_NAMES: list[str] = [
    PAYMENTS_GATEWAY, "Customer Portal", "Mobile Banking App", "Core Banking", "CRM", "ERP Finance",
    "HR Portal", "Email Service", "Corporate VPN", "Data Warehouse", "Identity Provider", "Order Management",
    "Inventory Service", "Billing Engine", "Fraud Detection", "Notification Service", "Search Service",
    "API Gateway", "Document Management", "Intranet", "Ticketing System", "Monitoring Platform",
    "Backup Service", "File Share", "Print Service", "Telephony", "Video Conferencing", "Loan Origination",
    "Card Management", "Treasury System", "Risk Engine", "Reporting Portal", "Mainframe Batch",
    "Kubernetes Cluster A", "Kubernetes Cluster B", "Oracle Cluster", "Postgres Cluster", "Core Switch DC1",
    "Core Switch DC2", "Firewall Cluster",
]
PAYMENTS_GATEWAY_SHARE = 0.05  # share of all incidents on the Payments Gateway CI

# Priority mix for non-PG vs Payments Gateway incidents (P1..P5). Derivation: overall P1 ~= 4%,
# PG holds 5% of incidents but ~35% of P1 => PG P1 rate 7x overall.
PRIORITY_P_OTHER = [0.0274, 0.12, 0.35, 0.40, 0.1026]
PRIORITY_P_PG = [0.28, 0.25, 0.25, 0.17, 0.05]
# ServiceNow default priority matrix: (impact, urgency) -> priority
PRIORITY_MATRIX: dict[int, list[tuple[int, int]]] = {
    1: [(1, 1)],
    2: [(1, 2), (2, 1)],
    3: [(1, 3), (2, 2), (3, 1)],
    4: [(2, 3), (3, 2)],
    5: [(3, 3)],
}
# Median resolution hours by priority; after-hours incidents take 1.6x longer (GT b).
RESOLUTION_MEDIAN_HOURS = {1: 4.0, 2: 10.0, 3: 26.0, 4: 50.0, 5: 80.0}
AFTER_HOURS_FACTOR = 1.6
# SLA breach probability by reassignment bucket (GT a): >=3 has 3x the rate of <=1.
BREACH_P_LE1 = 0.10
BREACH_P_EQ2 = 0.17
BREACH_P_GE3 = 0.30

INCIDENT_STATES = {1: "New", 2: "In Progress", 3: "On Hold", 6: "Resolved", 7: "Closed", 8: "Canceled"}
CHANGE_STATES = {-5: "New", -4: "Assess", -3: "Authorize", -2: "Scheduled", -1: "Implement", 0: "Review", 3: "Closed", 4: "Canceled"}
CHANGE_TYPE_P = {"standard": 0.60, "normal": 0.32, "emergency": 0.08}

GROUND_TRUTH: dict[str, dict[str, Any]] = {
    "reassignment_sla_breach": {
        "description": "Incidents reassigned 3+ times breach SLA (made_sla = false) about 3x as often as incidents reassigned at most once.",
        "metric": "breach_rate(reassignment_count >= 3) / breach_rate(reassignment_count <= 1)",
        "expected": BREACH_P_GE3 / BREACH_P_LE1,
        "tolerance": 0.6,
        "tables": ["incident"],
        "columns": ["incident.reassignment_count", "incident.made_sla"],
    },
    "after_hours_resolution": {
        "description": "Incidents opened after hours (before 08:00, at/after 18:00, or on weekends) take ~1.6x longer to resolve.",
        "metric": "mean(resolved_at - opened_at | after hours) / mean(... | business hours), resolved rows with resolved_at >= opened_at",
        "expected": AFTER_HOURS_FACTOR,
        "tolerance": 0.2,
        "tables": ["incident"],
        "columns": ["incident.opened_at", "incident.resolved_at"],
    },
    "payments_gateway_p1": {
        "description": "The 'Payments Gateway' application CI accounts for ~35% of priority-1 incidents while carrying ~5% of all incidents.",
        "ci_name": PAYMENTS_GATEWAY,
        "metric": "share of priority=1 incidents whose cmdb_ci is Payments Gateway; share of all incidents",
        "expected": {"p1_share": 0.35, "all_share": PAYMENTS_GATEWAY_SHARE},
        "tolerance": {"p1_share": 0.08, "all_share": 0.02},
        "tables": ["incident", "cmdb_ci"],
        "columns": ["incident.priority", "incident.cmdb_ci", "cmdb_ci.name"],
    },
    "emergency_change_incidents": {
        "description": "Incidents caused_by a change cluster within 48 hours after emergency changes; emergency changes cause far more incidents per change.",
        "metric": "share of caused_by incidents opened within 48h after the change end_date, emergency vs other; incidents per change by type",
        "expected": {"within_48h_share_emergency": 0.95, "within_48h_share_other": 0.07, "incidents_per_change_ratio_min": 10.0},
        "tolerance": {"within_48h_share_emergency": 0.05, "within_48h_share_other": 0.06},
        "tables": ["incident", "change_request"],
        "columns": ["incident.caused_by", "incident.opened_at", "change_request.type", "change_request.end_date"],
    },
    "network_ops_reassignment": {
        "description": "Assignment group 'Network Operations' has the highest mean reassignment_count.",
        "group_name": "Network Operations",
        "metric": "argmax over assignment_group of mean(reassignment_count)",
        "expected": "Network Operations",
        "tables": ["incident", "sys_user_group"],
        "columns": ["incident.assignment_group", "incident.reassignment_count", "sys_user_group.name"],
    },
}

DATA_QUALITY: dict[str, dict[str, Any]] = {
    "resolved_before_opened": {"table": "incident", "approx_share_of_resolved": 0.01, "check": "resolved_at < opened_at"},
    "null_assignment_group": {"table": "incident", "approx_share": 0.03, "check": "assignment_group IS NULL"},
    "duplicate_numbers": {"table": "incident", "count": 15, "check": "number appears more than once"},
    "future_opened_at": {"table": "incident", "count": 5, "check": f"opened_at > '{PERIOD_END:%Y-%m-%d}'"},
}

# ServiceNow dictionary per table: element -> (internal_type, label, reference, max_length)
DICTIONARY: dict[str, list[tuple[str, str, str, str | None, int]]] = {
    "incident": [
        ("sys_id", "GUID", "Sys ID", None, 32),
        ("number", "string", "Number", None, 40),
        ("opened_at", "glide_date_time", "Opened", None, 40),
        ("resolved_at", "glide_date_time", "Resolved", None, 40),
        ("closed_at", "glide_date_time", "Closed", None, 40),
        ("priority", "integer", "Priority", None, 40),
        ("impact", "integer", "Impact", None, 40),
        ("urgency", "integer", "Urgency", None, 40),
        ("state", "integer", "State", None, 40),
        ("category", "choice", "Category", None, 40),
        ("assignment_group", "reference", "Assignment group", "sys_user_group", 32),
        ("cmdb_ci", "reference", "Configuration item", "cmdb_ci", 32),
        ("caused_by", "reference", "Caused by Change", "change_request", 32),
        ("reassignment_count", "integer", "Reassignment count", None, 40),
        ("reopen_count", "integer", "Reopen count", None, 40),
        ("made_sla", "boolean", "Made SLA", None, 40),
        ("contact_type", "choice", "Channel", None, 40),
        ("short_description", "string", "Short description", None, 160),
        ("caller_id", "reference", "Caller", "sys_user", 32),
        ("sys_updated_on", "glide_date_time", "Updated", None, 40),
    ],
    "change_request": [
        ("sys_id", "GUID", "Sys ID", None, 32),
        ("number", "string", "Number", None, 40),
        ("type", "choice", "Type", None, 40),
        ("risk", "integer", "Risk", None, 40),
        ("state", "integer", "State", None, 40),
        ("start_date", "glide_date_time", "Planned start date", None, 40),
        ("end_date", "glide_date_time", "Planned end date", None, 40),
        ("assignment_group", "reference", "Assignment group", "sys_user_group", 32),
        ("cmdb_ci", "reference", "Configuration item", "cmdb_ci", 32),
        ("close_code", "choice", "Close code", None, 40),
        ("short_description", "string", "Short description", None, 160),
        ("sys_updated_on", "glide_date_time", "Updated", None, 40),
    ],
    "sys_user_group": [
        ("sys_id", "GUID", "Sys ID", None, 32),
        ("name", "string", "Name", None, 80),
        ("description", "string", "Description", None, 1000),
        ("active", "boolean", "Active", None, 40),
        ("sys_updated_on", "glide_date_time", "Updated", None, 40),
    ],
    "cmdb_ci": [
        ("sys_id", "GUID", "Sys ID", None, 32),
        ("name", "string", "Name", None, 255),
        ("sys_class_name", "string", "Class", None, 80),
        ("operational_status", "integer", "Operational status", None, 40),
        ("business_criticality", "choice", "Business criticality", None, 40),
        ("support_group", "reference", "Support group", "sys_user_group", 32),
        ("sys_updated_on", "glide_date_time", "Updated", None, 40),
    ],
}
TABLE_LABELS = {
    "incident": "Incident",
    "change_request": "Change Request",
    "sys_user_group": "Group",
    "cmdb_ci": "Configuration Item",
}

# Choice labels (what sysparm_display_value returns for choice fields).
CHOICE_LABELS: dict[str, dict[str, dict[str, str]]] = {
    "incident": {
        "priority": {"1": "1 - Critical", "2": "2 - High", "3": "3 - Moderate", "4": "4 - Low", "5": "5 - Planning"},
        "impact": {"1": "1 - High", "2": "2 - Medium", "3": "3 - Low"},
        "urgency": {"1": "1 - High", "2": "2 - Medium", "3": "3 - Low"},
        "state": {str(k): v for k, v in INCIDENT_STATES.items()},
        "category": {c: c.capitalize() for c in CATEGORIES},
        "contact_type": {"phone": "Phone", "email": "Email", "self-service": "Self-service", "walk-in": "Walk-in", "monitoring": "Monitoring"},
    },
    "change_request": {
        "type": {"standard": "Standard", "normal": "Normal", "emergency": "Emergency"},
        "risk": {"1": "Very High", "2": "High", "3": "Moderate", "4": "Low"},
        "state": {str(k): v for k, v in CHANGE_STATES.items()},
        "close_code": {"successful": "Successful", "successful_issues": "Successful with issues", "unsuccessful": "Unsuccessful"},
    },
    "cmdb_ci": {
        "operational_status": {"1": "Operational", "2": "Non-Operational"},
    },
}


def user_display_name(sys_id: str) -> str:
    """Deterministic display name for a (synthetic) sys_user reference."""
    return f"User {sys_id[:6].upper()}"


_ARROW_TYPES = {
    "GUID": pa.string(),
    "string": pa.string(),
    "choice": pa.string(),
    "reference": pa.string(),
    "integer": pa.int64(),
    "boolean": pa.bool_(),
    "glide_date_time": pa.timestamp("us"),
}


def arrow_schema(table: str) -> pa.Schema:
    return pa.schema([pa.field(e, _ARROW_TYPES[t]) for e, t, *_ in DICTIONARY[table]])


def _sys_id(seed: int, kind: str, i: int) -> str:
    return hashlib.md5(f"{seed}:{kind}:{i}".encode()).hexdigest()


def _sys_ids(seed: int, kind: str, n: int) -> np.ndarray:
    return np.array([_sys_id(seed, kind, i) for i in range(n)], dtype=object)


def _to_datetime_array(base: datetime, seconds: np.ndarray) -> np.ndarray:
    return np.datetime64(base, "us") + (seconds * 1_000_000).astype("int64").astype("timedelta64[us]")


def _after_hours(ts: np.ndarray) -> np.ndarray:
    days = ts.astype("datetime64[D]")
    hours = ((ts - days).astype("timedelta64[h]")).astype(int)
    weekday = (days.astype("int64") + 3) % 7  # 1970-01-01 was a Thursday -> Monday=0
    return (hours < 8) | (hours >= 18) | (weekday >= 5)


@lru_cache(maxsize=4)
def generate_servicenow_data(
    seed: int = DEFAULT_SEED,
    n_incidents: int = 20_000,
    n_changes: int = 3_000,
) -> dict[str, pa.Table]:
    """Return {"incident", "change_request", "sys_user_group", "cmdb_ci"} as pyarrow Tables."""
    rng = np.random.default_rng(seed)
    period_seconds = (PERIOD_END - PERIOD_START).total_seconds()
    base_ts = np.datetime64(PERIOD_START, "us")

    # --- sys_user_group -------------------------------------------------------------------
    n_groups = len(GROUPS)
    group_ids = _sys_ids(seed, "grp", n_groups)
    group_by_name = dict(zip(GROUPS, group_ids, strict=True))
    groups = pa.table(
        {
            "sys_id": pa.array(group_ids, pa.string()),
            "name": pa.array(GROUPS, pa.string()),
            "description": pa.array([f"{g} resolver group" for g in GROUPS], pa.string()),
            "active": pa.array([True] * n_groups, pa.bool_()),
            "sys_updated_on": pa.array([datetime(2025, 6, 1) + timedelta(days=i) for i in range(n_groups)], pa.timestamp("us")),
        },
        schema=arrow_schema("sys_user_group"),
    )

    # --- cmdb_ci -------------------------------------------------------------------------
    n_ci = len(CI_NAMES)
    ci_ids = _sys_ids(seed, "ci", n_ci)
    ci_classes = ["cmdb_ci_appl"] * 33 + ["cmdb_ci_cluster"] * 4 + ["cmdb_ci_ip_switch"] * 2 + ["cmdb_ci_ip_firewall"]
    ci_support = [group_by_name["Payments Engineering"]] + [group_ids[i % n_groups] for i in range(1, n_ci)]
    ci_crit = ["1 - most critical"] + [["1 - most critical", "2 - somewhat critical", "3 - less critical"][i % 3] for i in range(1, n_ci)]
    cis = pa.table(
        {
            "sys_id": pa.array(ci_ids, pa.string()),
            "name": pa.array(CI_NAMES, pa.string()),
            "sys_class_name": pa.array(ci_classes, pa.string()),
            "operational_status": pa.array([1] * n_ci, pa.int64()),
            "business_criticality": pa.array(ci_crit, pa.string()),
            "support_group": pa.array(ci_support, pa.string()),
            "sys_updated_on": pa.array([datetime(2025, 7, 1) + timedelta(hours=7 * i) for i in range(n_ci)], pa.timestamp("us")),
        },
        schema=arrow_schema("cmdb_ci"),
    )
    # CI selection: Payments Gateway gets a fixed share; the rest is spread (skewed) over others.
    other_w = rng.gamma(2.0, 1.0, n_ci - 1)
    ci_p = np.concatenate([[PAYMENTS_GATEWAY_SHARE], (1 - PAYMENTS_GATEWAY_SHARE) * other_w / other_w.sum()])

    # --- change_request ------------------------------------------------------------------
    types = np.array(list(CHANGE_TYPE_P))
    ch_type = rng.choice(types, n_changes, p=list(CHANGE_TYPE_P.values()))
    ch_start_s = rng.uniform(0, period_seconds - 3 * 86400, n_changes)
    ch_dur_s = rng.uniform(1, 8, n_changes) * 3600
    ch_start = _to_datetime_array(PERIOD_START, ch_start_s)
    ch_end = _to_datetime_array(PERIOD_START, ch_start_s + ch_dur_s)
    ch_ci_idx = rng.choice(n_ci, n_changes, p=ci_p)
    ch_group_idx = rng.integers(0, n_groups, n_changes)
    ch_risk = np.where(ch_type == "emergency", rng.choice([2, 3], n_changes, p=[0.7, 0.3]), rng.choice([2, 3, 4], n_changes, p=[0.1, 0.4, 0.5]))
    ch_state = rng.choice([3, 4, 0], n_changes, p=[0.92, 0.05, 0.03])
    close_code = np.where(
        ch_state == 3,
        rng.choice(["successful", "successful_issues", "unsuccessful"], n_changes, p=[0.85, 0.1, 0.05]),
        None,
    )
    ch_ids = _sys_ids(seed, "chg", n_changes)
    ch_desc = np.array(["Deploy release", "Patch servers", "Firewall rule update", "Database upgrade", "Config change"], dtype=object)[
        rng.integers(0, 5, n_changes)
    ]
    changes = pa.table(
        {
            "sys_id": pa.array(ch_ids, pa.string()),
            "number": pa.array([f"CHG{30000 + i:07d}" for i in range(n_changes)], pa.string()),
            "type": pa.array(ch_type, pa.string()),
            "risk": pa.array(ch_risk, pa.int64()),
            "state": pa.array(ch_state, pa.int64()),
            "start_date": pa.array(ch_start, pa.timestamp("us")),
            "end_date": pa.array(ch_end, pa.timestamp("us")),
            "assignment_group": pa.array(group_ids[ch_group_idx], pa.string()),
            "cmdb_ci": pa.array(ci_ids[ch_ci_idx], pa.string()),
            "close_code": pa.array(close_code, pa.string()),
            "short_description": pa.array(ch_desc, pa.string()),
            "sys_updated_on": pa.array(ch_end + np.timedelta64(2, "h"), pa.timestamp("us")),
        },
        schema=arrow_schema("change_request"),
    )

    # --- incident ------------------------------------------------------------------------
    n = n_incidents
    # Opening time: 68% during business hours on weekdays, the rest after hours / weekends.
    day = rng.integers(0, int(period_seconds // 86400), n)
    business = rng.random(n) < 0.68
    day_start = base_ts + day.astype("timedelta64[D]").astype("timedelta64[us]")
    weekday = (day_start.astype("datetime64[D]").astype("int64") + 3) % 7
    # business rows: move weekend days to the preceding Friday; hour in [8, 18)
    shift = np.where(business & (weekday == 5), 1, np.where(business & (weekday == 6), 2, 0))
    day_start = day_start - shift.astype("timedelta64[D]").astype("timedelta64[us]")
    bh_seconds = rng.uniform(8 * 3600, 18 * 3600, n)
    ah_seconds = np.where(rng.random(n) < 0.5, rng.uniform(0, 8 * 3600, n), rng.uniform(18 * 3600, 24 * 3600, n))
    opened = day_start + (np.where(business, bh_seconds, ah_seconds) * 1e6).astype("int64").astype("timedelta64[us]")

    ci_idx = rng.choice(n_ci, n, p=ci_p)
    caused_by = np.full(n, None, dtype=object)

    # Change-caused incidents (GT d): ~6% of incidents, 75% of them tied to emergency changes and
    # opened within 48h after the change ends; the rest follow normal/standard changes and are
    # spread over the following 30 days.
    emerg_idx = np.flatnonzero(ch_type == "emergency")
    other_idx = np.flatnonzero(ch_type != "emergency")
    n_caused = int(n * 0.06)
    caused_rows = rng.choice(n, n_caused, replace=False)
    from_emergency = rng.random(n_caused) < 0.75
    chg_pick = np.where(from_emergency, rng.choice(emerg_idx, n_caused), rng.choice(other_idx, n_caused))
    lag_s = np.where(
        from_emergency,
        np.minimum(rng.exponential(12 * 3600, n_caused), 47.5 * 3600),
        rng.uniform(0, 30 * 86400, n_caused),
    )
    new_open = ch_end[chg_pick] + (lag_s * 1e6).astype("int64").astype("timedelta64[us]")
    in_period = new_open < np.datetime64(PERIOD_END, "us")
    caused_rows, chg_pick, new_open = caused_rows[in_period], chg_pick[in_period], new_open[in_period]
    opened[caused_rows] = new_open
    caused_by[caused_rows] = ch_ids[chg_pick]
    ci_idx[caused_rows] = ch_ci_idx[chg_pick]

    after_hours = _after_hours(opened)
    is_pg = ci_idx == 0

    priority = np.where(
        is_pg,
        rng.choice([1, 2, 3, 4, 5], n, p=PRIORITY_P_PG),
        rng.choice([1, 2, 3, 4, 5], n, p=PRIORITY_P_OTHER),
    )
    impact = np.empty(n, dtype=np.int64)
    urgency = np.empty(n, dtype=np.int64)
    pick = rng.random(n)
    for p, cells in PRIORITY_MATRIX.items():
        mask = priority == p
        k = np.minimum((pick[mask] * len(cells)).astype(int), len(cells) - 1)
        impact[mask] = np.array([c[0] for c in cells])[k]
        urgency[mask] = np.array([c[1] for c in cells])[k]

    category = rng.choice(np.array(CATEGORIES), n, p=CATEGORY_P)
    category = np.where(is_pg, np.where(rng.random(n) < 0.8, "software", category), category)
    group_idx = np.empty(n, dtype=np.int64)
    gi = {g: i for i, g in enumerate(GROUPS)}
    for cat in CATEGORIES:
        mask = category == cat
        owners = np.array([gi[g] for g in CATEGORY_GROUPS[cat]])
        group_idx[mask] = rng.choice(owners, int(mask.sum()), p=CATEGORY_GROUP_P[cat])
    lam = np.array([GROUP_REASSIGN_LAMBDA[g] for g in GROUPS])[group_idx]
    reassignment = rng.poisson(lam)
    breach_p = np.where(reassignment <= 1, BREACH_P_LE1, np.where(reassignment == 2, BREACH_P_EQ2, BREACH_P_GE3))
    made_sla = rng.random(n) >= breach_p

    median_h = np.array([RESOLUTION_MEDIAN_HOURS[int(p)] for p in range(1, 6)])[priority - 1]
    res_hours = median_h * np.exp(rng.normal(0, 0.6, n)) * np.where(after_hours, AFTER_HOURS_FACTOR, 1.0)
    resolved = opened + (res_hours * 3600e6).astype("int64").astype("timedelta64[us]")
    close_lag = (rng.uniform(1, 7, n) * 86400e6).astype("int64").astype("timedelta64[us]")
    closed = resolved + close_lag
    end64 = np.datetime64(PERIOD_END, "us")
    open_still = resolved > end64
    is_closed = (~open_still) & (closed <= end64)
    state = np.where(open_still, rng.choice([1, 2, 3], n, p=[0.2, 0.6, 0.2]), np.where(is_closed, 7, 6))
    resolved_arr = np.where(open_still, None, resolved.astype(object))
    closed_arr = np.where(is_closed, closed.astype(object), None)
    reopen = rng.poisson(0.05, n)

    # --- data-quality defects --------------------------------------------------------------
    resolved_rows = np.flatnonzero(~open_still)
    bad_res = rng.choice(resolved_rows, max(1, int(len(resolved_rows) * 0.01)), replace=False)
    for r in bad_res:
        resolved_arr[r] = (opened[r] - np.timedelta64(int(rng.uniform(1, 48) * 3600e6), "us")).astype(object)
    null_group = rng.random(n) < 0.03
    future_rows = rng.choice(np.setdiff1d(np.arange(n), caused_rows), 5, replace=False)
    for k, r in enumerate(future_rows):
        opened[r] = np.datetime64(datetime(2026, 11, 1) + timedelta(days=17 * k, hours=10), "us")
        resolved_arr[r] = None
        closed_arr[r] = None
        state[r] = 1

    numbers = np.array([f"INC{10001 + i:07d}" for i in range(n)], dtype=object)
    dup_src = rng.choice(n, 15, replace=False)
    dup_dst = rng.choice(np.setdiff1d(np.arange(n), dup_src), 15, replace=False)
    numbers[dup_dst] = numbers[dup_src]

    group_col = np.where(null_group, None, group_ids[group_idx])
    desc_idx = rng.integers(0, 4, n)
    short_desc = np.array([SHORT_DESCRIPTIONS[c][i] for c, i in zip(category, desc_idx, strict=True)], dtype=object)
    callers = _sys_ids(seed, "usr", 800)
    caller = callers[rng.integers(0, 800, n)]
    contact = rng.choice(np.array(CONTACT_TYPES), n, p=CONTACT_P)
    updated = np.array(
        [c if c is not None else (r if r is not None else o) for o, r, c in zip(opened.astype(object), resolved_arr, closed_arr, strict=True)],
        dtype=object,
    )
    incidents = pa.table(
        {
            "sys_id": pa.array(_sys_ids(seed, "inc", n), pa.string()),
            "number": pa.array(numbers, pa.string()),
            "opened_at": pa.array(opened, pa.timestamp("us")),
            "resolved_at": pa.array(resolved_arr, pa.timestamp("us")),
            "closed_at": pa.array(closed_arr, pa.timestamp("us")),
            "priority": pa.array(priority, pa.int64()),
            "impact": pa.array(impact, pa.int64()),
            "urgency": pa.array(urgency, pa.int64()),
            "state": pa.array(state, pa.int64()),
            "category": pa.array(category, pa.string()),
            "assignment_group": pa.array(group_col, pa.string()),
            "cmdb_ci": pa.array(ci_ids[ci_idx], pa.string()),
            "caused_by": pa.array(caused_by, pa.string()),
            "reassignment_count": pa.array(reassignment, pa.int64()),
            "reopen_count": pa.array(reopen, pa.int64()),
            "made_sla": pa.array(made_sla, pa.bool_()),
            "contact_type": pa.array(contact, pa.string()),
            "short_description": pa.array(short_desc, pa.string()),
            "caller_id": pa.array(caller, pa.string()),
            "sys_updated_on": pa.array(updated, pa.timestamp("us")),
        },
        schema=arrow_schema("incident"),
    )
    tables = {"incident": incidents, "change_request": changes, "sys_user_group": groups, "cmdb_ci": cis}
    return {name: _truncate_to_seconds(t) for name, t in tables.items()}


def _truncate_to_seconds(table: pa.Table) -> pa.Table:
    """ServiceNow stores glide_date_time at second precision."""
    for i, f in enumerate(table.schema):
        if pa.types.is_timestamp(f.type):
            col = pc.floor_temporal(table.column(i), unit="second")
            table = table.set_column(i, f, col)
    return table


def servicenow_tables(seed: int = DEFAULT_SEED) -> dict[str, pa.Table]:
    """Alias kept short for callers (mock server, fixtures)."""
    return generate_servicenow_data(seed)
