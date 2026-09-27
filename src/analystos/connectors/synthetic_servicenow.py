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
    """Deterministic display name for a (synthetic) sys_user reference: a fulfiller's name for the
    assignee pool of the activity data, otherwise a neutral caller label."""
    return AGENT_NAMES.get(sys_id) or f"User {sys_id[:6].upper()}"


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


# =============================================================================================
# Activity data for process and task mining: catalog tasks and a flattened activity log
# =============================================================================================
# The four tables above never change (tests and dated evidence depend on them): the activity data is
# derived from them with its own random stream, so adding it leaves their bytes identical.
#
# `u_task_activity` is a custom table, the shape a `sys_audit` / `metric_instance` export flattens to:
# one row per lifecycle step of a task record (incident, change_request, sc_task), with the record's
# number, the step, when it happened, the group and person holding the record after it, the state before
# and after, and its position in the record's history. The export covers records opened in the last six
# months (ACTIVITY_START .. PERIOD_END, the audit retention window) and has no history for incident rows
# carrying the injected DATA_QUALITY defects (their timestamps or group are wrong in the record, not in
# the audit trail).
ACTIVITY_START = datetime(2026, 3, 1)
N_CATALOG_TASKS = 6_000

INCIDENT_ACTIVITIES = ["Created", "Assigned", "Reassigned", "Work started", "On hold", "Resumed", "Resolved", "Reopened",
                       "Closed", "Cancelled"]
CHANGE_ACTIVITIES = ["Created", "Assessed", "Authorized", "Scheduled", "Implementation started", "Implemented", "Reviewed",
                     "Closed", "Cancelled", "Failed"]
# The happy path of each record type (what packs/itsm/process_models.yaml declares as the reference model).
REFERENCE_PATHS: dict[str, list[str]] = {
    "incident": ["Created", "Assigned", "Work started", "Resolved", "Closed"],
    "change_request": ["Created", "Assessed", "Authorized", "Scheduled", "Implementation started", "Implemented", "Reviewed",
                       "Closed"],
    "sc_task": ["Created", "Assigned", "Work started", "Closed"],
}

SC_TASK_STATES = {-5: "Pending", 1: "Open", 2: "Work in Progress", 3: "Closed Complete", 4: "Closed Incomplete",
                  7: "Closed Skipped"}
# Catalog items and the group that fulfils them.
CATALOG_ITEMS: list[tuple[str, str, float]] = [
    ("New laptop", "End User Computing", 0.22),
    ("Monitor or peripheral", "End User Computing", 0.12),
    ("Software license", "Application Support", 0.16),
    ("Access request", "Identity and Access", 0.18),
    ("VPN token", "Network Operations", 0.08),
    ("Database access", "Database Administration", 0.08),
    ("New virtual machine", "Cloud Platform", 0.10),
    ("Mailbox change", "Windows Operations", 0.06),
]
# Hardware requests wait for stock: End User Computing puts most catalog tasks on hold (GT catalog_on_hold).
CATALOG_HOLD_P = {"End User Computing": 0.70}
CATALOG_HOLD_P_OTHER = 0.10
CATALOG_HOLD_MEDIAN_H = {"End User Computing": 72.0}
CATALOG_HOLD_MEDIAN_H_OTHER = 10.0
CATALOG_WORK_MEDIAN_H = 18.0
CATALOG_CANCEL_P = 0.08
CATALOG_CANCEL_BEFORE_WORK_P = 0.75
# Network Operations bounces incidents with these partner groups (GT activity_reassignment_loops).
PING_PONG_PARTNERS = ["Cloud Platform", "Security Operations"]
INCIDENT_HOLD_P = 0.12
# Changes: emergency changes often skip formal authorization (GT change_authorization_skipped); cancelled
# database upgrades are pulled after they were scheduled, other changes at assessment (GT change_cancel_point).
SKIP_AUTH_P = {"emergency": 0.30, "normal": 0.02, "standard": 0.02}
CANCEL_AFTER_SCHEDULED_P = {"Database upgrade": 0.85}
CANCEL_AFTER_SCHEDULED_P_OTHER = 0.15

