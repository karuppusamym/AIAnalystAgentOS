"""Approval and retry boundaries for report/alert egress (no real network calls)."""
from types import SimpleNamespace

import pytest

from analystos.core.errors import ApprovalRequired, PolicyDenied, UpstreamUnavailable
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import Alert, Approval, Monitor, User
from analystos.governance import approvals
from analystos.security.auth import hash_password
from analystos.services import external_delivery, schedules
from analystos.services.workspaces import add_member, create_workspace

pytestmark = pytest.mark.integration


def _user(session, label):
    user = User(id=new_id("usr"), email=f"{label}-{new_id('x')}@test.invalid", name=label,
                password_hash=hash_password("x"), is_admin=False, attributes={})
    session.add(user)
    session.flush()
    return user


def test_alert_webhook_requires_exact_approval_and_retries(control_db, monkeypatch):
    settings = SimpleNamespace(external_webhook_hosts="hooks.example.com", external_webhook_secret="test-signing-key",
                               external_email_domains="", external_smtp_host=None, external_smtp_sender=None,
                               outbound_private_hosts="")
    monkeypatch.setattr(external_delivery, "get_settings", lambda: settings)
    with session_scope() as session:
        owner, approver = _user(session, "owner"), _user(session, "approver")
        ws = create_workspace(session, owner, name="Delivery test", objective="Test egress", autonomy_level=3)
        session.flush()
        add_member(session, owner, ws.id, approver.email, "approver")
        alert = Alert(id=new_id("alt"), workspace_id=ws.id, severity="warning", title="Outlier",
                      message="Exceeded threshold", data={"count": 7}, dedupe_key=new_id("dedupe"))
        session.add(alert)
        session.flush()
        with pytest.raises(PolicyDenied):
            external_delivery.request(session, owner, ws.id, subject_type="alert", subject_id=alert.id,
                                      channel="webhook", destination="https://evil.example/hook")
        approval = external_delivery.request(session, owner, ws.id, subject_type="alert", subject_id=alert.id,
                                             channel="webhook", destination="https://hooks.example.com/hook")
        aid, owner_id, approver_id = approval.id, owner.id, approver.id

    with session_scope() as session:
        with pytest.raises(ApprovalRequired):
            external_delivery.execute(session.get(User, owner_id), aid)
        approvals.decide(session, aid, session.get(User, approver_id), approve=True)

    calls = []

    def send(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            raise UpstreamUnavailable("temporary outage")
        return {"status_code": 200}

    monkeypatch.setattr(external_delivery.outbound, "call", send)
    with session_scope() as session, pytest.raises(UpstreamUnavailable):
        external_delivery.execute(session.get(User, owner_id), aid)
    with session_scope() as session:
        approval = session.get(Approval, aid)
        assert approval.status == "approved"
        assert approval.evidence["attempts"] == 1
    with session_scope() as session:
        assert external_delivery.execute(session.get(User, owner_id), aid) == {
            "approval_id": aid, "status": "delivered", "attempts": 2}
    with session_scope() as session:
        external_delivery.execute(session.get(User, owner_id), aid)
    assert len(calls) == 2
    assert calls[0][1]["headers"]["Idempotency-Key"] == aid
    assert calls[1][1]["headers"]["Idempotency-Key"] == aid


def test_alert_mutation_blocks_approved_delivery(control_db, monkeypatch):
    monkeypatch.setattr(external_delivery, "get_settings", lambda: SimpleNamespace(
        external_webhook_hosts="hooks.example.com", external_webhook_secret="secret",
        external_email_domains="", external_smtp_host=None, external_smtp_sender=None,
        outbound_private_hosts=""))
    with session_scope() as session:
        owner, approver = _user(session, "owner"), _user(session, "approver")
        ws = create_workspace(session, owner, name="Delivery mutation", objective="Test egress", autonomy_level=3)
        session.flush()
        add_member(session, owner, ws.id, approver.email, "approver")
        alert = Alert(id=new_id("alt"), workspace_id=ws.id, severity="warning", title="Original",
                      message="Original", data={}, dedupe_key=new_id("dedupe"))
        session.add(alert)
        session.flush()
        approval = external_delivery.request(session, owner, ws.id, subject_type="alert", subject_id=alert.id,
                                             channel="webhook", destination="https://hooks.example.com/hook")
        aid, owner_id, approver_id, alert_id = approval.id, owner.id, approver.id, alert.id
    with session_scope() as session:
        approvals.decide(session, aid, session.get(User, approver_id), approve=True)
        session.get(Alert, alert_id).message = "Changed"
    with session_scope() as session, pytest.raises(ApprovalRequired):
        external_delivery.execute(session.get(User, owner_id), aid)


def test_monitor_schedule_proposes_approval_without_sending(control_db, monkeypatch):
    monkeypatch.setattr(external_delivery, "get_settings", lambda: SimpleNamespace(
        external_webhook_hosts="hooks.example.com", external_webhook_secret="secret",
        external_email_domains="", external_smtp_host=None, external_smtp_sender=None,
        outbound_private_hosts=""))
    with session_scope() as session:
        owner = _user(session, "owner")
        ws = create_workspace(session, owner, name="Scheduled delivery", objective="Test egress", autonomy_level=3)
        session.flush()
        monitor = Monitor(id=new_id("mon"), workspace_id=ws.id, name="Threshold", kind="metric_threshold",
                          config={}, created_by=owner.id)
        alert = Alert(id=new_id("alt"), workspace_id=ws.id, severity="warning", title="Threshold exceeded",
                      message="Too high", data={}, dedupe_key=new_id("dedupe"))
        session.add_all([monitor, alert])
        session.flush()
        owner_id, ws_id, monitor_id, alert_id = owner.id, ws.id, monitor.id, alert.id
    monkeypatch.setattr("analystos.services.monitors.evaluate_monitor", lambda *args, **kwargs: {
        "alert": True, "alert_id": alert_id, "message": "Too high"})
    with session_scope() as session:
        result = schedules._monitors(session.get(User, owner_id), ws_id, "schedule-1", "fire-1", {
            "monitor_ids": [monitor_id], "external_delivery": {
                "channel": "webhook", "destination": "https://hooks.example.com/events"}})
    aid = result["monitors"][monitor_id]["delivery_approval_id"]
    with session_scope() as session:
        approval = session.get(Approval, aid)
        assert approval.status == "pending"
        assert approval.payload["subject"]["alert_id"] == alert_id
