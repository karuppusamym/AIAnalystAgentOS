"""Existing-dashboard mode (N-2, BI-011/012), service side: import maps a dashboard to the workspace's sources and
semantic metrics, re-executes each chart through the gateway (never the BI tool's own SQL path) and compares with
the numbers the BI tool shows, flags drift and mismatched definitions, and proposes changes that are only ever
applied through an approval verified immediately before the write. A fake BI source and a fake gateway; no services."""
from __future__ import annotations

import copy
import datetime as dt
from typing import Any

import pytest
from tests.unit.step_fixtures import WS, world  # noqa: F401

from analystos.contracts.bi import BIChartQuery
from analystos.core.errors import ApprovalRequired, Conflict, Forbidden, InvalidInput, NotFound, PolicyDenied
from analystos.core.ids import new_id, stable_hash
from analystos.db.base import session_scope
from analystos.db.models import Approval, AuditEvent, BiDashboardImport, LineageEdge, RunEvent, SemanticMetric, User
from analystos.gateway.types import QueryResult
from analystos.governance.approvals import decide
from analystos.publishing.superset_import import chart_query
from analystos.services import existing_dashboards as svc

JAN5 = int(dt.datetime(2026, 1, 5, tzinfo=dt.UTC).timestamp() * 1000)


def _dataset(owned: bool = True, database: str = "Sales DW") -> dict[str, Any]:
    """Owned: a virtual dataset this workspace published (`aos_<ws>_`); otherwise a physical one other teams share."""
    where = {"name": "aos_ws_steps_orders", "sql": "SELECT state, amount, ordered_at, late FROM sales.orders",
             "kind": "virtual"} if owned else {"name": "orders", "sql": None, "kind": "physical"}
    return {"id": 11, "schema": "sales", "database": database, **where, "main_dttm_col": "ordered_at",
            "columns": [{"name": "state"}, {"name": "amount"}, {"name": "ordered_at", "is_dttm": True}],
            "metrics": [{"name": "order_count", "expression": "COUNT(*)", "certified": False},
                        {"name": "revenue", "expression": "SUM(amount) * 1.2", "certified": False},
                        {"name": "avg_amount", "expression": "AVG(amount)", "certified": True},
                        {"name": "late_share", "expression": "AVG(CASE WHEN late THEN 1.0 ELSE 0 END)", "certified": False}]}


FORMS = {
    1: ("Orders by state", {"viz_type": "pie", "metric": "order_count", "groupby": ["state"], "row_limit": 25}),
    2: ("Revenue", {"viz_type": "big_number_total", "metric": "revenue"}),
    3: ("Average amount", {"viz_type": "big_number_total", "metric": "avg_amount",
                           "adhoc_filters": [{"expressionType": "SIMPLE", "clause": "WHERE", "subject": "state",
                                              "operator": "==", "comparator": "Closed"}]}),
    4: ("Weekly orders", {"viz_type": "echarts_timeseries_line", "x_axis": "ordered_at", "time_grain_sqla": "P1W",
                          "metrics": ["order_count"]}),
    5: ("Late share", {"viz_type": "big_number_total", "metric": "late_share"}),
    6: ("Amounts", {"viz_type": "histogram_v2", "column": "amount"}),
}
BI_DATA = {
    1: {"columns": ["state", "order_count"], "rows": [["Closed", 60], ["Open", 40]]},
    2: {"columns": ["revenue"], "rows": [[1200.0]]},
    3: {"columns": ["avg_amount"], "rows": [[12.0]]},  # the dashboard says 12; governed says 10
    4: {"columns": ["ordered_at", "order_count"], "rows": [[JAN5, 5]]},
    5: {"columns": ["late_share"], "rows": [[0.25]]},
}
GOVERNED = [  # (substring of the composed SQL, columns, rows)
    ('"state", COUNT(*) AS "order_count"', ["state", "order_count"], [["Closed", 60], ["Open", 40]]),
    ('AS "revenue"', ["revenue"], [[1200.0]]),
    ('AS "avg_amount"', ["avg_amount"], [[10.0]]),
    ("DATE_TRUNC", ["ordered_at", "order_count"], [["2026-01-05T00:00:00", 5]]),
    ('AS "late_share"', ["late_share"], [[0.25]]),
]


