"""P7-04 / P7-05 on the in-memory control plane: step versions, edit-and-re-run with downstream voiding
(old versions readable), self-check corrections and flags, the sweep agreeing with the events, pins that
replay the frozen query, and branches (fork, compare, merge with both branches' lineage)."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from analystos.contracts.step import ForkIn, MergeIn, NotebookIn, PinIn, StepEdit, StepIn
from analystos.core.errors import InvalidInput, PolicyDenied, PreconditionFailed
from analystos.db.base import session_scope
from analystos.db.models import AnalysisStep, Artifact, LineageEdge, Relationship, VerificationRecord
from analystos.evidence import verification
from analystos.services import branches, notebooks, step_pins
from analystos.services import steps as steps_svc
from tests.unit.step_fixtures import WS, FakeRuntime, world  # noqa: F401

ORDERS = ("FROM sales.orders", ["state", "n"], [["Closed", 60], ["Open", 40]])


@pytest.fixture
def thread(world):  # noqa: F811
    with session_scope() as s:
        nb = notebooks.create(s, s.merge(world["analyst"]), WS, NotebookIn(title="Late orders"))
    return {"branch": nb["branch_id"], "notebook": nb["id"], **world}


def _query(t, rt, sql="SELECT state, COUNT(*) AS n FROM sales.orders GROUP BY state", title="orders by state", **kw):
    return steps_svc.create(t["analyst"], WS, t["branch"], StepIn(kind="query", title=title, spec={"sql": sql}, **kw), runtime=rt)


def _record(record_id):
    with session_scope() as s:
        r = s.get(VerificationRecord, record_id)
        s.expunge(r)
    return r


def test_a_query_step_runs_through_the_runtime_and_records_a_verdict(thread):
    rt = FakeRuntime([ORDERS])
    step = _query(thread, rt)
    assert step["version"] == 1 and step["status"] == "ok" and step["kind"] == "query"
    assert step["result_snapshot"]["kind"] == "artifact" and step["receipts"][0]["query_id"].startswith("qry")
    assert {c["check"] for c in step["checks"]} >= {"empty_result", "magnitude", "truncation", "grouping", "fanout", "dq_gate"}
    assert step["verification_record"]["state"] == "ACTIVE" and step["verification_record"]["verdict"] == "verified"
    deps = {(d["kind"], d["ref"]) for d in step["verification_record"]["dependencies"]}
    assert ("query", f"step:{step['id']}") in deps and ("policy", WS) in deps
    with session_scope() as s:
        full = steps_svc.with_result(s, s.get(AnalysisStep, step["id"]))
    assert full["result"]["rows"] == [["Closed", 60], ["Open", 40]]


def test_editing_a_step_voids_dependents_reruns_them_and_keeps_old_versions(thread):
    rt = FakeRuntime([ORDERS, ("FROM sales.order_line", ["n"], [[400]])])
    a = _query(thread, rt)
    b = steps_svc.create(thread["analyst"], WS, thread["branch"],
                         StepIn(kind="method", title="total", spec={"cell": "python", "code": "result = 1"},
                                depends_on=[a["id"]]), runtime=rt)
    c = steps_svc.create(thread["analyst"], WS, thread["branch"],
                         StepIn(kind="claim", title="claim", spec={"text": "There are 100 orders."}, depends_on=[b["id"]]),
                         runtime=rt)
    assert b["status"] == "ok" and c["status"] == "ok"
    old = {x["id"]: x["verification_record"]["record_id"] for x in (a, b, c)}

    out = steps_svc.edit(thread["analyst"], a["id"], StepEdit(spec={"sql": "SELECT COUNT(*) AS n FROM sales.order_line"}),
                         expected_version=1, runtime=rt)
    assert out["step"]["version"] == 2 and [r["id"] for r in out["rerun"]] == [b["id"], c["id"]]
    assert set(old.values()) <= set(out["voided_records"])
    for sid, rid in old.items():
        rec = _record(rid)
        assert rec.state == "VOID" and rec.void_kind == "query", (sid, rec.state)
    assert "edited" in _record(old[a["id"]]).void_reason
    # dependents re-ran on the new result: the claim's "100" no longer binds (400 now) -> flagged, not hidden
    claim = out["rerun"][1]
    assert claim["version"] == 2 and claim["status"] == "flagged"
    assert claim["verification_record"]["verdict"] == "failed_verification"
    with session_scope() as s:
        history = steps_svc.versions(s, s.get(AnalysisStep, a["id"]))
    assert [v["version"] for v in history] == [2, 1]
    assert history[1]["spec"]["sql"].startswith("SELECT state") and history[1]["verification_record"]["state"] == "VOID"
    with pytest.raises(PreconditionFailed):
        steps_svc.edit(thread["analyst"], a["id"], StepEdit(title="stale"), expected_version=1, runtime=rt)


def test_the_nightly_sweep_finds_nothing_the_step_events_missed(thread):
    rt = FakeRuntime([ORDERS])
    a = _query(thread, rt)
    steps_svc.rerun(thread["analyst"], a["id"], runtime=rt)
    with session_scope() as s:
        result = verification.sweep(s, actor="test")
    assert result["late_voids"] == 0 and result["checked"] >= 1
    # a missed event (a version written without `new_version`) is caught by the sweep as a late void
    with session_scope() as s:
        step = s.get(AnalysisStep, a["id"])
        step.current_version = 1
    with session_scope() as s:
        assert verification.sweep(s, actor="test")["late_voids"] == 1


def test_a_filter_value_in_the_wrong_case_is_corrected_and_rerun(thread):
    rt = FakeRuntime([("'Closed'", ["n"], [[60]]), ("'closed'", ["n"], [])])
    step = _query(thread, rt, sql="SELECT COUNT(*) AS n FROM sales.orders AS orders WHERE orders.state = 'closed'")
    assert step["status"] == "ok" and len(rt.ran) == 2
    fix = step["corrections"][0]
    assert fix["check"] == "empty_result" and "'Closed'" in fix["to_sql"]
    check = next(c for c in step["checks"] if c["check"] == "empty_result")
    assert check["passed"] and check["corrected"]
    assert [r["role"] for r in step["receipts"]] == ["superseded_by_correction", "corrected"]


def test_an_uncorrectable_check_flags_the_step(thread):
    with session_scope() as s:
        s.add(Relationship(id="rel_1", workspace_id=WS, from_asset_id="ast_lines", from_column="order_id",
                           to_asset_id="ast_orders", to_column="id", cardinality="many_to_one", confidence=1.0,
                           validated=True, evidence={}, origin="user"))
    rt = FakeRuntime([("SUM(o.amount)", ["total"], [[1234.5]])])
    step = _query(thread, rt, sql="SELECT SUM(o.amount) AS total FROM sales.orders o JOIN sales.order_line l ON l.order_id = o.id")
    assert step["status"] == "flagged" and not step["corrections"]
    assert not next(c for c in step["checks"] if c["check"] == "fanout")["passed"]
    assert step["verification_record"]["badge"] == "failed"


def test_a_failed_upstream_fails_its_dependents_without_running_them(thread):
    rt = FakeRuntime([ORDERS])
    a = _query(thread, rt)
    b = steps_svc.create(thread["analyst"], WS, thread["branch"],
                         StepIn(kind="method", title="py", spec={"cell": "python", "code": "result = 1"}, depends_on=[a["id"]]),
                         runtime=rt)
    out = steps_svc.edit(thread["analyst"], a["id"], StepEdit(spec={"sql": "SELECT * FROM sales.nowhere"}), expected_version=1,
                         runtime=rt)
    assert out["step"]["status"] == "failed" and "sql_rejected" in out["step"]["error"]
    assert out["rerun"][0]["status"] == "failed" and "upstream" in out["rerun"][0]["error"]
    assert len(rt.python_calls) == 1  # only the first run of b
    assert b["status"] == "ok"


def test_specs_are_validated_before_anything_runs(thread):
    rt = FakeRuntime([ORDERS])
    for kind, spec in (("query", {}), ("query", {"sql": "x", "semantic_query": {"metrics": ["m"]}}),
                       ("method", {"cell": "python", "code": "import socket\nresult = 1"}), ("chart", {"chart": {}}),
                       ("claim", {})):
        with pytest.raises(InvalidInput):
            steps_svc.create(thread["analyst"], WS, thread["branch"], StepIn(kind=kind, spec=spec), runtime=rt)
    assert rt.ran == []


def test_pinning_needs_an_approval_and_the_tile_replays_the_frozen_query(thread, monkeypatch):
    from datetime import datetime

    from analystos.governance import approvals
    from analystos.governance.approvals import decide

    monkeypatch.setattr(approvals, "utcnow", lambda: datetime.utcnow())  # SQLite stores expiry without its zone

    rt = FakeRuntime([ORDERS, ("FROM sales.order_line", ["n"], [[400]])])
    a = _query(thread, rt)
    owner = thread["owner"]
    with session_scope() as s:
        out = step_pins.pin(s, s.merge(owner), s.get(AnalysisStep, a["id"]), PinIn(target="tile", dashboard="Ops"))
    assert out["status"] == "approval_required"
    with session_scope() as s:
        decide(s, out["approval_id"], s.merge(thread["approver"]), approve=True)
    with session_scope() as s:
        pinned = step_pins.pin(s, s.merge(owner), s.get(AnalysisStep, a["id"]),
                               PinIn(target="tile", dashboard="Ops", approval_id=out["approval_id"]))["pin"]
        tile = s.get(Artifact, pinned["target_id"])
        assert tile.type == "chart" and tile.content["frozen"]["sql"].startswith("SELECT state")
        assert pinned["definition"]["kind"] == "step_query"
    steps_svc.edit(thread["analyst"], a["id"], StepEdit(spec={"sql": "SELECT COUNT(*) AS n FROM sales.order_line"}),
                   expected_version=1, runtime=rt)
    rt.ran.clear()
    replay = step_pins.replay(owner, WS, pinned["id"], runtime=rt)
    assert rt.ran == [pinned["frozen"]["sql"]]  # the frozen v1 query, not the edited step
    assert replay["pin_state"] == "upgrade_available" and replay["step_current_version"] == 2
    with session_scope() as s:  # a flagged or void version cannot be pinned
        with pytest.raises(PolicyDenied):
            step_pins.pin(s, s.merge(owner), s.get(AnalysisStep, a["id"]), PinIn(target="tile", version=1))


def test_fork_compare_and_merge_keep_both_branches_lineage(thread):
    rt = FakeRuntime([ORDERS, ("FROM sales.order_line", ["state", "n"], [["Closed", 70], ["Open", 30]])])
    a = _query(thread, rt)
    b = steps_svc.create(thread["analyst"], WS, thread["branch"],
                         StepIn(kind="chart", title="bar", spec={"chart": {"type": "bar", "x": "state", "y": "n"}},
                                depends_on=[a["id"]]), runtime=rt)
    assert b["status"] == "ok"
    fork = branches.fork(thread["analyst"], WS, thread["branch"],
                         ForkIn(from_step_id=a["id"], name="lines instead", include_downstream=True,
                                spec={"sql": "SELECT state, COUNT(*) AS n FROM sales.order_line GROUP BY state"}), runtime=rt)
    assert fork["branch"]["parent_branch_id"] == thread["branch"] and fork["branch"]["forked_from"]["step_id"] == a["id"]
    own = [s for s in fork["steps"] if not s["inherited"]]
    assert [s["forked_from"]["step_id"] for s in own] == [a["id"], b["id"]]
    assert fork["edited"]["step"]["version"] == 2 and fork["edited"]["rerun"][0]["kind"] == "chart"

    with session_scope() as s:
        from analystos.db.models import StepBranch

        cmp = branches.compare(s, s.get(StepBranch, thread["branch"]), s.get(StepBranch, fork["branch"]["id"]))
    pair = next(p for p in cmp["steps"] if p["root"] == a["id"])
    assert pair["match"] == "diverged" and pair["spec_diff"]
    assert {c["key"][0]: c["delta"] for c in pair["numbers"]["cells"]} == {"Closed": 10, "Open": -10}
    assert pair["verdicts"] == {"a": "verified", "b": "verified"}

    first = branches.merge(thread["analyst"], WS, thread["branch"], MergeIn(title="Late orders"))
    second = branches.merge(thread["analyst"], WS, fork["branch"]["id"], MergeIn(report_id=first["report"]["id"]))
    content = second["report"]["content"]
    assert {x["branch_id"] for x in content["branches"]} == {thread["branch"], fork["branch"]["id"]}
    assert len(content["sections"]) == 4 and second["report"]["version"] == 2
    with session_scope() as s:
        edges = {(e.from_type, e.relation, e.to_type) for e in s.scalars(select(LineageEdge).where(LineageEdge.workspace_id == WS))}
    assert {("step", "included_in", "artifact"), ("step_branch", "merged_into", "artifact"),
            ("step_branch", "forked_into", "step_branch"), ("step", "forked_into", "step")} <= edges


def test_a_void_verdict_refuses_the_merge(thread):
    rt = FakeRuntime([ORDERS, ("FROM sales.order_line", ["n"], [[400]])])
    a = _query(thread, rt)
    b = steps_svc.create(thread["analyst"], WS, thread["branch"],
                         StepIn(kind="method", title="py", spec={"cell": "python", "code": "result = 1"}, depends_on=[a["id"]]),
                         runtime=rt)
    fork = branches.fork(thread["analyst"], WS, thread["branch"], ForkIn(from_step_id=b["id"]), runtime=rt)
    # the parent's upstream changes: the fork's copy depended on it, so its verdict is void (and shown so)
    steps_svc.edit(thread["analyst"], a["id"], StepEdit(spec={"sql": "SELECT COUNT(*) AS n FROM sales.order_line"}),
                   expected_version=1, runtime=rt)
    with pytest.raises(PolicyDenied) as exc:
        branches.merge(thread["analyst"], WS, fork["branch"]["id"], MergeIn())
    assert "void" in exc.value.message
