"""P4-05: ownership transfer of a metric or the semantic model is a hash-bound offer that only the named new owner
accepts; a definition changed since the offer is refused; the approvals inbox cannot approve it; new versions keep
the owner."""
from __future__ import annotations

import pytest

from analystos.core.errors import Conflict, Forbidden, InvalidInput, PolicyDenied
from analystos.db.base import session_scope
from analystos.db.models import Approval, SemanticMetric, SemanticModel, User, Workspace, WorkspaceMember
from analystos.semantic import ownership as O

WS = "ws_own"


@pytest.fixture
def world(sqlite_db):
    with session_scope() as s:
        for uid, role in (("usr_a", "editor"), ("usr_b", "editor"), ("usr_v", "viewer"), ("usr_o", "owner")):
            s.add(User(id=uid, email=f"{uid}@own", name=uid, password_hash="x", is_admin=False, active=True, attributes={}))
            s.add(WorkspaceMember(workspace_id=WS, user_id=uid, role=role))
        s.add(Workspace(id=WS, name="own", created_by="usr_o", settings={}, policy_version=1))
        for v, status in ((1, "deprecated"), (2, "approved"), (3, "rejected")):
            s.add(SemanticMetric(id=f"met_{v}", workspace_id=WS, name="mttr_hours", version=v, status=status,
                                 definition={"name": "mttr_hours", "expressions": [{"dialect": "ANSI_SQL", "expression": "AVG(x)"}]},
                                 expression="AVG(x)", normalized_expression="avg(x)", owner_id="usr_a", proposed_by="usr_a",
                                 proposed_via="user", content_hash=f"h{v}"))
        s.add(SemanticModel(id="sem_1", workspace_id=WS, name="service", version=1, status="approved", owner_id="usr_a",
                            origin="user", content_hash="mh1", created_by="usr_a"))


def _user(s, uid):
    return s.get(User, uid)


def test_the_named_new_owner_accepts_and_every_version_moves(world):
    with session_scope() as s:
        apr = O.offer(s, _user(s, "usr_a"), WS, "metric", "mttr_hours", to_owner="usr_b", reason="team change")
        apr_id = apr.id
        assert apr.action == O.ACTION and apr.payload["content_hash"] == "h2" and apr.payload["from_owner"] == "usr_a"
    with session_scope() as s, pytest.raises(Forbidden, match="only the named new owner"):
        O.accept(s, _user(s, "usr_o"), WS, apr_id)  # not even a workspace owner accepts for them
    with session_scope() as s:
        out = O.accept(s, _user(s, "usr_b"), WS, apr_id)
        assert out["to_owner"] == "usr_b" and out["rows"] == 2
        owners = {m.version: m.owner_id for m in s.query(SemanticMetric)}
        assert owners == {1: "usr_b", 2: "usr_b", 3: "usr_a"}  # the rejected proposal is not the metric
        assert s.get(Approval, apr_id).status == "executed" and s.get(Approval, apr_id).decided_by == "usr_b"
    with session_scope() as s, pytest.raises(Conflict, match="executed"):
        O.accept(s, _user(s, "usr_b"), WS, apr_id)  # single use


def test_a_definition_changed_since_the_offer_is_refused(world):
    with session_scope() as s:
        apr_id = O.offer(s, _user(s, "usr_a"), WS, "model", "service", to_owner="usr_b").id
    with session_scope() as s:
        s.add(SemanticModel(id="sem_2", workspace_id=WS, name="service", version=2, status="approved", owner_id="usr_a",
                            origin="user", content_hash="mh2", created_by="usr_a"))
    with session_scope() as s, pytest.raises(PolicyDenied, match="changed since the offer"):
        O.accept(s, _user(s, "usr_b"), WS, apr_id)
    with session_scope() as s:
        assert s.get(SemanticModel, "sem_1").owner_id == "usr_a" and s.get(SemanticModel, "sem_2").owner_id == "usr_a"


def test_who_may_offer_and_to_whom(world):
    with session_scope() as s:
        with pytest.raises(Forbidden, match="only the owner"):
            O.offer(s, _user(s, "usr_b"), WS, "metric", "mttr_hours", to_owner="usr_b")
        with pytest.raises(InvalidInput, match="not an active editor"):
            O.offer(s, _user(s, "usr_a"), WS, "metric", "mttr_hours", to_owner="usr_v")
        with pytest.raises(InvalidInput, match="already owns"):
            O.offer(s, _user(s, "usr_o"), WS, "metric", "mttr_hours", to_owner="usr_a")
        # a workspace owner may hand over a definition someone else owns
        assert O.offer(s, _user(s, "usr_o"), WS, "metric", "mttr_hours", to_owner="usr_b").requested_by == "usr_o"


def test_the_recipient_declines_or_the_offer_is_withdrawn(world):
    with session_scope() as s:
        apr_id = O.offer(s, _user(s, "usr_a"), WS, "metric", "mttr_hours", to_owner="usr_b").id
    with session_scope() as s, pytest.raises(Forbidden):
        O.decline(s, _user(s, "usr_o"), WS, apr_id)
    with session_scope() as s:
        assert O.decline(s, _user(s, "usr_b"), WS, apr_id).status == "rejected"


def test_the_approvals_inbox_cannot_approve_an_offer(world, monkeypatch):
    from analystos.api.routers import artifacts

    with session_scope() as s:
        apr_id = O.offer(s, _user(s, "usr_a"), WS, "metric", "mttr_hours", to_owner="usr_b").id
    with session_scope() as s, pytest.raises(InvalidInput, match="recipient"):
        artifacts._decide(apr_id, _user(s, "usr_o"), s, True, None)


def test_a_new_metric_version_keeps_the_owner(world):
    from analystos.contracts.semantic import DialectExpression, SemanticMetricDef
    from analystos.semantic.service import propose_metric

    with session_scope() as s:
        defn = SemanticMetricDef(name="mttr_hours", expressions=[DialectExpression(expression="AVG(y)")])
        row, created = propose_metric(s, WS, defn, proposed_by="usr_b", via="user")
        assert created and row.owner_id == "usr_a" and row.proposed_by == "usr_b"