class FakeBI:
    destination = "superset"

    def __init__(self, **dataset: Any) -> None:
        self.forms = copy.deepcopy(FORMS)
        self.dataset = dataset
        self.applied: list[list[dict]] = []
        self.data_reads: list[Any] = []

    def list_dashboards(self, workspace_id: str) -> list[dict]:
        return [{"id": 42, "title": "Sales overview", "slug": "sales", "published": True, "url": "http://bi/42"}]

    def inspect_dashboard(self, dashboard_id: Any, *, workspace_id: str | None = None) -> dict:
        if str(dashboard_id) != "42":
            raise NotFound(f"dashboard {dashboard_id} not found")
        ds = _dataset(**self.dataset)
        charts = [{"id": cid, "name": name, "viz_type": fd["viz_type"], "dataset_id": "11",
                   "query": chart_query({**fd, "datasource": "11__table"}, ds).model_dump()} for cid, (name, fd) in self.forms.items()]
        return {"id": 42, "title": "Sales overview", "slug": "sales", "published": True, "url": "http://bi/42",
                "charts": charts, "datasets": [ds], "filters": [], "layout": []}

    def chart_data(self, chart_id: Any) -> dict:
        self.data_reads.append(chart_id)
        return copy.deepcopy(BI_DATA.get(int(chart_id), {"error": "no query context"}))

    def apply_changes(self, dashboard: dict, changes: list[dict]) -> list[str]:
        self.applied.append(changes)
        return [f"dataset:{c['dataset_id']}:metric:{c['metric']}" for c in changes]


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def execute(self, scope, sql, *, actor, purpose, max_rows=None, **_):  # noqa: ANN001, ANN201
        self.calls.append({"sql": sql, "actor": actor, "purpose": purpose, "scope": scope, "max_rows": max_rows})
        for needle, columns, rows in GOVERNED:
            if needle in sql:
                return QueryResult(query_id=new_id("qry"), columns=columns, rows=rows, row_count=len(rows),
                                   fingerprint=stable_hash(sql), result_hash=stable_hash(rows), referenced_assets=["sales.orders"],
                                   sql=sql)
        raise AssertionError(f"unexpected SQL {sql}")


def _approve_metric(name: str, expression: str) -> None:
    from analystos.semantic.ossie import normalize_expression

    with session_scope() as s:
        s.add(SemanticMetric(id=new_id("smet"), workspace_id=WS, name=name, version=1, status="approved",
                             definition={"name": name, "expressions": [{"expression": expression}]}, expression=expression,
                             normalized_expression=normalize_expression(expression), proposed_by="usr_owner",
                             proposed_via="user", content_hash="x"))


@pytest.fixture
def bi_world(world, monkeypatch):  # noqa: F811
    from analystos.db import models
    from analystos.governance import approvals

    # SQLite returns naive datetimes: keep the approval clock naive so expiry checks compare like with like.
    monkeypatch.setattr(approvals, "utcnow", lambda: dt.datetime.now(dt.UTC).replace(tzinfo=None))

    with session_scope() as s:
        models.Base.metadata.tables["bi_dashboard_import"].create(s.get_bind(), checkfirst=True)
        s.get(models.Source, "src_sales").config = {"superset_database": "Sales DW"}  # the BI database that is this source
    _approve_metric("order_count", "count(*)")        # same definition, not certified in the BI tool
    _approve_metric("revenue", "SUM(amount)")          # the BI tool inflates it
    _approve_metric("avg_amount", "AVG(amount)")       # matches and certified
    return world


def _import(bi: FakeBI, gw: FakeGateway, uid: str = "usr_owner") -> dict:
    with session_scope() as s:
        return svc.import_dashboard(s, s.get(User, uid), WS, "42", source=bi, gateway=gw)


