"""P4-C02 (query budgets) and P4-C11 (tool-gate coverage) against a real control plane.

The gateway is a recording fake that writes the same `query_execution` audit rows the real one
does, so budgets are counted exactly as in production without staging any data."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from analystos.contracts.policy import DataScope
from analystos.core.errors import BudgetExceeded, PolicyDenied, SQLRejected
from analystos.core.ids import new_id, utcnow
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Artifact, QueryExecution, RunTask, ToolExecution, User, Workspace
from analystos.governance.policy import load_policy
from analystos.security.auth import hash_password
from analystos.services.workspaces import add_member, create_workspace, set_policy

pytestmark = pytest.mark.integration


class RecordingGateway:
    """Writes one audit row per statement (as QueryGateway does) and returns a minimal result."""

    def __init__(self, reject: bool = False) -> None:
        self.calls: list[dict] = []
        self.reject = reject

    def _record(self, scope, sql, *, actor, purpose, run_id=None, task_id=None, **_):  # noqa: ANN001, ANN003, ANN202
        self.calls.append({"sql": sql, "actor": actor, "purpose": purpose, "run_id": run_id})
        with session_scope() as s:
            s.add(QueryExecution(id=new_id("qry"), workspace_id=scope.workspace_id, run_id=run_id, task_id=task_id,
                                 actor=actor, purpose=purpose, sql=sql, status="rejected" if self.reject else "ok"))
        if self.reject:
            raise SQLRejected("column nope does not exist")
        return SimpleNamespace(query_id="q", columns=["n"], rows=[[1]], row_count=1, truncated=False, result_hash="h")

    def execute(self, scope, sql, **kw):  # noqa: ANN001, ANN003, ANN201
        return self._record(scope, sql, **kw)

    def run_sql_for(self, scope, *, actor, run_id=None, task_id=None, source_id=None):  # noqa: ANN001, ANN201
        gw = self

        class _Runner:
            dialect = "postgres"

            def __call__(self, sql, *, purpose="analysis", max_rows=None, use_cache=True):  # noqa: ANN001, ANN204
                return gw._record(scope, sql, actor=actor, purpose=purpose, run_id=run_id, task_id=task_id)

        return _Runner()


def _user(s, email):  # noqa: ANN001, ANN202
    u = User(id=new_id("usr"), email=email, name=email, password_hash=hash_password("x"))
    s.add(u)
    s.flush()
    return u


@pytest.fixture()
def world(control_db):
    with session_scope() as s:
        owner = _user(s, f"owner-{new_id('x')}@t")
        analyst = _user(s, f"analyst-{new_id('x')}@t")
        ws = create_workspace(s, owner, name="Gate test", objective="", autonomy_level=3)
        s.flush()
        add_member(s, owner, ws.id, analyst.email, "analyst")
        run = AnalysisRun(id=new_id("run"), workspace_id=ws.id, objective="x" * 20, requested_by=owner.id, scope={},
                          instructions=[], constraints={}, plan_version=1)
        s.add(run)
        s.flush()
        tasks = {}
        for key, agent in (("verify", "critic"), ("profile", "profiler")):
            t = RunTask(id=new_id("tsk"), run_id=run.id, key=key, agent_id=agent, title=key, plan_version=1)
            s.add(t)
            tasks[agent] = t.id
        ids = {"ws": ws.id, "owner": owner.id, "analyst": analyst.id, "run": run.id, "tasks": tasks}
    return ids


def _policy(world, **doc):  # noqa: ANN001, ANN003, ANN202
    with session_scope() as s:
        set_policy(s, s.get(User, world["owner"]), world["ws"], doc)


def _scope(world, user_key="owner"):  # noqa: ANN001, ANN202
    return DataScope(workspace_id=world["ws"], user_id=world[user_key], role="owner", source_ids=["src_gate"],
                     assets=["src_gate.t"], asset_sources={"src_gate.t": "src_gate"}, columns={"src_gate.t": ["n"]},
                     source_dialects={"src_gate": "postgres"})


def _run_ctx(world, agent_id, gateway):  # noqa: ANN001, ANN202
    from analystos.runtime.context import RunContext, Services
    from analystos.tools.registry import get_agent_spec

    with session_scope() as s:
        run, task = s.get(AnalysisRun, world["run"]), s.get(RunTask, world["tasks"][agent_id])
        user, ws = s.get(User, world["owner"]), s.get(Workspace, world["ws"])
        policy, agent = load_policy(s, ws), get_agent_spec(s, agent_id)
        s.expunge_all()
    return RunContext(run=run, task=task, user=user, workspace=ws, policy=policy, scope=_scope(world), agent=agent,
                      services=Services(router=None, gateway=gateway))


def _ask_ctx(world, gateway, user_key="owner"):  # noqa: ANN001, ANN202
    from analystos.runtime.context import Services
    from analystos.tools.registry import get_agent_spec

    with session_scope() as s:
        user, ws = s.get(User, world[user_key]), s.get(Workspace, world["ws"])
        policy, agent = load_policy(s, ws), get_agent_spec(s, "sql")
        s.expunge_all()
    return SimpleNamespace(user=user, workspace=ws, scope=_scope(world, user_key), policy=policy, agent=agent,
                           services=Services(router=None, gateway=gateway), run=None, task=None)


def _denials(world, tool_id):  # noqa: ANN001, ANN202
    with session_scope() as s:
        return s.scalar(select(func.count()).select_from(ToolExecution).where(
            ToolExecution.workspace_id == world["ws"], ToolExecution.tool_id == tool_id, ToolExecution.status == "denied"))


# ------------------------------------------------------------------------------------ P4-C11
def test_denylisted_sql_execute_blocks_every_path_it_names(world):
    from analystos.agents.sql_agent import ask

    _policy(world, tool_denylist=["sql.execute"])
    gw = RecordingGateway()
    with pytest.raises(PolicyDenied, match="tool_denied_by_workspace_policy"):  # skill SQL (profiling, analysis...)
        _run_ctx(world, "profiler", gw).run_sql("src_gate")
    critic = _run_ctx(world, "critic", gw)
    with pytest.raises(PolicyDenied, match="tool_denied_by_workspace_policy"):  # REV verification re-runs
        critic.run_sql("src_gate")
    with pytest.raises(PolicyDenied, match="tool_denied_by_workspace_policy"):  # explicit invocation
        critic.tools().invoke("sql.execute", {"purpose": "x"}, lambda: gw.execute(critic.scope, "SELECT 1", actor="a", purpose="x"))
    with pytest.raises(PolicyDenied, match="tool_denied_by_workspace_policy"):  # Ask, before any model call
        ask(_ask_ctx(world, gw), "how many?")
    assert gw.calls == []
    assert _denials(world, "sql.execute") == 4  # every denial is recorded, not rolled back with the raise


def test_denylisted_artifact_write_blocks_agent_artifacts_only(world):
    from analystos.artifacts.registry import save_artifact

    _policy(world, tool_denylist=["artifact.write"])
    with pytest.raises(PolicyDenied, match="artifact.write"), session_scope() as s:
        save_artifact(s, workspace_id=world["ws"], type_="profile", name="p", content={"a": 1}, run_id=world["run"],
                      creator_agent="profiler")
    assert _denials(world, "artifact.write") == 1  # recorded although the caller's transaction rolled back
    with session_scope() as s:  # a signed-in user's own artifact is governed by its route, not the agent tool gate
        save_artifact(s, workspace_id=world["ws"], type_="report", name="r", content={"a": 1}, run_id=world["run"],
                      creator_user=world["owner"])
    with session_scope() as s:
        names = set(s.scalars(select(Artifact.name).where(Artifact.workspace_id == world["ws"])))
    assert names == {"r"}
    gw = RecordingGateway()
    _run_ctx(world, "profiler", gw).run_sql("src_gate")("SELECT 1")  # a different tool is unaffected
    assert len(gw.calls) == 1


def test_implicit_paths_work_without_a_denylist_entry(world):
    from analystos.artifacts.registry import save_artifact

    gw = RecordingGateway()
    ctx = _run_ctx(world, "profiler", gw)
    ctx.run_sql("src_gate")("SELECT 1", purpose="profile.table")
    with session_scope() as s:
        art = save_artifact(s, workspace_id=world["ws"], type_="profile", name="p", content={"a": 1}, run_id=world["run"],
                            creator_agent="profiler")
        assert art.version == 1
    assert gw.calls[0]["actor"] == "agent:profiler" and gw.calls[0]["run_id"] == world["run"]


# ------------------------------------------------------------------------------------ P4-C02
def test_verification_reruns_count_toward_the_run_budget(world):
    _policy(world, max_queries_per_run=3)
    gw = RecordingGateway()
    _run_ctx(world, "profiler", gw).run_sql("src_gate")("SELECT 1")  # an earlier step used one statement
    critic = _run_ctx(world, "critic", gw)
    rerun = critic.run_sql("src_gate")
    rerun("SELECT 1", purpose="verification.rerun", use_cache=False)
    rerun("SELECT 1", purpose="verification.rerun", use_cache=False)
    with pytest.raises(BudgetExceeded, match=r"per-run query budget \(3\)"):
        rerun("SELECT 1", purpose="verification.rerun", use_cache=False)
    assert [c["purpose"] for c in gw.calls] == ["analysis", "verification.rerun", "verification.rerun"]


def test_ask_has_a_per_user_and_per_workspace_budget(world):
    from analystos.governance.budgets import check_ask_budget

    _policy(world, ask_queries_per_user_per_hour=2, ask_queries_per_workspace_per_hour=3)
    with session_scope() as s:
        for uid, age in ((world["owner"], 0), (world["owner"], 0), (world["analyst"], 0), (world["analyst"], 180)):
            s.add(QueryExecution(id=new_id("qry"), workspace_id=world["ws"], actor=f"user:{uid}", purpose="ask", sql="SELECT 1",
                                 status="ok", created_at=utcnow() - timedelta(minutes=age)))
    with session_scope() as s:
        policy = load_policy(s, s.get(Workspace, world["ws"]))
        with pytest.raises(BudgetExceeded) as user_over:
            check_ask_budget(s, world["ws"], world["owner"], policy)
        assert user_over.value.details["scope"] == "user" and user_over.value.http_status == 429
        with pytest.raises(BudgetExceeded) as ws_over:  # the analyst has 1 of 2, but the workspace has 3 of 3
            check_ask_budget(s, world["ws"], world["analyst"], policy)
        assert ws_over.value.details["scope"] == "workspace"


def test_ask_counts_every_attempt_and_stops_repairing_at_the_budget(world, monkeypatch):
    from analystos.agents import sql_agent

    _policy(world, ask_queries_per_user_per_hour=2)
    monkeypatch.setattr(sql_agent, "catalog_for_prompt", lambda ctx, **kw: "catalog")
    model_calls: list[str] = []

    def fake_llm(ctx, purpose, prompt, payload, **kw):  # noqa: ANN001, ANN003, ANN202
        model_calls.append(purpose)
        return {"sql": f"SELECT nope_{len(model_calls)}"}, "test/model"

    monkeypatch.setattr(sql_agent, "llm_json", fake_llm)
    gw = RecordingGateway(reject=True)
    with pytest.raises(BudgetExceeded, match="per user per hour"):
        sql_agent.ask(_ask_ctx(world, gw), "how many?", max_repairs=5)
    assert len(gw.calls) == 2 and all(c["actor"] == f"user:{world['owner']}" and c["purpose"] == "ask" for c in gw.calls)
    assert model_calls == ["sql_generation", "sql_repair", "sql_repair"]
    model_calls.clear()
    with pytest.raises(BudgetExceeded):  # already over: refused before any model call
        sql_agent.ask(_ask_ctx(world, gw), "again?")
    assert model_calls == [] and len(gw.calls) == 2