_FIRST = ["Priya", "James", "Aisha", "Tom", "Mei", "Carlos", "Fatima", "Lukas", "Grace", "Omar", "Sofia", "Daniel",
          "Hannah", "Ravi", "Elena", "Kwame", "Yuki", "Liam", "Nadia", "Marco"]
_LAST = ["Shah", "Wilson", "Khan", "Becker", "Chen", "Silva", "Ahmed", "Novak", "Okafor", "Rossi", "Kim", "Murphy",
         "Haddad", "Iyer", "Petrov", "Mensah", "Tanaka", "Brennan", "Costa", "Larsen"]
AGENTS_PER_GROUP = 6


def _agent_pool(seed: int = DEFAULT_SEED) -> dict[str, list[tuple[str, str]]]:
    """Fulfillers per group: (sys_user sys_id, display name). Names are synthetic and deterministic."""
    pool: dict[str, list[tuple[str, str]]] = {}
    k = 0
    for g in GROUPS:
        people = []
        for _ in range(AGENTS_PER_GROUP):
            name = f"{_FIRST[k % len(_FIRST)]} {_LAST[(k * 7 + k // len(_FIRST)) % len(_LAST)]}"
            people.append((_sys_id(seed, "agent", k), name))
            k += 1
        pool[g] = people
    return pool


AGENT_NAMES: dict[str, str] = {sid: name for people in _agent_pool().values() for sid, name in people}

GROUND_TRUTH.update({
    "activity_reassignment_loops": {
        "description": "In the incident activity log, Network Operations bounces tickets with Cloud Platform and Security "
                       "Operations: the most frequent group-to-group handover pair involves Network Operations, and it has "
                       "the highest mean number of Reassigned steps per incident.",
        "metric": "top unordered (from, to) assignment_group handover pair; argmax mean(Reassigned per case) by final group",
        "expected": {"top_pair_contains": "Network Operations", "top_mean_reassigned_group": "Network Operations"},
        "tables": ["u_task_activity"],
        "columns": ["u_task_activity.assignment_group", "u_task_activity.activity"],
    },
    "change_authorization_skipped": {
        "description": "About 30% of closed emergency changes (2% of others) went from Assessed straight to Scheduled "
                       "without the Authorized step: a conformance violation against the change reference path.",
        "metric": "share of closed changes without an Authorized step, emergency vs other",
        "expected": {"emergency": SKIP_AUTH_P["emergency"], "other": SKIP_AUTH_P["normal"]},
        "tolerance": {"emergency": 0.12, "other": 0.02},
        "tables": ["u_task_activity", "change_request"],
        "columns": ["u_task_activity.activity", "change_request.type"],
    },
    "change_cancel_point": {
        "description": "Cancelled 'Database upgrade' changes are mostly cancelled after being Scheduled (~85%); other "
                       "cancelled changes are mostly cancelled right after assessment (~85%).",
        "metric": "share of cancelled changes whose step before Cancelled is Scheduled, by short_description",
        "expected": {"database_upgrade": CANCEL_AFTER_SCHEDULED_P["Database upgrade"], "other": CANCEL_AFTER_SCHEDULED_P_OTHER},
        "tolerance": {"database_upgrade": 0.3, "other": 0.15},
        "tables": ["u_task_activity", "change_request"],
        "columns": ["u_task_activity.activity", "change_request.short_description"],
    },
    "catalog_on_hold_fulfilment": {
        "description": "End User Computing puts ~70% of its catalog tasks On hold (awaiting stock, median 72 h) against ~10% "
                       "elsewhere, so its median fulfilment time is more than twice that of the other groups.",
        "metric": "share of sc_task cases with On hold, End User Computing vs others; median(closed_at - opened_at) ratio",
        "expected": {"on_hold_share_euc": 0.70, "on_hold_share_other": 0.10, "median_ratio_min": 2.0},
        "tolerance": {"on_hold_share_euc": 0.06, "on_hold_share_other": 0.04},
        "tables": ["sc_task", "u_task_activity"],
        "columns": ["sc_task.assignment_group", "sc_task.opened_at", "sc_task.closed_at", "u_task_activity.activity"],
    },
    "catalog_cancelled_before_work": {
        "description": "About 8% of catalog tasks are cancelled (Closed Skipped); three quarters of them before any work "
                       "started (the step before Cancelled is Assigned or Reassigned).",
        "metric": "share of sc_task ending in Cancelled; share of those whose previous step is not Work started",
        "expected": {"cancelled_share": CATALOG_CANCEL_P, "before_work_share": CATALOG_CANCEL_BEFORE_WORK_P},
        "tolerance": {"cancelled_share": 0.02, "before_work_share": 0.08},
        "tables": ["sc_task", "u_task_activity"],
        "columns": ["sc_task.state", "u_task_activity.activity"],
    },
})