def test_import_maps_verifies_flags_and_proposes(bi_world):
    bi, gw = FakeBI(), FakeGateway()
    out = _import(bi, gw)
    assert (out["version"], out["status"], out["title"]) == (1, "attention", "Sales overview")
    ds = out["mapping"]["datasets"][0]
    assert (ds["assets"], ds["source_id"], ds["mapped"]) == (["sales.orders"], "src_sales", True)
    metrics = {m["label"]: m["status"] for m in out["mapping"]["metrics"]}
    assert metrics == {"order_count": "matches", "revenue": "definition_mismatch", "avg_amount": "matches",
                       "late_share": "unmapped"}
    status = {v["chart_id"]: v["status"] for v in out["verification"]}
    assert status == {1: "verified", 2: "verified", 3: "mismatch", 4: "verified", 5: "verified", 6: "not_reproducible"}
    mismatch = next(v for v in out["verification"] if v["chart_id"] == 3)["comparison"]["mismatches"][0]
    assert (mismatch["governed"], mismatch["bi"], mismatch["delta"]) == (10.0, 12.0, -2.0)
    kinds = sorted(f["kind"] for f in out["findings"])
    assert kinds == ["definition_mismatch", "not_reproducible", "number_mismatch", "uncertified_metric", "unmapped_metric"]
    by_kind = {p["kind"]: p for p in out["proposals"]}
    assert by_kind["align_metric"]["change"] == {"type": "set_dataset_metric", "dataset_id": "11", "metric": "revenue",
                                                 "expression": "SUM(amount)", "certify": True,
                                                 "details": "Approved semantic metric revenue"}
    assert by_kind["certify_metric"]["write_back"] and by_kind["propose_semantic_metric"]["platform"]
    assert not by_kind["review_numbers"]["write_back"]
    assert all(p["status"] == "proposed" for p in out["proposals"])
    assert bi.applied == []  # nothing is applied on import


def test_every_chart_query_goes_through_the_gateway_under_the_callers_scope(bi_world):
    bi, gw = FakeBI(), FakeGateway()
    _import(bi, gw)
    assert len(gw.calls) == 5  # the histogram is not reproducible, so it is never run
    assert {c["purpose"] for c in gw.calls} == {svc.PURPOSE} and {c["actor"] for c in gw.calls} == {"user:usr_owner"}
    assert all(c["scope"].workspace_id == WS and c["scope"].user_id == "usr_owner" for c in gw.calls)
    sql = {c["sql"] for c in gw.calls}
    # the virtual dataset's own SQL is a subquery of the statement the gateway validates (tables must be in scope)
    assert any("FROM (SELECT state, amount, ordered_at, late FROM sales.orders) AS" in q and "GROUP BY" in q
               and "LIMIT 25" in q for q in sql)
    assert any("\"state\" = 'Closed'" in q for q in sql)
    assert any("DATE_TRUNC('week'" in q for q in sql)


def test_import_records_lineage_events_and_audit(bi_world):
    out = _import(FakeBI(), FakeGateway())
    with session_scope() as s:
        edges = {(e.relation, e.to_type, e.to_id) for e in s.query(LineageEdge).filter_by(from_type="bi_dashboard", from_id=out["id"])}
        assert ("reads", "table", "sales.orders") in edges
        assert {t for r, t, _ in edges if r == "uses_metric"} == {"semantic_metric"}
        assert s.query(RunEvent).filter_by(type="bi_dashboard.imported").count() == 1
        assert s.query(AuditEvent).filter_by(action="bi_dashboard.imported").count() == 1


def test_roles_and_workspace_binding(bi_world):
    for uid in ("usr_viewer", "usr_analyst"):  # importing reads the BI tool's catalog: editor, as the Superset crawl
        with pytest.raises(Forbidden):
            _import(FakeBI(), FakeGateway(), uid=uid)
    out = _import(FakeBI(), FakeGateway())
    with session_scope() as s:
        viewer, analyst = s.get(User, "usr_viewer"), s.get(User, "usr_analyst")
        assert [i["id"] for i in svc.list_imports(s, viewer, WS)] == [out["id"]]  # summaries only
        assert "verification" not in svc.list_imports(s, viewer, WS)[0]
        with pytest.raises(Forbidden):  # the detail carries values computed under the importer's scope
            svc.get_import(s, viewer, WS, out["id"])
        assert svc.get_import(s, analyst, WS, out["id"])["id"] == out["id"]
        with pytest.raises(NotFound):
            svc.get_import(s, analyst, "ws_elsewhere", out["id"])


