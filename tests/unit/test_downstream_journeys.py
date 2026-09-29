"""What happens after an answer or a run (no services): an Ask turn never stays "running" and its stream
always ends; stale running work is swept; a pending promotion (dashboard, schedule) lives on the turn with
its approval's status and is reused, not duplicated; an Ask answer becomes an HTML report from its stored
result only; a missing report file is a 404 with a remedy; a report failure does not fail a run; ML compute
falls back in-process only when Temporal is unreachable; a publication is rolled back once."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.unit.step_fixtures import WS, world  # noqa: F401

from analystos.core.errors import Conflict, InvalidInput, NotFound, UpstreamUnavailable
from analystos.core.ids import new_id, utcnow
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Approval, AskThread, AskTurn
from analystos.services import ask as ask_svc

ROWS = [["Network", 3], ["Email", 7]]


def _thread(user_id: str = "usr_owner") -> str:
    with session_scope() as s:
        t = AskThread(id=new_id("ask"), workspace_id=WS, user_id=user_id, title="t", archived=False, revision=1)
        s.add(t)
        return t.id


def _answered_turn(thread_id: str, user_id: str = "usr_owner", **over) -> str:
    with session_scope() as s:
        turn = AskTurn(id=new_id("askt"), thread_id=thread_id, workspace_id=WS, user_id=user_id, seq=1,
                       question="How many incidents per channel <b>now</b>?", parameters={}, status="answered",
                       sql="SELECT channel, COUNT(*) AS n FROM sales.orders GROUP BY 1", explanation="The model says 999.",
                       result={"columns": ["channel", "n"], "rows": ROWS, "row_count": 2, "truncated": False,
                               "query_id": "qry_1", "result_hash": "rh"},
                       answered_by="model", model="m1", attempts=[], stages=[], decisions=[],
                       provenance={"assets": [{"asset": "sales.orders", "asset_id": "ast_orders", "source_name": "sales"}]},
                       promotions=[], **over)
        s.add(turn)
        return turn.id


def _user(uid: str):
    from tests.unit.step_fixtures import user

    return user(uid)


# ------------------------------------------------------------------------------------ never stuck "running"
def test_an_unexpected_error_ends_the_turn_as_a_failed_refusal(world, monkeypatch):  # noqa: F811
    thread = _thread()
    ctx = SimpleNamespace(scope=SimpleNamespace(assets=["sales.orders"]))
    monkeypatch.setattr(ask_svc, "adhoc_context", lambda s, u, ws: ctx)

    def boom(ctx, question, parameters=None):
        raise RuntimeError("socket closed with secret=abc")

    turn = ask_svc.ask_in_thread(_user("usr_owner"), thread, "How many orders?", ask_fn=boom)
    assert turn["status"] == "refused" and turn["refusal"]["kind"] == "failed"
    assert "secret" not in turn["refusal"]["message"] and turn["stages"][-1]["key"] == "done"


def test_a_turn_whose_result_cannot_be_saved_is_marked_failed(world, monkeypatch):  # noqa: F811
    thread = _thread()
    monkeypatch.setattr(ask_svc, "adhoc_context", lambda s, u, ws: SimpleNamespace(scope=SimpleNamespace(assets=["x"])))

    def fail(*a, **k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(ask_svc, "_persist_turn", fail)
    with pytest.raises(RuntimeError):
        ask_svc.ask_in_thread(_user("usr_owner"), thread, "How many orders?",
                              ask_fn=lambda ctx, q, parameters=None: {"status": "answered"})
    with session_scope() as s:
        (turn,) = s.query(AskTurn).filter(AskTurn.thread_id == thread).all()
        assert turn.status == "refused" and turn.refusal["kind"] == "failed"


def test_the_stream_always_ends_after_an_unexpected_error():
    def crashing(user, thread_id, question, parameters, *, on_stage):
        on_stage({"key": "scope", "text": "Checking"})
        raise KeyError("internal detail")

    async def collect():
        return [f async for f in ask_svc.stream_turn(SimpleNamespace(id="u"), "ask_1", "q", ask=crashing)]

    frames = asyncio.run(collect())
    events = [f.split("\n", 1)[0] for f in frames]
    assert events == ["event: stage", "event: error", "event: end"]
    assert "internal detail" not in frames[1] and "unexpected error" in frames[1]


def test_the_sweep_ends_stale_running_turns_and_experiments_only(world, sqlite_db):  # noqa: F811
    from analystos.db import models
    from analystos.db.models import MLExperiment
    from analystos.services import stale_work

    models.Base.metadata.create_all(sqlite_db.kw["bind"], tables=[models.Base.metadata.tables["ml_experiment"]])
    thread = _thread()
    old, now = utcnow() - timedelta(hours=3), utcnow()
    with session_scope() as s:
        for tid, at in (("askt_old", old), ("askt_new", now)):
            s.add(AskTurn(id=tid, thread_id=thread, workspace_id=WS, user_id="usr_owner", seq=1, question="q", parameters={},
                          status="running", attempts=[], stages=[], decisions=[], provenance={}, promotions=[], created_at=at))
        for eid, at in (("mlx_old", utcnow() - timedelta(days=1)), ("mlx_new", now)):
            s.add(MLExperiment(id=eid, workspace_id=WS, definition_key="k", task="classify", spec={}, spec_hash="h",
                               dataset_asset="sales.orders",
                               status="running", readiness={}, summary={}, artifacts={}, query_ids=[], created_by="user:usr_owner",
                               created_at=at))
    out = stale_work.sweep()
    assert out == {"ask_turns": ["askt_old"], "ml_experiments": ["mlx_old"]}
    with session_scope() as s:
        assert s.get(AskTurn, "askt_old").refusal["kind"] == "failed" and s.get(AskTurn, "askt_new").status == "running"
        assert s.get(MLExperiment, "mlx_old").status == "failed" and s.get(MLExperiment, "mlx_new").status == "running"
    assert stale_work.sweep() == {"ask_turns": [], "ml_experiments": []}


# ------------------------------------------------------------------------------------ pending promotions
def test_a_pending_promotion_carries_its_approvals_status_and_completing_replaces_it(world):  # noqa: F811
    from analystos.governance.approvals import request_approval

    with session_scope() as s:
        a = request_approval(s, workspace_id=WS, run_id=None, action=ask_svc.DASHBOARD_ACTION, payload={"turn_id": "t1"},
                             plan_hash=None, policy_version=1, requested_by="usr_owner", risk_tier="medium",
                             destination="preview", affected_assets=[])
        a.status = "approved"
        apr = a.id
    pending = {"target": "dashboard", "id": apr, "status": "approval_required", "approval_id": apr, "at": "t0"}
    with session_scope() as s:
        (view,) = ask_svc.promotions_view(s, [pending])
        assert view["approval_status"] == "approved"
        s.get(Approval, apr).expires_at = utcnow() - timedelta(minutes=1)
        s.flush()
        assert ask_svc.promotions_view(s, [pending])[0]["approval_status"] == "expired"
    done = ask_svc.record_promotion([{"target": "metric", "id": "m"}, pending],
                                    {"target": "dashboard", "id": "art_1", "status": "published", "approval_id": apr})
    assert [p["status"] if "status" in p else p["id"] for p in done] == ["m", "published"] and done[1]["requested_at"] == "t0"


def test_an_expired_request_is_replaced_not_reused(world):  # noqa: F811
    from analystos.governance.approvals import request_approval

    def ask_once():
        with session_scope() as s:
            return request_approval(s, workspace_id=WS, run_id=None, action="ask.schedule", payload={"k": 1}, plan_hash=None,
                                    policy_version=1, requested_by="usr_owner", risk_tier="medium", destination=None,
                                    affected_assets=[]).id

    first = ask_once()
    assert ask_once() == first  # a retry reuses the open request
    with session_scope() as s:
        s.get(Approval, first).expires_at = utcnow() - timedelta(minutes=1)
    assert ask_once() != first


def test_a_schedule_request_is_recorded_on_the_turn_and_reused(world, monkeypatch):  # noqa: F811
    from analystos.services import saved_analysis, schedules

    monkeypatch.setattr(schedules, "validate", lambda *a, **k: None)
    turn_id = _answered_turn(_thread())
    owner = _user("usr_owner")

    def request():
        with session_scope() as s:
            return saved_analysis.request_schedule(s, s.merge(owner), turn_id, name="n", cron="0 9 * * *", timezone="UTC")

    first, again = request(), request()
    assert first["status"] == "approval_required" and again["approval_id"] == first["approval_id"]
    with session_scope() as s:
        promotions = s.get(AskTurn, turn_id).promotions
        view = ask_svc.promotions_view(s, promotions)
    assert [(p["target"], p["status"], p["cron"]) for p in promotions] == [("schedule", "approval_required", "0 9 * * *")]
    assert view[0]["approval_status"] == "pending"


# ------------------------------------------------------------------------------------ answer report
def test_the_answer_report_shows_the_stored_result_escaped_and_no_model_text():
    from analystos.reports.answer import render_answer_html

    html = render_answer_html({"title": "Why <script>x</script>?", "workspace_name": "w", "turn_id": "askt_1", "thread_id": "ask_1",
                               "answered_by": "model", "model": "m1", "governance": "ad_hoc", "sql": "SELECT 1 < 2",
                               "result": {"columns": ["channel", "n"], "rows": ROWS, "row_count": 2, "query_id": "qry_1",
                                          "result_hash": "rh"},
                               "assets": [{"asset": "sales.orders"}], "staleness": {"label": "Data as of 2 hours ago"},
                               "evidence_status": {"state": "ad_hoc", "reasons": []}, "verification": None})
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html
    assert "<td>Network</td><td>3</td>" in html and "<td>Email</td><td>7</td>" in html and "SELECT 1 &lt; 2" in html
    assert "qry_1" in html and "not verified" in html


def test_an_answer_is_saved_as_a_downloadable_report_and_a_missing_file_says_regenerate(world, monkeypatch, tmp_path):  # noqa: F811
    from analystos.core.config import get_settings
    from analystos.db.models import Artifact
    from analystos.services import ask_outputs, reports

    monkeypatch.setattr(get_settings(), "artifact_dir", tmp_path)
    turn_id = _answered_turn(_thread())
    with session_scope() as s:
        record = ask_outputs.answer_report(s, s.merge(_user("usr_owner")), s.get(AskTurn, turn_id))
    with session_scope() as s:
        art = s.get(Artifact, record["id"])
        assert art.type == "report" and art.run_id is None and art.content["origin"]["turn_id"] == turn_id
        body, mime, ext = reports.report_file(art, "html")
        assert ext == "html" and b"Network" in body and b"999" not in body  # the model's text is not in the report
        with pytest.raises(NotFound, match="format pdf not generated"):
            reports.report_file(art, "pdf")
        Path(art.content["files"]["html"]["path"]).unlink()
        with pytest.raises(NotFound, match="generate the report again") as err:
            reports.report_file(art, "html")
        assert err.value.details["remedy"] == "regenerate"


def test_a_dashboard_bundle_keeps_earlier_answers_and_revalidates_them():
    from analystos.services import ask_outputs

    def validate(scope, sql, max_rows):
        if "secret" in sql:
            raise InvalidInput("not in scope")

    import analystos.gateway.validator as validator

    mp = pytest.MonkeyPatch()
    mp.setattr(validator, "validate_sql", validate)
    try:
        entries = [ask_outputs._entry("askt_a", "Earlier", "SELECT a FROM t", ["a"], []),
                   ask_outputs._entry("askt_b", "Gone", "SELECT secret FROM t", ["secret"], []),
                   ask_outputs._entry("askt_c", "Now", "SELECT c, n FROM t", ["c", "n"], ["t"])]
        bundle, dropped = ask_outputs.answer_bundle(None, WS, "Ask answers", "preview", entries)
        assert [c.key for c in bundle.charts] == ["ask_askt_a", "ask_askt_c"] and dropped == ["Gone"]
        assert bundle.dashboards[0].key == "ask_answers" and bundle.dashboards[0].charts == ["ask_askt_a", "ask_askt_c"]
        with pytest.raises(InvalidInput, match="no longer passes"):
            ask_outputs.answer_bundle(None, WS, "Ask answers", "preview", entries[:2])
    finally:
        mp.undo()


def test_superset_without_the_bi_profile_binds_the_request_to_preview(monkeypatch):
    from analystos.core.config import get_settings
    from analystos.core.errors import PolicyDenied
    from analystos.services import ask_outputs

    monkeypatch.setattr(get_settings(), "superset_url", "")
    destination, note = ask_outputs.usable_destination(["superset"], "superset")
    assert destination == "preview" and "preview" in note
    with pytest.raises(PolicyDenied):
        ask_outputs.usable_destination(["superset"], "powerbi")
    with pytest.raises(InvalidInput, match="Power BI publishing is not implemented"):
        ask_outputs.usable_destination(["powerbi"], "powerbi")


# ------------------------------------------------------------------------------------ reports never fail a run
def test_a_report_failure_is_recorded_on_the_run_not_raised(world, monkeypatch):  # noqa: F811
    from analystos.agents import supervisor
    from analystos.services import reports

    with session_scope() as s:
        s.add(AnalysisRun(id="run_r", workspace_id=WS, objective="o", status="RUNNING", requested_by="usr_owner",
                          summary={"summary_markdown": "kept"}))

    def disabled(*a, **k):
        raise InvalidInput("report generation is turned off by the administrator")

    monkeypatch.setattr(reports, "generate_report", disabled)
    said: list[str] = []
    ctx = SimpleNamespace(run=SimpleNamespace(id="run_r"), agent=SimpleNamespace(id="supervisor"),
                          say=lambda text, kind=None: said.append(text))
    assert supervisor._scheduled_report(ctx, {"kind": "weekly_summary", "formats": ["pdf"]}) is None
    with session_scope() as s:
        summary = s.get(AnalysisRun, "run_r").summary
    assert summary["summary_markdown"] == "kept" and "turned off" in summary["report_error"] and said


# ------------------------------------------------------------------------------------ ML compute fallback
class _Handle:
    def __init__(self, outcome):
        self.outcome = outcome

    async def result(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _Client:
    def __init__(self, start_error=None, outcome=None):
        self.start_error, self.outcome, self.started = start_error, outcome, 0

    async def start_workflow(self, *a, **k):
        self.started += 1
        if self.start_error:
            raise self.start_error
        return _Handle(self.outcome)


@pytest.fixture
def temporal(monkeypatch):
    from analystos.ml import jobs
    from analystos.workers import dispatch
    from analystos.workflows import orchestrator, queues

    ran: list[dict] = []
    monkeypatch.setattr(orchestrator, "get_settings", lambda: SimpleNamespace(orchestrator="temporal", ml_max_seconds=1,
                                                                              temporal_queue_prefix="aos"))
    monkeypatch.setattr(dispatch, "pool_configured", lambda *a, **k: False)
    monkeypatch.setattr(queues, "workflow_options", lambda *a, **k: {})
    monkeypatch.setattr(jobs, "run_ml_job", lambda job: ran.append(job) or {"status": "in-process"})

    def use(client=None, connect_error=None):
        async def connect():
            if connect_error:
                raise connect_error
            return client

        monkeypatch.setattr(orchestrator, "_temporal_client", connect)
        return ran

    return use


def test_ml_compute_runs_in_process_only_when_temporal_is_unreachable(temporal):
    from analystos.workflows.orchestrator import run_ml_compute

    ran = temporal(connect_error=RuntimeError("Failed client connect: connection refused"))
    assert run_ml_compute({"k": 1}) == {"status": "in-process"} and ran == [{"k": 1}]


def test_a_workflow_failure_on_temporal_is_raised_not_rerun(temporal):
    from analystos.workflows.orchestrator import run_ml_compute

    client = _Client(outcome=ValueError("activity failed: training crashed"))
    ran = temporal(client=client)
    with pytest.raises(UpstreamUnavailable, match="did not finish"):
        run_ml_compute({"k": 1})
    assert client.started == 1 and ran == []
    ok = _Client(outcome={"status": "succeeded"})
    temporal(client=ok)
    assert run_ml_compute({"k": 2}) == {"status": "succeeded"}


def test_an_unavailable_service_at_start_is_unreachable_but_a_timeout_is_not():
    from analystos.workflows.orchestrator import _connection_failure

    class RPCError(Exception):
        status = SimpleNamespace(name="UNAVAILABLE")

    assert _connection_failure(RPCError()) and _connection_failure(ConnectionRefusedError())
    assert not _connection_failure(TimeoutError()) and not _connection_failure(ValueError())


# ------------------------------------------------------------------------------------ rollback once
def test_a_publication_is_rolled_back_once(world, sqlite_db, monkeypatch):  # noqa: F811
    from analystos.api.routers import artifacts
    from analystos.db import models
    from analystos.db.models import Artifact, Publication
    from analystos.publishing import base

    models.Base.metadata.create_all(sqlite_db.kw["bind"], tables=[models.Base.metadata.tables["publication"]])
    removed: list[dict] = []
    monkeypatch.setattr(base, "get_publisher", lambda d, s=None: SimpleNamespace(rollback=lambda ids: removed.append(ids) or ["x"]))
    with session_scope() as s:
        s.add(Publication(id="pub_1", workspace_id=WS, run_id=None, approval_id=None, destination="preview",
                          idempotency_key="k1", status="succeeded", external_ids={"charts": {"ask_askt_1": "preview:c1"}}))
        for aid, ext in (("art_mine", "preview:c1"), ("art_other", "preview:c2")):
            s.add(Artifact(id=aid, workspace_id=WS, run_id=None, type="chart", name=aid, status="published", platform="preview",
                           external_id=ext, content={}, content_hash="h"))
    owner = _user("usr_owner")
    with session_scope() as s:
        artifacts.rollback("pub_1", user=owner, session=s)
    with session_scope() as s:
        assert s.get(Artifact, "art_mine").status == "rolled_back" and s.get(Artifact, "art_other").status == "published"
        with pytest.raises(Conflict, match="already rolled back"):
            artifacts.rollback("pub_1", user=owner, session=s)
    assert len(removed) == 1