DICTIONARY["sc_task"] = [
    ("sys_id", "GUID", "Sys ID", None, 32),
    ("number", "string", "Number", None, 40),
    ("request_item", "string", "Request item", None, 40),
    ("short_description", "string", "Short description", None, 160),
    ("state", "integer", "State", None, 40),
    ("priority", "integer", "Priority", None, 40),
    ("assignment_group", "reference", "Assignment group", "sys_user_group", 32),
    ("assigned_to", "reference", "Assigned to", "sys_user", 32),
    ("opened_at", "glide_date_time", "Opened", None, 40),
    ("closed_at", "glide_date_time", "Closed", None, 40),
    ("reassignment_count", "integer", "Reassignment count", None, 40),
    ("sys_updated_on", "glide_date_time", "Updated", None, 40),
]
DICTIONARY["u_task_activity"] = [
    ("sys_id", "GUID", "Sys ID", None, 32),
    ("task_type", "string", "Task type", None, 40),
    ("task_sys_id", "document_id", "Task", None, 32),
    ("task_number", "string", "Task number", None, 40),
    ("activity", "string", "Activity", None, 40),
    ("activity_at", "glide_date_time", "Activity time", None, 40),
    ("assignment_group", "string", "Assignment group", None, 80),
    ("assigned_to", "string", "Assigned to", None, 80),
    ("state_before", "string", "State before", None, 40),
    ("state_after", "string", "State after", None, 40),
    ("sequence", "integer", "Sequence", None, 40),
]
TABLE_LABELS.update({"sc_task": "Catalog Task", "u_task_activity": "Task Activity"})
CHOICE_LABELS["sc_task"] = {
    "state": {str(k): v for k, v in SC_TASK_STATES.items()},
    "priority": CHOICE_LABELS["incident"]["priority"],
}
_ARROW_TYPES["document_id"] = pa.string()


class _Log:
    """Column buffers of the activity log; `case()` appends one record's ordered steps."""

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.cols: dict[str, list[Any]] = {e: [] for e, *_ in DICTIONARY["u_task_activity"]}

    def case(self, task_type: str, sys_id: str, number: str, steps: list[tuple[str, np.datetime64, str | None, str | None]],
             states: dict[str, str | None], start_state: str | None = None) -> None:
        """steps: (activity, time, group, assignee). `states` maps an activity to the state it leaves the record
        in (None keeps the current state)."""
        state = start_state
        prev_t: np.datetime64 | None = None
        for i, (activity, t, group, person) in enumerate(steps, 1):
            if prev_t is not None and t <= prev_t:  # strictly increasing at second precision
                t = prev_t + np.timedelta64(1, "s")
            prev_t = t
            after = states.get(activity) or state
            c = self.cols
            c["sys_id"].append(_sys_id(self.seed, "act", len(c["sys_id"])))
            c["task_type"].append(task_type)
            c["task_sys_id"].append(sys_id)
            c["task_number"].append(number)
            c["activity"].append(activity)
            c["activity_at"].append(t)
            c["assignment_group"].append(group)
            c["assigned_to"].append(person)
            c["state_before"].append(state)
            c["state_after"].append(after)
            c["sequence"].append(i)
            state = after

    def table(self) -> pa.Table:
        data = dict(self.cols)
        data["activity_at"] = np.array(data["activity_at"], dtype="datetime64[s]").astype("datetime64[us]")
        return pa.table({k: pa.array(v, _ARROW_TYPES[t]) for (k, t, *_), v in
                         zip(DICTIONARY["u_task_activity"], data.values(), strict=True)},
                        schema=arrow_schema("u_task_activity"))


