"""GET /api/approvals/{id}: the requester and anyone who sees the workspace inbox read one approval's state;
an outsider gets the same 404 as an unknown id; the route decides nothing."""
from __future__ import annotations

import pytest
from tests.unit.step_fixtures import WS, world  # noqa: F401

from analystos.api.routers.artifacts import get_approval
from analystos.core.errors import NotFound
from analystos.db.base import session_scope
from analystos.db.models import Approval, User
from analystos.governance.approvals import request_approval


@pytest.fixture
def apr(world):  # noqa: F811
    with session_scope() as s:
        s.add(User(id="usr_outsider", email="outsider@x", name="outsider", password_hash="x", is_admin=False, active=True,
                   attributes={}))
        a = request_approval(s, workspace_id=WS, run_id=None, action="step_pin.tile", payload={"step": "stp_1", "v": 2},
                             plan_hash=None, policy_version=1, requested_by="usr_analyst", risk_tier="medium",
                             destination="tile:t1", affected_assets=["sales.orders"])
        return a.id


def _read(approval_id: str, uid: str) -> dict:
    with session_scope() as s:
        return get_approval(approval_id, user=s.get(User, uid), session=s).model_dump()


def test_the_requester_and_inbox_members_read_it(apr):
    mine = _read(apr, "usr_analyst")
    assert (mine["kind"], mine["status"], mine["requested_by"], mine["decided_by"]) == ("step_pin.tile", "pending",
                                                                                        "usr_analyst", None)
    assert mine["subject"] == {"run_id": None, "destination": "tile:t1", "affected_assets": ["sales.orders"]}
    assert len(mine["payload_hash"]) == 64 and mine["created_at"] and mine["expires_at"] and mine["decided_at"] is None
    assert "payload" not in mine
    assert _read(apr, "usr_viewer")["id"] == apr  # the inbox's own rule: a workspace viewer


def test_an_outsider_gets_the_same_404_as_an_unknown_id(apr):
    with pytest.raises(NotFound) as hidden:
        _read(apr, "usr_outsider")
    with pytest.raises(NotFound) as unknown:
        _read("apr_missing", "usr_outsider")
    assert str(hidden.value) == str(unknown.value)
    with session_scope() as s:
        assert s.get(Approval, apr).status == "pending"


def test_the_route_is_read_only():
    from analystos.api.routers.artifacts import router

    methods = {m for r in router.routes if getattr(r, "path", None) == "/api/approvals/{approval_id}" for m in r.methods}
    assert methods == {"GET"}