def test_no_rows_are_kept_only_receipts_and_differences(bi_world):
    out = _import(FakeBI(), FakeGateway())
    for v in out["verification"]:
        assert "rows" not in (v.get("governed") or {}) and "rows" not in (v.get("bi") or {})
        assert isinstance((v.get("comparison") or {}).get("only_in_bi", 0), int)


def test_a_dataset_on_another_bi_database_is_not_mapped(bi_world):
    out = _import(FakeBI(database="Finance prod"), FakeGateway())
    ds = out["mapping"]["datasets"][0]
    assert not ds["mapped"] and ds["assets"] == [] and "Finance prod" in ds["reason"]
    assert {v["status"] for v in out["verification"]} == {"unmapped", "not_reproducible"}


def test_a_shared_dataset_is_never_written(bi_world):
    bi = FakeBI(owned=False)
    out = _import(bi, FakeGateway())
    assert out["mapping"]["datasets"][0]["mapped"] and not out["mapping"]["datasets"][0]["owned"]
    assert not any(p["write_back"] for p in out["proposals"])
    assert {"ask_dataset_owner"} <= {p["kind"] for p in out["proposals"]}
    with session_scope() as s, pytest.raises(InvalidInput):
        svc.request_update(s, s.get(User, "usr_owner"), WS, out["id"], [_proposal(out, "ask_dataset_owner")])


def test_under_row_security_the_bi_numbers_are_not_read(bi_world):
    from analystos.contracts.policy import DataScope

    bi = FakeBI()
    inspection = bi.inspect_dashboard(42)
    scope = DataScope(workspace_id=WS, user_id="usr_owner", role="owner", source_ids=["src_sales"], assets=["sales.orders"],
                      asset_sources={"sales.orders": "src_sales"}, source_dialects={"src_sales": "postgres"},
                      row_filters={"sales.orders": ["state = 'Open'"]})
    datasets = svc.map_datasets(inspection, scope, {"src_sales": {"Sales DW"}})
    out = svc.verify_charts(inspection, datasets, scope, gateway=FakeGateway(), source=bi, actor="user:usr_owner")
    assert {v["status"] for v in out} == {"filtered", "not_reproducible"} and bi.data_reads == []


def _proposal(out: dict, kind: str) -> str:
    return next(p["id"] for p in out["proposals"] if p["kind"] == kind)


def test_write_back_needs_an_approval_verified_before_the_write(bi_world):
    bi = FakeBI()
    out = _import(bi, FakeGateway())
    ids = [_proposal(out, "align_metric"), _proposal(out, "certify_metric")]
    with session_scope() as s, pytest.raises(Forbidden):  # an analyst cannot ask to change the BI tool
        svc.request_update(s, s.get(User, "usr_analyst"), WS, out["id"], ids)
    with session_scope() as s:
        req = svc.request_update(s, s.get(User, "usr_owner"), WS, out["id"], ids)
    assert req["status"] == "approval_required"
    with session_scope() as s, pytest.raises(ApprovalRequired):  # still pending
        svc.execute_update(s, s.get(User, "usr_owner"), WS, out["id"], req["approval_id"], source=bi)
    assert bi.applied == []
    with session_scope() as s:
        decide(s, req["approval_id"], s.get(User, "usr_approver"), approve=True)
    with session_scope() as s:
        done = svc.execute_update(s, s.get(User, "usr_owner"), WS, out["id"], req["approval_id"], source=bi)
    assert done["status"] == "applied" and len(bi.applied) == 1
    assert {c["metric"] for c in bi.applied[0]} == {"revenue", "order_count"}
    with session_scope() as s:
        assert s.get(Approval, req["approval_id"]).status == "executed"
        assert s.get(BiDashboardImport, out["id"]).applied[-1]["status"] == "applied"
        with pytest.raises(ApprovalRequired):  # single use
            svc.execute_update(s, s.get(User, "usr_owner"), WS, out["id"], req["approval_id"], source=bi)
    assert len(bi.applied) == 1