def _sec(t: Any) -> np.datetime64:
    return np.datetime64(t, "s")


def _midpoint(a: np.datetime64, b: np.datetime64) -> np.datetime64:
    return a + np.timedelta64(int((b - a) / np.timedelta64(2, "s")), "s")


def _spread(rng: np.random.Generator, start: np.datetime64, end: np.datetime64, weights: list[float]) -> list[np.datetime64]:
    """Times of len(weights) steps inside (start, end]: gap i ~ weight_i * Exp(1), scaled so the last step lands on end."""
    total = max(int((end - start) / np.timedelta64(1, "s")), len(weights))
    gaps = np.asarray(weights, dtype=float) * rng.exponential(1.0, len(weights))
    offsets = [int(round(x)) for x in np.cumsum(gaps) / gaps.sum() * total]
    offsets[-1] = total
    for i in range(len(offsets) - 2, -1, -1):  # strictly increasing, the last step exactly on `end`
        offsets[i] = min(offsets[i], offsets[i + 1] - 1)
    for i in range(len(offsets)):
        offsets[i] = max(offsets[i], i + 1, offsets[i - 1] + 1 if i else 1)
    return [start + np.timedelta64(x, "s") for x in offsets]


def _chain(rng: np.random.Generator, final: str, k: int, category: str | None) -> list[str]:
    """Groups holding a record from its first assignment to its last (k reassignments, the last one is `final`)."""
    groups = [final]
    for _ in range(k):
        nxt = groups[0]
        if final == "Network Operations":
            options = [PING_PONG_PARTNERS[int(rng.integers(0, 2))]] if nxt == final else [final]
        else:
            owners = CATEGORY_GROUPS.get(category or "", GROUPS)
            options = [g for g in owners if g != nxt] or [g for g in GROUPS if g != nxt]
        groups.insert(0, options[int(rng.integers(0, len(options)))])
    return groups


