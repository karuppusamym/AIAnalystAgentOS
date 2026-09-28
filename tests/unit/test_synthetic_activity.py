"""Synthetic ServiceNow activity data (sc_task + the u_task_activity log): consistent with the parent records,
planted process patterns (GROUND_TRUTH), served by the mock and discovered by the connector. The four original
tables are unchanged byte for byte (tests and dated evidence depend on them)."""
from __future__ import annotations

import hashlib
import time
import warnings
from datetime import timedelta

import polars as pl
import pyarrow as pa
import pytest

from analystos.connectors.synthetic_servicenow import (
    ACTIVITY_START,
    GROUND_TRUTH,
    PERIOD_END,
    REFERENCE_PATHS,
    generate_activity_data,
    generate_servicenow_data,
)

warnings.filterwarnings("ignore", message=".*starlette.testclient.*")
# sha256 of each base table's Arrow IPC stream, taken before the activity data existed.
BASE_TABLE_SHA = {
    "incident": "994c68e177d5d99e",
    "change_request": "fa9fd3dce1aae921",
    "sys_user_group": "4d0dca0bfdf466e9",
    "cmdb_ci": "027cdbe1393749bc",
}


def _sha(t: pa.Table) -> str:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, t.schema) as w:
        w.write_table(t)
    return hashlib.sha256(sink.getvalue().to_pybytes()).hexdigest()[:16]


@pytest.fixture(scope="module")
def data() -> dict[str, pl.DataFrame]:
    base = {k: pl.from_arrow(v) for k, v in generate_servicenow_data().items()}
    act = {k: pl.from_arrow(v) for k, v in generate_activity_data().items()}
    return {**base, **act}


@pytest.fixture(scope="module")
def ev(data) -> pl.DataFrame:
    return data["u_task_activity"]


def test_generation_is_fast_deterministic_and_bounded():
    generate_servicenow_data()
    generate_activity_data.cache_clear()
    started = time.perf_counter()
    a = generate_activity_data()
    assert time.perf_counter() - started < 10.0
    b = generate_activity_data.__wrapped__()
    assert a["u_task_activity"].equals(b["u_task_activity"]) and a["sc_task"].equals(b["sc_task"])
    assert a["sc_task"].num_rows == 6_000
    assert 60_000 < a["u_task_activity"].num_rows <= 150_000


def test_the_original_tables_are_unchanged():
    base = generate_servicenow_data.__wrapped__()
    assert set(base) == {"incident", "change_request", "sys_user_group", "cmdb_ci"}
    assert {k: _sha(v) for k, v in base.items()} == BASE_TABLE_SHA


def test_every_case_is_an_ordered_history_starting_with_created(ev):
    g = ev.group_by("task_sys_id").agg(pl.col("sequence"), pl.col("activity_at"), pl.col("activity").first().alias("first"),
                                       pl.col("task_type").n_unique().alias("types"))
    assert (g["first"] == "Created").all() and (g["types"] == 1).all()
    for seq, times in zip(g["sequence"].to_list()[:3000], g["activity_at"].to_list()[:3000], strict=True):
        assert seq == list(range(1, len(seq) + 1))
        assert all(x < y for x, y in zip(times, times[1:], strict=False))
    assert ev["activity_at"].min() >= ACTIVITY_START and ev["activity_at"].max() <= PERIOD_END
    assert set(ev["task_type"].unique()) == set(REFERENCE_PATHS)


def test_incident_histories_match_the_incident_records(data, ev):
    inc = data["incident"].join(data["sys_user_group"].select(pl.col("sys_id").alias("assignment_group"),
                                                              pl.col("name").alias("group_name")), on="assignment_group", how="left")
    e = ev.filter(pl.col("task_type") == "incident")
    per = e.group_by("task_sys_id").agg(
        created=pl.col("activity_at").filter(pl.col("activity") == "Created").first(),
        last_resolved=pl.col("activity_at").filter(pl.col("activity") == "Resolved").last(),
        closed=pl.col("activity_at").filter(pl.col("activity") == "Closed").first(),
        reassigned=(pl.col("activity") == "Reassigned").sum(),
        reopened=(pl.col("activity") == "Reopened").sum(),
        last_group=pl.col("assignment_group").last(),
        last_state=pl.col("state_after").last(),
    ).join(inc, left_on="task_sys_id", right_on="sys_id")
    assert per.height == e["task_sys_id"].n_unique()
    assert (per["created"] == per["opened_at"]).all()
    resolved = per.filter(pl.col("resolved_at").is_not_null())
    assert (resolved["last_resolved"] == resolved["resolved_at"]).all()
    closed = per.filter(pl.col("closed_at").is_not_null())
    assert (closed["closed"] == closed["closed_at"]).all() and (closed["last_state"] == "Closed").all()
    assert per.filter(pl.col("closed_at").is_null())["closed"].null_count() == per.filter(pl.col("closed_at").is_null()).height
    assert (per["reassigned"] == per["reassignment_count"]).all()
    assert (per["reopened"] == per["reopen_count"]).all()
    assert (per["last_group"] == per["group_name"]).all()
    # the export covers the records opened in the window whose record fields are not DATA_QUALITY defects
    covered = inc.filter((pl.col("opened_at") >= ACTIVITY_START) & (pl.col("opened_at") <= PERIOD_END)
                         & pl.col("group_name").is_not_null()
                         & (pl.col("resolved_at").is_null() | (pl.col("resolved_at") >= pl.col("opened_at"))))
    assert covered.height == per.height


