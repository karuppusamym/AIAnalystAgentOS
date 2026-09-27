"""Process mining analytics (skills/process_mining.py): exact outputs on a small log, keyset paging through a
runner, event-log detection, and the ITSM pack's declared model."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from analystos.core.errors import InvalidInput
from analystos.skills import process_mining as pm

T0 = datetime(2026, 1, 5, 9, 0)
REF = ["Start", "Review", "Approve", "End"]


def _rows() -> list[tuple]:
    def case(cid: str, steps: list[tuple[str, float, str | None]]) -> list[tuple]:
        return [(cid, a, T0 + timedelta(hours=h), r) for a, h, r in steps]

    return [
        *case("A", [("Start", 0, "T1"), ("Review", 1, "T1"), ("Approve", 3, "T2"), ("End", 4, "T2")]),
        *case("B", [("Start", 0, "T1"), ("Review", 2, "T1"), ("Approve", 4, "T2"), ("End", 8, "T2")]),
        *case("C", [("Start", 0, "T1"), ("Review", 1, "T1"), ("Review", 2, "T2"), ("Approve", 6, "T1"), ("End", 7, "T1")]),
        *case("D", [("Start", 0, "T1"), ("Cancel", 5, "T1")]),
        # out of order; stored in reverse to show rows are ordered by time per case
        *reversed(case("E", [("Start", 0, None), ("Approve", 1, None), ("Review", 2, None), ("End", 3, None)])),
    ]


@pytest.fixture()
def cases():
    return pm.cases_from_rows(_rows())


def test_cases_are_grouped_and_ordered(cases):
    assert sorted(cases) == ["A", "B", "C", "D", "E"]
    assert [e.activity for e in cases["E"]] == ["Start", "Approve", "Review", "End"]
    assert pm.cases_from_rows([("x", None, T0), (None, "a", T0), ("y", "a", "not a time")]) == {}


def test_directly_follows_graph(cases):
    edges = pm.directly_follows(cases)
    assert [(e["source"], e["target"], e["count"]) for e in edges] == [
        ("Approve", "End", 3), ("Review", "Approve", 3), ("Start", "Review", 3),
        ("Approve", "Review", 1), ("Review", "End", 1), ("Review", "Review", 1), ("Start", "Approve", 1), ("Start", "Cancel", 1)]
    by = {(e["source"], e["target"]): e for e in edges}
    assert by[("Start", "Review")] | {} == {"source": "Start", "target": "Review", "count": 3, "cases": 3, "median_hours": 1.0,
                                           "p90_hours": 1.8, "mean_hours": 1.33}
    assert by[("Review", "Approve")]["median_hours"] == 2.0 and by[("Review", "Approve")]["p90_hours"] == 3.6
    assert by[("Approve", "End")]["p90_hours"] == 3.4


def test_variants_and_throughput(cases):
    v = pm.variants(cases, top=3, happy_path=REF)
    assert v["total"] == 4 and v["other_cases"] == 1 and v["happy_path_rank"] == 1
    assert [(x["activities"], x["cases"], x["share"], x["median_hours"], x["happy_path"]) for x in v["top"]] == [
        (REF, 2, 0.4, 6.0, True),
        (["Start", "Approve", "Review", "End"], 1, 0.2, 3.0, False),
        (["Start", "Cancel"], 1, 0.2, 5.0, False)]
    tp = pm.throughput(cases)
    assert (tp["median_hours"], tp["p90_hours"], tp["min_hours"], tp["max_hours"]) == (5.0, 7.6, 3.0, 8.0)
    assert [b["cases"] for b in tp["histogram"]] == [0, 1, 3, 1, 0, 0, 0, 0, 0]
    assert tp["by_end_activity"][0] == {"activity": "End", "cases": 4, "median_hours": 5.5, "p90_hours": 7.7}


def test_bottlenecks_weigh_the_median_wait_by_volume(cases):
    bn = pm.bottlenecks(pm.directly_follows(cases), len(cases))
    assert [(b["source"], b["target"], b["weight_hours"]) for b in bn] == [
        ("Review", "Approve", 6.0), ("Start", "Cancel", 5.0), ("Approve", "End", 3.0), ("Start", "Review", 3.0),
        ("Approve", "Review", 1.0)]
    assert bn[0]["sentence"] == "Review → Approve takes a median 2.0 h (1 in 10 over 3.6 h) for 3 cases."
    assert pm.bottlenecks(pm.directly_follows(cases), len(cases), min_cases=2)[0]["source"] == "Review"
    assert [b["target"] for b in pm.bottlenecks(pm.directly_follows(cases), len(cases), min_cases=3)] == ["Approve", "End", "Review"]


def test_rework_cancellations_and_handovers(cases):
    assert pm.rework(cases) == {"cases": 1, "share": 0.2,
                                "activities": [{"activity": "Review", "cases": 1, "share": 0.2, "extra_events": 1}]}
    assert pm.cancel_activities(["Cancelled", "Closed", "Abandoned cart", "Rejected", "Resolved"]) == \
        ["Abandoned cart", "Cancelled", "Rejected"]
    cn = pm.cancellations(cases, ["Cancel"])
    assert (cn["cases"], cn["share"], cn["median_hours_to_cancel"]) == (1, 0.2, 5.0)
    assert cn["after"] == [{"activity": "Start", "cases": 1, "share": 1.0}] and cn["by_resource"] == [{"resource": "T1", "cases": 1}]
    ho = pm.handovers(cases)
    assert (ho["cases_with_handover"], ho["share"]) == (3, 0.6)
    assert ho["pairs"] == [{"source": "T1", "target": "T2", "count": 3, "cases": 3},
                           {"source": "T2", "target": "T1", "count": 1, "cases": 1}]
    assert ho["ping_pong"] == [{"a": "T1", "b": "T2", "cases": 1}]
    assert ho["resources"][0] == {"resource": "T1", "events": 10, "cases": 4, "handovers_out": 3, "handovers_in": 1}


def test_conformance_against_a_reference_path(cases):
    assert pm.trace_deviations(["Start", "Approve", "Review", "End"], REF) == \
        {"missing": [], "extra": [], "out_of_order": ["Approve"]}
    assert pm.trace_deviations(["Start", "Review", "Escalate", "End"], REF) == \
        {"missing": ["Approve"], "extra": ["Escalate"], "out_of_order": []}
    cf = pm.conformance(cases, REF, source="declared", cancel=["Cancel"])
    assert (cf["completed_cases"], cf["conforming_cases"], cf["fitness"]) == (4, 3, 0.75)  # a repeated step is rework, not a deviation
    assert cf["excluded"] == {"open": 0, "cancelled": 1}
    assert cf["deviations"] == [{"kind": "out_of_order", "activity": "Approve", "cases": 1, "share": 0.25,
                                 "sentence": "'Approve' out of order in 1 completed cases (25%)."}]


def test_analysis_infers_the_happy_path_and_writes_plain_highlights(cases):
    a = pm.analyze_cases(cases)
    assert a["version"] == pm.VERSION
    assert a["conformance"]["reference"] == REF and a["conformance"]["source"].startswith("inferred")
    assert a["cancellations"]["activities"] == ["Cancel"]
    assert a["summary"] | {} == {**a["summary"], "cases": 5, "events": 19, "activities": 5, "variants": 4,
                                 "mean_events_per_case": 3.8, "median_hours": 5.0, "fitness": 0.75}
    assert a["highlights"][0] == "5 cases and 19 events follow 4 different paths; the most common path covers 40% of cases."
    assert "Biggest wait: Review → Approve" in a["highlights"][2]
    assert any(h.startswith("1 cases (20%) were cancelled, most often after 'Start'") for h in a["highlights"])
    with pytest.raises(InvalidInput):
        pm.analyze_cases({})


def test_fmt_hours():
    assert [pm.fmt_hours(x) for x in (0.25, 6.24, 47.9, 72, None)] == ["15 min", "6.2 h", "47.9 h", "3.0 days", "n/a"]


# ------------------------------------------------------------------------------------------ reading
class DuckRunner:
    """A stand-in for the gateway runner: executes on DuckDB and applies the row cap like the gateway."""

    dialect = "duckdb"

    def __init__(self, rows: list[tuple]):
        import duckdb

        self.con = duckdb.connect()
        self.con.execute("CREATE SCHEMA s")
        self.con.execute("CREATE TABLE s.log (case_id VARCHAR, activity VARCHAR, event_time TIMESTAMP, team VARCHAR, kind VARCHAR)")
        self.con.executemany("INSERT INTO s.log VALUES (?, ?, ?, ?, ?)", rows)
        self.sql: list[str] = []

    def __call__(self, sql: str, *, purpose: str, max_rows: int):
        self.sql.append(sql)
        rows = self.con.execute(sql).fetchall()
        return SimpleNamespace(query_id=f"q{len(self.sql)}", rows=rows[:max_rows], truncated=len(rows) > max_rows)


MAP = pm.Mapping(case="case_id", activity="activity", timestamp="event_time", resource="team")


SIZES = {"a": 2, "b": 3, "c": 1, "d": 5, "e": 2}


def _log() -> list[tuple]:
    out = []
    for cid, n in SIZES.items():
        out += [(cid, f"step{i}", T0 + timedelta(minutes=i), "T", "x" if cid != "e" else "y") for i in range(n)]
    return out


@pytest.mark.parametrize("page_rows", [1, 2, 3, 4, 5, 13, 100])
def test_keyset_pages_never_split_or_lose_a_case(page_rows):
    runner = DuckRunner(_log())
    read = pm.read_event_log(runner, "s.log", MAP, page_rows=page_rows)
    got = pm.cases_from_rows(read["rows"])
    want = pm.cases_from_rows(_log())
    # every case is read; only a case longer than a whole page is cut (to its first page) and reported
    assert got == {c: evs[:page_rows] for c, evs in want.items()}
    split = any(n > page_rows for n in SIZES.values())
    assert read["coverage"]["split_case"] is split and read["coverage"]["truncated"] is split
    assert read["queries"] == [f"q{i + 1}" for i in range(len(runner.sql))]
    assert read["sql"] == runner.sql[0]


def test_filters_and_the_event_bound():
    runner = DuckRunner(_log())
    read = pm.read_event_log(runner, "s.log", MAP, filters=[{"column": "kind", "op": "=", "value": "y"}])
    assert {r[0] for r in read["rows"]} == {"e"}
    read = pm.read_event_log(runner, "s.log", MAP, filters=[{"column": "case_id", "op": "not_in", "values": ["a", "b"]}])
    assert {r[0] for r in read["rows"]} == {"c", "d", "e"}
    bounded = pm.read_event_log(DuckRunner(_log()), "s.log", MAP, page_rows=4, max_events=6)
    assert bounded["coverage"]["truncated"] and {r[0] for r in bounded["rows"]} == {"a", "b", "c"}
    with pytest.raises(InvalidInput):
        pm.event_log_sql("s.log", MAP, filters=[{"column": "kind", "op": "like", "value": "%"}])


def test_the_sql_is_one_plain_ordered_select():
    sql = pm.event_log_sql("s.log", MAP, dialect="postgres", after_case="O'Brien", op=">")
    assert sql.startswith('SELECT "case_id", "activity", "event_time", "team" FROM "s"."log" WHERE')
    assert "'O''Brien'" in sql and sql.endswith('ORDER BY "case_id", "event_time", "activity"')
    assert "OVER" not in sql.upper()


# ------------------------------------------------------------------------------------------ detection
def _col(name: str, dtype: str = "text", **kw) -> dict:
    return {"name": name, "data_type": dtype, **kw}


def test_an_event_log_is_detected_with_a_mapping():
    asset = {"name": "order_events", "role": "event", "row_count": 1000, "columns": [
        _col("event_id", is_key=True, profile={"distinct": 1000}), _col("order_id", profile={"distinct": 200}),
        _col("event_type", profile={"distinct": 7, "top_values": [{"value": "created", "count": 200}]}),
        _col("occurred_at", "timestamp"), _col("loaded_at", "timestamp"), _col("handled_by_team"),
        _col("agent_name", tags=["pii"]), _col("channel", profile={"distinct": 3, "top_values": [{"value": "web", "count": 600}]}),
        _col("amount", "numeric")]}
    found = pm.detect_event_log(asset)
    assert found["mapping"] == {"case_column": "order_id", "activity_column": "event_type",
                                "timestamp_column": "occurred_at", "resource_column": "handled_by_team"}
    assert found["segments"] == [{"column": "channel", "values": [{"value": "web", "count": 600}]}]
    assert "the crawler classified the table as an event table" in found["reasons"]


def test_a_plain_entity_table_is_not_an_event_log():
    assert pm.detect_event_log({"name": "customers", "columns": [
        _col("customer_id", is_key=True), _col("name"), _col("created_at", "timestamp")]}) is None
    # an event log needs a case column that repeats: the table's own key does not
    assert pm.detect_event_log({"name": "t", "row_count": 10, "columns": [
        _col("id", is_key=True, profile={"distinct": 10}), _col("status"), _col("changed_at", "timestamp")]}) is None


def test_the_itsm_pack_declares_the_task_activity_log():
    declared = pm.declared_event_logs()
    model = pm.declared_for(declared, ["u_task_activity"])
    assert model["pack"].startswith("pack.itsm@")
    assert model["segment_column"] == "task_type"
    assert set(model["segments"]) == {"incident", "change_request", "sc_task"}
    assert pm.declared_for(declared, ["something_else"]) is None