@lru_cache(maxsize=2)
def generate_activity_data(seed: int = DEFAULT_SEED) -> dict[str, pa.Table]:
    """Return {"sc_task", "u_task_activity"}, consistent with the tables of `generate_servicenow_data(seed)`:
    Created at opened_at, the last Resolved at resolved_at, Closed at closed_at, one Reassigned step per
    reassignment_count, one Reopened per reopen_count, cancelled records ending in Cancelled."""
    base = generate_servicenow_data(seed)
    rng = np.random.default_rng(seed + 101)
    pool = _agent_pool(seed)
    group_name = dict(zip(base["sys_user_group"]["sys_id"].to_pylist(), base["sys_user_group"]["name"].to_pylist(), strict=True))
    window = _sec(ACTIVITY_START)
    end = _sec(PERIOD_END)
    log = _Log(seed)

    def person(group: str | None) -> str | None:
        return pool[group][int(rng.integers(0, AGENTS_PER_GROUP))][1] if group else None

    # --- incidents ---------------------------------------------------------------------------
    inc_states = {"Created": "New", "Work started": "In Progress", "On hold": "On Hold", "Resumed": "In Progress",
                  "Resolved": "Resolved", "Reopened": "In Progress", "Closed": "Closed", "Cancelled": "Canceled"}
    inc = base["incident"].to_pydict()
    for i in range(len(inc["sys_id"])):
        opened, resolved, closed = inc["opened_at"][i], inc["resolved_at"][i], inc["closed_at"][i]
        group = group_name.get(inc["assignment_group"][i]) if inc["assignment_group"][i] else None
        if opened < ACTIVITY_START or opened > PERIOD_END or group is None or (resolved is not None and resolved < opened):
            continue
        k, m, state = inc["reassignment_count"][i], inc["reopen_count"][i], inc["state"][i]
        o = _sec(opened)
        chain = _chain(rng, group, k, inc["category"][i])
        hold = rng.random() < INCIDENT_HOLD_P and state != 1
        # the ordered middle steps and the relative weight of the wait before each
        mid: list[tuple[str, float]] = [("Assigned", 0.15)] + [("Reassigned", 2.0)] * k
        if state != 1:
            mid.append(("Work started", 3.0))
        if hold and state != 3:
            mid += [("On hold", 1.5), ("Resumed", 4.0)]
        mid += [("Resolved", 5.0), ("Reopened", 1.0)] * m
        if resolved is not None:
            mid.append(("Resolved", 5.0))
            stop = _sec(resolved)
        else:
            if state == 3:
                mid.append(("On hold", 1.5))
            stop = o + np.timedelta64(int((end - o) / np.timedelta64(1, "s") * rng.uniform(0.3, 0.95)), "s")
        times = _spread(rng, o, stop, [w for _, w in mid])
        steps: list[tuple[str, np.datetime64, str | None, str | None]] = [("Created", o, None, None)]
        g_i, who = -1, None
        for (activity, _), t in zip(mid, times, strict=True):
            if activity in ("Assigned", "Reassigned"):
                g_i += 1
                who = person(chain[g_i])
            steps.append((activity, t, chain[max(g_i, 0)], who))
        if closed is not None:
            steps.append(("Closed", _sec(closed), group, who))
        log.case("incident", inc["sys_id"][i], inc["number"][i], steps, inc_states)

    # --- change requests ---------------------------------------------------------------------
    chg_states = {"Created": "New", "Assessed": "Authorize", "Authorized": "Scheduled", "Scheduled": "Scheduled",
                  "Implementation started": "Implement", "Implemented": "Review", "Failed": "Review", "Reviewed": "Review",
                  "Closed": "Closed", "Cancelled": "Canceled"}
    chg = base["change_request"].to_pydict()
    lead_s = {"standard": (1 * 86400, 3 * 86400), "normal": (3 * 86400, 14 * 86400), "emergency": (2 * 3600, 12 * 3600)}
    for i in range(len(chg["sys_id"])):
        typ, state, code, desc = chg["type"][i], chg["state"][i], chg["close_code"][i], chg["short_description"][i]
        start, finish = _sec(chg["start_date"][i]), _sec(chg["end_date"][i])
        lead = int(rng.uniform(*lead_s[typ]))
        created = start - np.timedelta64(lead, "s")
        if created < window or finish + np.timedelta64(2, "h") > end:
            continue
        group = group_name[chg["assignment_group"][i]]
        who = person(group)
        fr = np.sort(rng.uniform(0.05, 0.95, 3))
        at = [created + np.timedelta64(int(lead * f), "s") for f in fr]
        steps = [("Created", created, group, who), ("Assessed", at[0], group, who)]
        if state == 4:  # cancelled: after Scheduled for most database upgrades, at assessment otherwise
            p = CANCEL_AFTER_SCHEDULED_P.get(desc, CANCEL_AFTER_SCHEDULED_P_OTHER)
            if rng.random() < p:
                steps += [("Authorized", at[1], group, who), ("Scheduled", at[2], group, who),
                          ("Cancelled", _midpoint(at[2], start), group, who)]
            else:
                steps.append(("Cancelled", _midpoint(at[0], at[1]), group, who))
            log.case("change_request", chg["sys_id"][i], chg["number"][i], steps, chg_states)
            continue
        if rng.random() >= SKIP_AUTH_P[typ]:
            steps.append(("Authorized", at[1], group, who))
        steps += [("Scheduled", at[2], group, who), ("Implementation started", start, group, who),
                  ("Failed" if code == "unsuccessful" else "Implemented", finish, group, who)]
        if state == 3:
            steps += [("Reviewed", finish + np.timedelta64(int(rng.uniform(600, 3000)), "s"), group, who),
                      ("Closed", finish + np.timedelta64(2, "h"), group, who)]
        log.case("change_request", chg["sys_id"][i], chg["number"][i], steps, chg_states)

    # --- catalog tasks -----------------------------------------------------------------------
    n = N_CATALOG_TASKS
    period_s = int((PERIOD_END - PERIOD_START).total_seconds())
    items = rng.choice(len(CATALOG_ITEMS), n, p=[w for *_, w in CATALOG_ITEMS])
    opened_s = np.sort(rng.integers(0, period_s - 3600, n))
    sc = {e: [] for e, *_ in DICTIONARY["sc_task"]}
    sc_states = {"Created": "Open", "Work started": "Work in Progress", "On hold": "Pending", "Resumed": "Work in Progress",
                 "Cancelled": "Closed Skipped"}
    group_ids = {v: k for k, v in group_name.items()}
    for i in range(n):
        item, owner, _ = CATALOG_ITEMS[int(items[i])]
        o = _sec(PERIOD_START) + np.timedelta64(int(opened_s[i]), "s")
        k = int(rng.poisson(0.35))
        chain = _chain(rng, owner, k, None)
        hold = rng.random() < CATALOG_HOLD_P.get(owner, CATALOG_HOLD_P_OTHER)
        cancelled = rng.random() < CATALOG_CANCEL_P
        before_work = rng.random() < CATALOG_CANCEL_BEFORE_WORK_P
        work_h = CATALOG_WORK_MEDIAN_H * float(np.exp(rng.normal(0, 0.5)))
        hold_h = CATALOG_HOLD_MEDIAN_H.get(owner, CATALOG_HOLD_MEDIAN_H_OTHER) * float(np.exp(rng.normal(0, 0.4)))
        mid: list[tuple[str, float]] = [("Assigned", 0.2)] + [("Reassigned", 1.5)] * k
        if cancelled:
            mid += ([] if before_work else [("Work started", 2.0)]) + [("Cancelled", 3.0)]
            total_h = float(rng.uniform(2, 48))
        else:
            mid.append(("Work started", 2.0))
            if hold:
                mid += [("On hold", 1.0), ("Resumed", hold_h / max(work_h, 1e-3) * 4.0)]
            mid.append(("Closed", 4.0))
            total_h = work_h + (hold_h if hold else 0.0)
        stop = o + np.timedelta64(int(total_h * 3600), "s")
        times = _spread(rng, o, stop, [w for _, w in mid])
        steps: list[tuple[str, np.datetime64, str | None, str | None]] = [("Created", o, None, None)]
        holders: list[str | None] = [None]  # the assignee's sys_user sys_id after each step
        g_i, who_id = -1, None
        for (activity, _), t in zip(mid, times, strict=True):
            if activity in ("Assigned", "Reassigned"):
                g_i += 1
                who_id = pool[chain[g_i]][int(rng.integers(0, AGENTS_PER_GROUP))][0]
            steps.append((activity, t, chain[max(g_i, 0)], AGENT_NAMES[who_id] if who_id else None))
            holders.append(who_id)
        n_visible = sum(1 for s in steps if s[1] <= end)
        visible = steps[:n_visible]
        last = visible[-1][0]
        if last == "Closed":
            state = 4 if rng.random() < 0.02 else 3
        elif last == "Cancelled":
            state = 7
        else:
            state = {"On hold": -5, "Work started": 2, "Resumed": 2}.get(last, 1)
        number = f"SCTASK{10001 + i:07d}"
        sys_id = _sys_id(seed, "sctask", i)
        sc["sys_id"].append(sys_id)
        sc["number"].append(number)
        sc["request_item"].append(f"RITM{20001 + i:07d}")
        sc["short_description"].append(item)
        sc["state"].append(state)
        sc["priority"].append(int(rng.choice([3, 4], p=[0.3, 0.7])))
        sc["assignment_group"].append(group_ids[visible[-1][2]] if visible[-1][2] else None)
        sc["assigned_to"].append(holders[n_visible - 1])
        sc["opened_at"].append(o)
        sc["closed_at"].append(visible[-1][1] if state in (3, 4, 7) else None)
        sc["reassignment_count"].append(sum(1 for s in visible if s[0] == "Reassigned"))
        sc["sys_updated_on"].append(visible[-1][1])
        if o >= window:
            log.case("sc_task", sys_id, number, visible,
                     {**sc_states, "Closed": SC_TASK_STATES[state] if state in (3, 4) else "Closed Complete"})

    def ts(values: list[Any]) -> pa.Array:
        return pa.array([None if v is None else np.datetime64(v, "us").astype(datetime) for v in values], pa.timestamp("us"))

    sc_table = pa.table({e: ts(sc[e]) if t == "glide_date_time" else pa.array(sc[e], _ARROW_TYPES[t])
                         for e, t, *_ in DICTIONARY["sc_task"]}, schema=arrow_schema("sc_task"))
    return {"sc_task": sc_table, "u_task_activity": log.table()}