def test_advice_is_never_written_back(bi_world):
    out = _import(FakeBI(), FakeGateway())
    with session_scope() as s, pytest.raises(InvalidInput):
        svc.request_update(s, s.get(User, "usr_owner"), WS, out["id"], [_proposal(out, "review_numbers")])
    with session_scope() as s, pytest.raises(InvalidInput):
        svc.request_update(s, s.get(User, "usr_owner"), WS, out["id"], ["nope"])


def test_a_dashboard_changed_since_import_is_not_written_and_reimport_flags_drift(bi_world):
    bi = FakeBI()
    out = _import(bi, FakeGateway())
    with session_scope() as s:
        req = svc.request_update(s, s.get(User, "usr_owner"), WS, out["id"], [_proposal(out, "align_metric")])
    with session_scope() as s:
        decide(s, req["approval_id"], s.get(User, "usr_approver"), approve=True)
    bi.forms[1][1]["row_limit"] = 10  # someone edits the chart in the BI tool
    with session_scope() as s, pytest.raises(Conflict):
        svc.execute_update(s, s.get(User, "usr_owner"), WS, out["id"], req["approval_id"], source=bi)
    assert bi.applied == []
    again = _import(bi, FakeGateway())
    assert again["version"] == 2 and again["drift"] and again["previous_fingerprint"] == out["fingerprint"]
    drift = next(f for f in again["findings"] if f["kind"] == "structure_changed")
    assert drift["charts"] == ["1"] and drift["since_version"] == 1
    with session_scope() as s:
        assert s.query(RunEvent).filter_by(type="bi_dashboard.drift_detected").count() == 1
        # the approval bound to import v1 cannot be replayed against import v2
        with pytest.raises(PolicyDenied):
            svc.execute_update(s, s.get(User, "usr_owner"), WS, again["id"], req["approval_id"], source=bi)


def test_an_unmapped_metric_becomes_a_semantic_proposal_awaiting_its_own_approval(bi_world):
    out = _import(FakeBI(), FakeGateway())
    with session_scope() as s:
        res = svc.propose_metric(s, s.get(User, "usr_owner"), WS, out["id"], _proposal(out, "propose_semantic_metric"))
    assert res["status"] == "proposed" and res["metric"] == "late_share" and res["approval_id"]
    with session_scope() as s:
        row = s.query(SemanticMetric).filter_by(workspace_id=WS, name="late_share").one()
        assert row.status == "proposed" and row.proposed_via == "user"  # a person asked: user validation applies
        edge = s.query(LineageEdge).filter_by(from_type="semantic_metric", from_id=row.id, relation="derived_from").one()
        assert (edge.to_type, edge.to_id) == ("bi_dashboard", out["id"])
        with pytest.raises(Forbidden):
            svc.propose_metric(s, s.get(User, "usr_analyst"), WS, out["id"], _proposal(out, "propose_semantic_metric"))
        with pytest.raises(InvalidInput):
            svc.propose_metric(s, s.get(User, "usr_owner"), WS, out["id"], _proposal(out, "align_metric"))


def test_compare_unpivots_series_columns_and_tolerates_rounding():
    q = BIChartQuery(dataset_id="1", metrics=[{"label": "n", "expression": "COUNT(*)"}], dimensions=["state"],
                     time_column="d", time_grain="day")
    governed = {"columns": ["d", "state", "n"], "rows": [["2026-01-05 00:00:00", "Open", 3], ["2026-01-05 00:00:00", "Closed", 2.0000000001]]}
    bi = {"columns": ["d", "Open", "Closed"], "rows": [[JAN5, 3, 2]]}
    assert svc.compare(q, governed, bi, truncated=False)["status"] == "verified"
    bi_bad = {"columns": ["d", "Open", "Closed"], "rows": [[JAN5, 4, 2]]}
    res = svc.compare(q, governed, bi_bad, truncated=False)
    assert res["status"] == "mismatch" and res["mismatches"][0]["metric"] == "n"
    assert svc.compare(q, governed, {"columns": ["x", "y"], "rows": []}, truncated=False)["status"] == "governed_only"