def test_change_histories_match_the_change_records(data, ev):
    e = ev.filter(pl.col("task_type") == "change_request")
    per = e.group_by("task_sys_id").agg(
        last=pl.col("activity").last(), last_at=pl.col("activity_at").last(),
        started=pl.col("activity_at").filter(pl.col("activity") == "Implementation started").first(),
    ).join(data["change_request"], left_on="task_sys_id", right_on="sys_id")
    assert (per.filter(pl.col("state") == 4)["last"] == "Cancelled").all()
    closed = per.filter(pl.col("state") == 3)
    assert (closed["last"] == "Closed").all()
    assert (closed["last_at"] == closed["end_date"] + timedelta(hours=2)).all()
    assert (per.filter(pl.col("state") != 4)["started"] == per.filter(pl.col("state") != 4)["start_date"]).all()
    assert (per.filter(pl.col("state") == 0)["last"].is_in(["Implemented", "Failed"])).all()


def test_catalog_task_histories_match_the_catalog_tasks(data, ev):
    sc = data["sc_task"]
    e = ev.filter(pl.col("task_type") == "sc_task")
    per = e.group_by("task_sys_id").agg(
        first_at=pl.col("activity_at").first(), last=pl.col("activity").last(), last_at=pl.col("activity_at").last(),
        reassigned=(pl.col("activity") == "Reassigned").sum(), number=pl.col("task_number").first(),
    ).join(sc, left_on="task_sys_id", right_on="sys_id")
    assert per.height == sc.filter(pl.col("opened_at") >= ACTIVITY_START).height
    assert (per["first_at"] == per["opened_at"]).all() and (per["number"] == per["number_right"]).all()
    done = per.filter(pl.col("state").is_in([3, 4, 7]))
    assert (done["last_at"] == done["closed_at"]).all()
    assert (per.filter(pl.col("state") == 7)["last"] == "Cancelled").all()
    assert (per.filter(pl.col("state").is_in([3, 4]))["last"] == "Closed").all()
    assert (per["reassigned"] == per["reassignment_count"]).all()
    assert sc["number"].n_unique() == sc.height


# ---------------------------------------------------------------------------------------- ground truth
def test_ground_truth_network_operations_reassignment_loops(ev):
    e = ev.filter(pl.col("task_type") == "incident").sort("task_sys_id", "sequence")
    e = e.with_columns(prev=pl.col("assignment_group").shift(1).over("task_sys_id"))
    hand = e.filter(pl.col("prev").is_not_null() & (pl.col("prev") != pl.col("assignment_group")))
    pairs = hand.with_columns(pair=pl.concat_list(pl.min_horizontal("prev", "assignment_group"),
                                                  pl.max_horizontal("prev", "assignment_group")).list.join(" / "))
    top = pairs.group_by("pair").len().sort("len", descending=True)["pair"][0]
    gt = GROUND_TRUTH["activity_reassignment_loops"]["expected"]
    assert gt["top_pair_contains"] in top
    per = e.group_by("task_sys_id").agg(r=(pl.col("activity") == "Reassigned").sum(), g=pl.col("assignment_group").last())
    assert per.group_by("g").agg(pl.col("r").mean()).sort("r", descending=True)["g"][0] == gt["top_mean_reassigned_group"]


