"""Server-side authorization, scope resolution and plan/payload-bound approvals (§39, §45, §46).
Runs against a real Postgres control plane (the `control_db` fixture)."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from analystos.contracts.policy import ExecutionIdentity
from analystos.core.errors import ApprovalRequired, Conflict, Forbidden, NotFound, PolicyDenied
from analystos.core.ids import new_id, utcnow
from analystos.db.base import session_scope
from analystos.db.models import Source, SourceAsset, SourceColumn, User, WorkspaceMember
from analystos.governance import approvals
from analystos.governance.policy import evaluate, resolve_scope
from analystos.security.auth import hash_password
from analystos.services.workspaces import add_member, create_workspace, set_policy

pytestmark = pytest.mark.integration


def _user(s, email, admin=False, attrs=None):
    u = User(id=new_id("usr"), email=email, name=email, password_hash=hash_password("x"), is_admin=admin, attributes=attrs or {})
    s.add(u)
    s.flush()
    return u


@pytest.fixture()
def world(control_db):
    with session_scope() as s:
        owner = _user(s, f"owner-{new_id('x')}@t")
        analyst = _user(s, f"analyst-{new_id('x')}@t")
        approver = _user(s, f"approver-{new_id('x')}@t")
        viewer = _user(s, f"viewer-{new_id('x')}@t")
        outsider = _user(s, f"out-{new_id('x')}@t")
        ws = create_workspace(s, owner, name="Gov test", objective="Find SLA drivers", autonomy_level=3)
        s.flush()
        for u, role in ((analyst, "analyst"), (approver, "approver"), (viewer, "viewer")):
            add_member(s, owner, ws.id, u.email, role)
        src = Source(id=new_id("src"), workspace_id=ws.id, kind="servicenow", name="SN", config={}, status="ready", execution_mode="staged")
        s.add(src)
        s.flush()
        a = SourceAsset(id=new_id("ast"), source_id=src.id, workspace_id=ws.id, schema_name="src_t", name="incident",
                        source_name="incident", selected=True)
        b = SourceAsset(id=new_id("ast"), source_id=src.id, workspace_id=ws.id, schema_name="src_t", name="sys_user",
                        source_name="sys_user", selected=False)
        s.add_all([a, b])
        s.flush()
        for i, (n, tags) in enumerate([("number", []), ("made_sla", []), ("caller_email", ["pii"]), ("salary", ["restricted"])]):
            s.add(SourceColumn(asset_id=a.id, name=n, ordinal=i, data_type="text", tags=tags))
        ids = dict(ws=ws.id, owner=owner.id, analyst=analyst.id, approver=approver.id, viewer=viewer.id, outsider=outsider.id)
    return ids


def U(s, uid):
    return s.get(User, uid)


def test_scope_only_selected_assets_and_denies_pii_and_restricted(world):
    with session_scope() as s:
        scope = resolve_scope(s, U(s, world["analyst"]), world["ws"])
    assert scope.assets == ["src_t.incident"]
    assert set(scope.denied_columns) == {"src_t.incident.caller_email", "src_t.incident.salary"}
    assert scope.columns["src_t.incident"] == ["number", "made_sla", "caller_email", "salary"]


def test_pii_clearance_attribute_unlocks_pii_but_not_restricted(world):
    with session_scope() as s:
        u = U(s, world["analyst"])
        u.attributes = {"pii_clearance": True}
    with session_scope() as s:
        scope = resolve_scope(s, U(s, world["analyst"]), world["ws"])
    assert scope.denied_columns == ["src_t.incident.salary"]


def test_non_member_cannot_see_workspace(world):
    with session_scope() as s, pytest.raises(NotFound):
        resolve_scope(s, U(s, world["outsider"]), world["ws"])


def test_viewer_cannot_run_analysis(world):
    with session_scope() as s, pytest.raises(Forbidden):
        resolve_scope(s, U(s, world["viewer"]), world["ws"], minimum_role="analyst")


def test_policy_decisions(world):
    ident = ExecutionIdentity(user_id=world["analyst"], workspace_id=world["ws"])
    with session_scope() as s:
        analyst, owner = U(s, world["analyst"]), U(s, world["owner"])
        assert evaluate(s, analyst, ident, "run_analysis").decision == "allow"
        assert evaluate(s, analyst, ident, "source_mutation").decision == "deny"
        assert evaluate(s, analyst, ident, "publish", destination="superset").decision == "deny"  # role below editor
        d = evaluate(s, owner, ident.model_copy(update={"user_id": owner.id}), "publish", destination="superset")
        assert d.decision == "approval_required" and d.risk_tier == "high"
        assert evaluate(s, owner, ident, "publish", destination="tableau").decision == "deny"
        assert evaluate(s, analyst, ident, "run_analysis", autonomy_level=0).decision == "deny"
        assert evaluate(s, analyst, ident, "run_analysis", autonomy_level=2).decision == "approval_required"


def _proposal(s, world, payload=None, plan_hash="plan-A"):
    ws = s.get(__import__("analystos.db.models", fromlist=["Workspace"]).Workspace, world["ws"])
    return approvals.request_approval(s, workspace_id=world["ws"], run_id=None, action="publish_dashboard",
                                      payload=payload or {"dashboards": ["exec"]}, plan_hash=plan_hash, policy_version=ws.policy_version,
                                      requested_by=world["owner"], risk_tier="high", destination="superset", affected_assets=["x"])


def test_approval_bound_to_payload_and_plan(world):
    with session_scope() as s:
        a = _proposal(s, world)
        aid = a.id
    with session_scope() as s:
        approvals.decide(s, aid, U(s, world["approver"]), approve=True)
    with session_scope() as s:
        assert approvals.verify_for_execution(s, aid, payload={"dashboards": ["exec"]}, plan_hash="plan-A").status == "approved"
    with session_scope() as s, pytest.raises(ApprovalRequired, match="payload changed"):
        approvals.verify_for_execution(s, aid, payload={"dashboards": ["exec", "sneaky"]}, plan_hash="plan-A")
    with session_scope() as s:
        assert s.get(__import__("analystos.db.models", fromlist=["Approval"]).Approval, aid).status == "invalidated"


def test_replan_invalidates_approval(world):
    with session_scope() as s:
        aid = _proposal(s, world, payload={"v": 2}).id
    with session_scope() as s:
        approvals.decide(s, aid, U(s, world["approver"]), approve=True)
    with session_scope() as s, pytest.raises(ApprovalRequired, match="plan changed"):
        approvals.verify_for_execution(s, aid, payload={"v": 2}, plan_hash="plan-B")


def test_only_approver_roles_can_approve_and_only_once(world):
    with session_scope() as s:
        aid = _proposal(s, world, payload={"v": 3}).id
    with session_scope() as s, pytest.raises(Forbidden):
        approvals.decide(s, aid, U(s, world["analyst"]), approve=True)
    with session_scope() as s:
        approvals.decide(s, aid, U(s, world["approver"]), approve=False, reason="not yet")
    with session_scope() as s, pytest.raises(Conflict):
        approvals.decide(s, aid, U(s, world["approver"]), approve=True)


def test_separation_of_duties(world):
    with session_scope() as s:
        set_policy(s, U(s, world["owner"]), world["ws"], {"separation_of_duties": True})
    with session_scope() as s:
        aid = _proposal(s, world, payload={"v": 4}).id
    with session_scope() as s, pytest.raises(Forbidden, match="separation of duties"):
        approvals.decide(s, aid, U(s, world["owner"]), approve=True)


def test_policy_change_invalidates_pending_and_approved(world):
    with session_scope() as s:
        aid = _proposal(s, world, payload={"v": 5}).id
    with session_scope() as s:
        approvals.decide(s, aid, U(s, world["approver"]), approve=True)
    with session_scope() as s:
        set_policy(s, U(s, world["owner"]), world["ws"], {"max_rows": 1000})
    with session_scope() as s, pytest.raises(ApprovalRequired, match="policy changed"):
        approvals.verify_for_execution(s, aid, payload={"v": 5}, plan_hash="plan-A")


def test_revoked_approver_blocks_execution(world):
    with session_scope() as s:
        aid = _proposal(s, world, payload={"v": 6}).id
    with session_scope() as s:
        approvals.decide(s, aid, U(s, world["approver"]), approve=True)
    with session_scope() as s:  # approver removed while the run waits
        m = s.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == world["ws"], WorkspaceMember.user_id == world["approver"]))
        s.delete(m)
    with session_scope() as s, pytest.raises(PolicyDenied, match="approver no longer"):
        approvals.verify_for_execution(s, aid, payload={"v": 6}, plan_hash="plan-A")


def test_expired_approval(world):
    with session_scope() as s:
        a = _proposal(s, world, payload={"v": 7})
        a.expires_at = utcnow() - timedelta(minutes=1)
        aid = a.id
    with session_scope() as s, pytest.raises(Conflict, match="expired"):
        approvals.decide(s, aid, U(s, world["approver"]), approve=True)


def test_pause_and_resume_persist(world, monkeypatch):
    from analystos.db.models import AnalysisRun
    from analystos.services import runs as run_svc

    monkeypatch.setattr(run_svc, "signal_run", lambda run_id: None)
    with session_scope() as s:
        run = AnalysisRun(id=new_id("run"), workspace_id=world["ws"], objective="x" * 20, status="WAITING_USER",
                          requested_by=world["owner"], scope={}, instructions=[], constraints={})
        s.add(run)
    with session_scope() as s:
        user = U(s, world["analyst"])
        s.expunge(user)
    run_svc.control(user, run.id, "pause")
    with session_scope() as s:
        assert s.get(AnalysisRun, run.id).control == "pause"
    run_svc.control(user, run.id, "resume")
    with session_scope() as s:
        assert s.get(AnalysisRun, run.id).control == "run"