def test_ground_truth_change_authorization_and_cancel_point(data, ev):
    e = ev.filter(pl.col("task_type") == "change_request")
    per = e.group_by("task_sys_id").agg(
        acts=pl.col("activity"), last=pl.col("activity").last(),
        before_cancel=pl.col("activity").shift(1).filter(pl.col("activity") == "Cancelled").first(),
    ).join(data["change_request"], left_on="task_sys_id", right_on="sys_id")
    closed = per.filter(pl.col("last") == "Closed").with_columns(skipped=~pl.col("acts").list.contains("Authorized"),
                                                                 emergency=pl.col("type") == "emergency")
    share = dict(closed.group_by("emergency").agg(pl.col("skipped").mean()).iter_rows())
    gt = GROUND_TRUTH["change_authorization_skipped"]
    assert abs(share[True] - gt["expected"]["emergency"]) <= gt["tolerance"]["emergency"]
    assert abs(share[False] - gt["expected"]["other"]) <= gt["tolerance"]["other"]
    cancelled = per.filter(pl.col("last") == "Cancelled").with_columns(
        late=pl.col("before_cancel") == "Scheduled", db=pl.col("short_description") == "Database upgrade")
    late = dict(cancelled.group_by("db").agg(pl.col("late").mean()).iter_rows())
    gt = GROUND_TRUTH["change_cancel_point"]
    assert abs(late[True] - gt["expected"]["database_upgrade"]) <= gt["tolerance"]["database_upgrade"]
    assert abs(late[False] - gt["expected"]["other"]) <= gt["tolerance"]["other"]


def test_ground_truth_catalog_on_hold_and_cancellations(data, ev):
    sc = data["sc_task"].join(data["sys_user_group"].select(pl.col("sys_id").alias("assignment_group"),
                                                            pl.col("name").alias("group_name")), on="assignment_group")
    held = ev.filter((pl.col("task_type") == "sc_task") & (pl.col("activity") == "On hold"))["task_sys_id"].unique()
    in_log = sc.filter(pl.col("opened_at") >= ACTIVITY_START).with_columns(
        held=pl.col("sys_id").is_in(held.implode()), euc=pl.col("group_name") == "End User Computing")
    completed = in_log.filter(pl.col("state") == 3)
    share = dict(completed.group_by("euc").agg(pl.col("held").mean()).iter_rows())
    gt = GROUND_TRUTH["catalog_on_hold_fulfilment"]
    assert abs(share[True] - gt["expected"]["on_hold_share_euc"]) <= gt["tolerance"]["on_hold_share_euc"]
    assert abs(share[False] - gt["expected"]["on_hold_share_other"]) <= gt["tolerance"]["on_hold_share_other"]
    hours = completed.with_columns(h=(pl.col("closed_at") - pl.col("opened_at")).dt.total_seconds() / 3600)
    med = dict(hours.group_by("euc").agg(pl.col("h").median()).iter_rows())
    assert med[True] / med[False] >= gt["expected"]["median_ratio_min"]
    gt = GROUND_TRUTH["catalog_cancelled_before_work"]
    all_sc = data["sc_task"]
    assert abs((all_sc["state"] == 7).mean() - gt["expected"]["cancelled_share"]) <= gt["tolerance"]["cancelled_share"]
    e = ev.filter(pl.col("task_type") == "sc_task")
    before = e.group_by("task_sys_id").agg(
        prev=pl.col("activity").shift(1).filter(pl.col("activity") == "Cancelled").first()).drop_nulls()
    assert abs((before["prev"] != "Work started").mean() - gt["expected"]["before_work_share"]) <= gt["tolerance"]["before_work_share"]


# ---------------------------------------------------------------------------------------- mock + connector
def test_the_mock_serves_the_activity_tables_and_the_connector_discovers_them():
    from fastapi.testclient import TestClient

    from analystos.connectors.servicenow import ServiceNowConnector
    from analystos.connectors.servicenow_mock import app

    client = TestClient(app, base_url="http://sn.test")
    r = client.get("/api/now/table/u_task_activity", params={"sysparm_limit": 2, "sysparm_query": "task_type=sc_task"},
                   auth=("admin", "admin"))
    assert r.status_code == 200 and int(r.headers["X-Total-Count"]) > 10_000
    row = r.json()["result"][0]
    assert row["task_number"].startswith("SCTASK") and row["activity"] == "Created"
    conn = ServiceNowConnector({"instance_url": "http://sn.test", "username": "admin", "page_size": 5000,
                                "tables": ["u_task_activity", "sc_task"]}, http_client=client, password="admin",
                               sleep=lambda s: None)
    assets = {a.name: a for a in conn.discover()}
    cols = {c.name: c.data_type for c in assets["u_task_activity"].columns}
    assert cols["activity_at"] == "timestamp" and cols["sequence"] == "integer" and cols["task_sys_id"] == "text"
    assert "assignment_group_name" not in cols  # the export already carries display names
    sc_cols = {c.name for c in assets["sc_task"].columns}
    assert {"assignment_group_name", "assigned_to_name", "request_item"} <= sc_cols
    batch = next(conn.extract(assets["sc_task"], max_rows=5))
    names = batch.column("assigned_to_name").to_pylist()
    assert all(n is None or not n.startswith("User ") for n in names)  # fulfillers have names, callers stay labels
