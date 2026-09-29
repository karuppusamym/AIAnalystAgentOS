"""N-3: approved external email/webhook delivery. A destination is authorized by an approval bound to its
hash; a changed destination needs a new one; every send re-verifies the approval immediately before it;
retries are bounded exponential backoff ending in a dead letter; webhooks are HMAC-signed and pass the
SSRF guard; email goes through an SMTP relay configured from the environment. Fake SMTP and HTTP
transports only: no services."""
from __future__ import annotations

import hashlib
import ipaddress
import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select
from tests.unit.step_fixtures import WS, world  # noqa: F401

from analystos.contracts.events import EVENT_TYPES
from analystos.core.config import get_settings
from analystos.core.errors import Conflict, Forbidden, InvalidInput, PolicyDenied, UpstreamUnavailable
from analystos.core.ids import stable_hash, utcnow
from analystos.db.base import session_scope
from analystos.db.models import (
    Alert,
    Approval,
    Artifact,
    AuditEvent,
    Delivery,
    DeliveryDestination,
    LineageEdge,
    Notification,
    Schedule,
    ScheduleRun,
    User,
    WorkspaceMember,
)
from analystos.governance.approvals import decide
from analystos.services import deliveries as svc

SECRET = "whsec-test-only-0123"  # a test value, set in the environment by the fixture


def _public(host: str, port: int) -> list:
    return [ipaddress.ip_address("93.184.216.34")]


class Recorder:
    """An httpx MockTransport that records every request and answers with `status`."""

    def __init__(self, status: int = 200):
        self.status, self.requests = status, []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={"ok": self.status < 400})


class FakeSMTP:
    sent: list = []
    calls: list = []

    def __init__(self, host: str, port: int, timeout: float):
        FakeSMTP.calls.append(("connect", host, port))

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *exc):  # noqa: ANN002
        return False

    def starttls(self, context=None):  # noqa: ANN001
        FakeSMTP.calls.append(("starttls",))

    def login(self, user: str, password: str) -> None:
        FakeSMTP.calls.append(("login", user, password))

    def send_message(self, msg):  # noqa: ANN001
        FakeSMTP.sent.append(msg)
        return {}


class FailingTransport:
    def __init__(self, exc: Exception):
        self.exc, self.calls = exc, 0

    def send(self, config, message):  # noqa: ANN001
        self.calls += 1
        raise self.exc


@pytest.fixture
def env(world, sqlite_db, monkeypatch, tmp_path):  # noqa: F811
    from analystos.db import models

    models.Base.metadata.create_all(sqlite_db.kw["bind"], tables=[models.Base.metadata.tables[t] for t in
                                                                  ("delivery_destination", "delivery")])
    from datetime import datetime

    from analystos.governance import approvals

    monkeypatch.setattr(approvals, "utcnow", lambda: datetime.utcnow())  # SQLite stores expiry without its zone
    monkeypatch.setenv("ANALYSTOS_WEBHOOK_N3", SECRET)
    monkeypatch.setenv("N3_SMTP_PASSWORD", "smtp-test-only")
    s = get_settings()
    for key, value in {"smtp_host": "", "smtp_from": "", "smtp_username": "", "smtp_password_ref": "",
                       "delivery_email_allowed_domains": "", "outbound_private_hosts": "", "delivery_max_attempts": 3,
                       "delivery_backoff_seconds": 60.0, "delivery_backoff_max_seconds": 3600.0,
                       "artifact_dir": str(tmp_path)}.items():
        monkeypatch.setattr(s, key, value)
    FakeSMTP.sent, FakeSMTP.calls = [], []
    with session_scope() as sess:
        sess.add(Alert(id="alr_1", workspace_id=WS, monitor_id="mon_1", severity="critical", title="Late orders up",
                       message="late orders rose 40% week on week", data={}, dedupe_key="k1", status="open"))
    return tmp_path


def _user(sess, uid: str) -> User:  # noqa: ANN001
    return sess.get(User, uid)


def _webhook(url: str = "https://hooks.example.com/analystos", kinds: list[str] | None = None) -> str:
    with session_scope() as s:
        return svc.create_destination(s, _user(s, "usr_owner"), WS, name="ops hook", kind="webhook",
                                      config={"url": url, "secret_ref": "env:ANALYSTOS_WEBHOOK_N3"}, content_kinds=kinds).id


def _approve(dest_id: str) -> None:
    with session_scope() as s:
        dest = s.get(DeliveryDestination, dest_id)
        approval = decide(s, dest.approval_id, _user(s, "usr_approver"), approve=True, reason="ok")
        svc.apply_decision(s, approval)


def _authorized_webhook(**kw) -> str:  # noqa: ANN003
    dest_id = _webhook(**kw)
    _approve(dest_id)
    return dest_id


def _enqueue(dest_id: str, subject_type: str = "alert", subject_id: str = "alr_1") -> str:
    with session_scope() as s:
        return svc.enqueue(s, WS, destination_ids=[dest_id], subject_type=subject_type, subject_id=subject_id,
                           origin={"user": "usr_owner"})[0].id


def _delivery(delivery_id: str) -> Delivery:
    with session_scope() as s:
        d = s.get(Delivery, delivery_id)
        s.expunge(d)
    return d


# ------------------------------------------------------------------------------ validation and SSRF
@pytest.mark.parametrize("url", [
    "http://hooks.example.com/x", "https://localhost/x", "https://svc.internal/x", "https://printer.local/x",
    "https://10.0.0.5/x", "https://169.254.169.254/latest/meta-data", "https://[::1]/x", "https://100.64.0.1/x",
    "https://user:pw@hooks.example.com/x", "ftp://hooks.example.com/x", "https://intranet/x",
])
def test_webhook_urls_that_could_reach_the_inside_are_refused(env, url):
    with pytest.raises((InvalidInput, PolicyDenied)):
        svc.check_webhook_url(url)


def test_an_operator_listed_private_host_is_allowed(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "outbound_private_hosts", "hooks.corp.internal,10.20.0.0/16")
    assert svc.check_webhook_url("https://hooks.corp.internal/x")
    assert svc.check_webhook_url("http://hooks.corp.internal/x")  # http only for a listed host
    assert svc.check_webhook_url("https://10.20.1.2/x")


def test_webhooks_need_a_secret_reference_never_a_value(env):
    with pytest.raises(InvalidInput, match="secret_ref"):
        svc.normalize_config("webhook", {"url": "https://hooks.example.com/x", "secret_ref": SECRET})
    for other in ("env:ANALYSTOS_DATABASE_URL", "env:SOURCE_PASSWORD", "file:/etc/shadow"):  # only the webhook namespace
        with pytest.raises(InvalidInput, match="ANALYSTOS_WEBHOOK_"):
            svc.normalize_config("webhook", {"url": "https://hooks.example.com/x", "secret_ref": other})
    with pytest.raises(InvalidInput, match="unknown"):
        svc.normalize_config("webhook", {"url": "https://hooks.example.com/x", "secret_ref": "env:X", "headers": {}})


def test_email_recipients_are_validated_and_bounded_by_operator_domains(env, monkeypatch):
    cfg = svc.normalize_config("email", {"recipients": "Ops@Example.com; cfo@example.com"})
    assert cfg == {"recipients": ["cfo@example.com", "ops@example.com"], "attach_formats": ["pdf"]}
    with pytest.raises(InvalidInput, match="not an email"):
        svc.normalize_config("email", {"recipients": ["nope"]})
    monkeypatch.setattr(get_settings(), "delivery_email_allowed_domains", "corp.example")
    with pytest.raises(PolicyDenied, match="allowed email domains"):
        svc.normalize_config("email", {"recipients": ["x@gmail.com"]})


# ------------------------------------------------------------------------------ authorization bound to the hash
def test_a_destination_is_authorized_by_an_approval_bound_to_its_hash(env):
    dest_id = _webhook()
    with session_scope() as s:
        dest = s.get(DeliveryDestination, dest_id)
        approval = s.get(Approval, dest.approval_id)
        assert (dest.status, approval.action, approval.risk_tier) == ("pending", svc.APPROVAL_ACTION, "high")
        assert approval.payload_hash == stable_hash(svc.authorization_payload(dest))
        assert approval.payload["destination_hash"] == dest.destination_hash
        with pytest.raises(Forbidden, match="separation of duties"):  # never self-authorized
            decide(s, approval.id, _user(s, "usr_owner"), approve=True)
    _approve(dest_id)
    with session_scope() as s:
        dest = s.get(DeliveryDestination, dest_id)
        assert dest.status == "authorized" and dest.authorized_hash == dest.destination_hash
        assert svc._authorized(dest)
        expires = s.get(Approval, dest.approval_id).expires_at
        assert expires.replace(tzinfo=None) > (utcnow() + timedelta(days=80)).replace(tzinfo=None)


def test_a_changed_destination_needs_reapproval_and_refuses_what_was_queued(env):
    dest_id = _authorized_webhook()
    queued = _enqueue(dest_id)
    with session_scope() as s:
        old = s.get(DeliveryDestination, dest_id).approval_id
        dest = svc.update_destination(s, _user(s, "usr_owner"), dest_id, name="renamed")
        assert dest.status == "authorized" and dest.approval_id == old  # a rename is not a new target
        dest = svc.update_destination(s, _user(s, "usr_owner"), dest_id,
                                      config={"url": "https://other.example.com/in", "secret_ref": "env:ANALYSTOS_WEBHOOK_N3"})
        assert dest.status == "pending" and dest.approval_id != old and dest.authorized_hash is None
        assert s.get(Approval, old).status == "invalidated"
    assert _delivery(queued).status == "refused"
    assert svc.attempt(queued, transports={"webhook": Recorder().transport}) is None  # refused is terminal


def test_rejection_leaves_the_destination_unable_to_send(env):
    dest_id = _webhook()
    with session_scope() as s:
        dest = s.get(DeliveryDestination, dest_id)
        svc.apply_decision(s, decide(s, dest.approval_id, _user(s, "usr_approver"), approve=False))
        assert s.get(DeliveryDestination, dest_id).status == "rejected"
    d = _delivery(_enqueue(dest_id))
    assert d.status == "refused" and "not authorized" in d.last_error


# ------------------------------------------------------------------------------ sending
def test_a_signed_webhook_is_sent_once_with_its_idempotency_key(env):
    dest_id = _authorized_webhook()
    first, again = _enqueue(dest_id), _enqueue(dest_id)
    assert first == again  # the same alert to the same destination is queued once
    rec = Recorder()
    transports = {"webhook": svc.WebhookTransport(resolver=_public, http_transport=rec.transport, clock=lambda: 1_790_000_000)}
    assert svc.process_due(transports=transports) == {"delivered": 1}
    (req,) = rec.requests
    d = _delivery(first)
    assert d.status == "delivered" and d.attempts == 1 and d.response["status_code"] == 200
    assert req.headers["Idempotency-Key"] == d.idempotency_key and req.headers["host"] == "hooks.example.com"
    assert str(req.url).startswith("https://93.184.216.34")  # pinned to the vetted address
    body = req.content
    assert svc.verify_signature(SECRET, req.headers[svc.TIMESTAMP_HEADER], body, req.headers[svc.SIGNATURE_HEADER],
                                now=1_790_000_010)
    assert not svc.verify_signature("wrong", req.headers[svc.TIMESTAMP_HEADER], body, req.headers[svc.SIGNATURE_HEADER],
                                    now=1_790_000_010)
    payload = json.loads(body)
    assert payload["alert"]["title"] == "Late orders up" and payload["idempotency_key"] == d.idempotency_key
    assert SECRET.encode() not in body
    assert svc.process_due(transports=transports) == {}  # nothing due: never sent twice
    with session_scope() as s:
        sent = s.scalar(select(AuditEvent).where(AuditEvent.action == "delivery.sent"))
        assert sent.details["approval_id"] == s.get(DeliveryDestination, dest_id).approval_id
        assert s.scalar(select(LineageEdge).where(LineageEdge.relation == "delivered_via", LineageEdge.from_id == "alr_1"))


def test_the_approval_is_reverified_immediately_before_sending(env):
    dest_id = _authorized_webhook()
    queued = _enqueue(dest_id)
    with session_scope() as s:  # an edit that bypassed the API: the payload no longer hashes to what was approved
        dest = s.get(DeliveryDestination, dest_id)
        dest.config = {**dest.config, "url": "https://attacker.example.net/x"}
    rec = Recorder()
    assert svc.attempt(queued, transports={"webhook": svc.WebhookTransport(resolver=_public, http_transport=rec.transport)}) \
        == "refused"
    assert rec.requests == []
    with session_scope() as s:
        assert s.get(Approval, s.get(DeliveryDestination, dest_id).approval_id).status == "invalidated"
        assert s.scalar(select(Notification).where(Notification.kind == "delivery"))


def test_an_approver_who_lost_the_role_stops_the_send(env):
    dest_id = _authorized_webhook()
    queued = _enqueue(dest_id)
    with session_scope() as s:
        s.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == WS,
                                               WorkspaceMember.user_id == "usr_approver")).role = "viewer"
    rec = Recorder()
    assert svc.attempt(queued, transports={"webhook": svc.WebhookTransport(resolver=_public, http_transport=rec.transport)}) \
        == "refused"
    assert rec.requests == [] and "approval rights" in _delivery(queued).last_error


def test_a_webhook_host_that_resolves_inside_is_refused_at_send(env):
    queued = _enqueue(_authorized_webhook())
    rec = Recorder()
    inside = svc.WebhookTransport(resolver=lambda h, p: [ipaddress.ip_address("10.0.0.7")], http_transport=rec.transport)
    assert svc.attempt(queued, transports={"webhook": inside}) == "refused"
    assert rec.requests == [] and "non-public" in _delivery(queued).last_error


def test_retries_back_off_exponentially_then_dead_letter_and_redrive(env):
    queued = _enqueue(_authorized_webhook())
    failing = FailingTransport(UpstreamUnavailable("hooks.example.com unavailable: ConnectError"))
    t0 = utcnow()
    assert svc.attempt(queued, transports={"webhook": failing}, now=t0) == "retrying"
    d = _delivery(queued)
    assert d.attempts == 1 and abs((d.next_attempt_at.replace(tzinfo=None) - t0.replace(tzinfo=None)).total_seconds() - 60) < 1
    assert svc.attempt(queued, transports={"webhook": failing}, now=t0 + timedelta(seconds=30)) is None  # not due
    assert svc.attempt(queued, transports={"webhook": failing}, now=t0 + timedelta(seconds=61)) == "retrying"
    d = _delivery(queued)
    assert abs((d.next_attempt_at.replace(tzinfo=None) - t0.replace(tzinfo=None)).total_seconds() - 181) < 1  # 61 + 120
    assert svc.attempt(queued, transports={"webhook": failing}, now=t0 + timedelta(seconds=200)) == "dead_letter"
    assert failing.calls == 3 and _delivery(queued).attempts == 3
    assert svc.backoff(10) == timedelta(seconds=3600)  # capped
    with session_scope() as s:
        with pytest.raises(Forbidden):
            svc.redrive(s, _user(s, "usr_viewer"), queued)
        assert svc.redrive(s, _user(s, "usr_owner"), queued).status == "queued"
    rec = Recorder()
    assert svc.attempt(queued, transports={"webhook": svc.WebhookTransport(resolver=_public, http_transport=rec.transport)}) \
        == "delivered"


def test_a_permanent_http_rejection_dead_letters_at_once(env):
    queued = _enqueue(_authorized_webhook())
    rec = Recorder(status=400)
    assert svc.attempt(queued, transports={"webhook": svc.WebhookTransport(resolver=_public, http_transport=rec.transport)}) \
        == "dead_letter"
    d = _delivery(queued)
    assert d.attempts == 1 and "400" in d.last_error


def test_a_throttled_webhook_is_retried(env):
    queued = _enqueue(_authorized_webhook())
    rec = Recorder(status=429)
    assert svc.attempt(queued, transports={"webhook": svc.WebhookTransport(resolver=_public, http_transport=rec.transport)}) \
        == "retrying"


def test_a_crashed_sender_lease_is_picked_up_again(env):
    queued = _enqueue(_authorized_webhook())
    with session_scope() as s:
        d = s.get(Delivery, queued)
        d.status, d.locked_until, d.attempts = "sending", utcnow() - timedelta(minutes=1), 1
    rec = Recorder()
    transports = {"webhook": svc.WebhookTransport(resolver=_public, http_transport=rec.transport)}
    assert svc.process_due(transports=transports) == {"delivered": 1}
    assert _delivery(queued).attempts == 2 and len(rec.requests) == 1


# ------------------------------------------------------------------------------ email
def _report(tmp_path) -> str:  # noqa: ANN001
    pdf = b"%PDF-1.4 test report"
    path = tmp_path / "r.pdf"
    path.write_bytes(pdf)
    content = {"kind": "executive", "title": "Weekly orders", "report_data_hash": "d" * 64, "insights": 3, "metrics": 2,
               "alerts": 1, "files": {"pdf": {"path": str(path), "sha256": hashlib.sha256(pdf).hexdigest(),
                                              "bytes": len(pdf), "mime": "application/pdf", "ext": "pdf"}}}
    with session_scope() as s:
        s.add(Artifact(id="art_rep", workspace_id=WS, run_id="run_1", type="report", name="executive report", version=1,
                       status="final", content=content, content_hash=stable_hash(content)))
    return "art_rep"


def _email_destination() -> str:
    with session_scope() as s:
        dest_id = svc.create_destination(s, _user(s, "usr_owner"), WS, name="leadership", kind="email",
                                         config={"recipients": ["cfo@example.com", "ops@example.com"]},
                                         content_kinds=["report"]).id
    _approve(dest_id)
    return dest_id


def test_a_report_snapshot_is_emailed_through_the_configured_relay(env, monkeypatch):
    s = get_settings()
    for key, value in {"smtp_host": "smtp.example.com", "smtp_from": "analystos@example.com", "smtp_username": "relay",
                       "smtp_password_ref": "env:N3_SMTP_PASSWORD"}.items():
        monkeypatch.setattr(s, key, value)
    queued = _enqueue(_email_destination(), "report", _report(env))
    assert svc.attempt(queued, transports={"email": svc.SmtpTransport(factory=FakeSMTP)}) == "delivered"
    (msg,) = FakeSMTP.sent
    d = _delivery(queued)
    assert msg["To"] == "cfo@example.com, ops@example.com" and msg["Message-ID"] == f"<{d.idempotency_key}@analystos>"
    assert msg["Subject"] == "Report: Weekly orders"
    (att,) = list(msg.iter_attachments())
    assert att.get_content_type() == "application/pdf" and att.get_content() == b"%PDF-1.4 test report"
    assert ("starttls",) in FakeSMTP.calls and ("login", "relay", "smtp-test-only") in FakeSMTP.calls
    assert d.response["accepted"] == 2


def test_email_without_a_relay_is_a_dead_letter_not_a_retry_loop(env):
    queued = _enqueue(_email_destination(), "report", _report(env))
    assert svc.attempt(queued, transports={"email": svc.SmtpTransport(factory=FakeSMTP)}) == "dead_letter"
    assert "not configured" in _delivery(queued).last_error and FakeSMTP.sent == []


def test_a_destination_only_takes_the_content_it_was_authorized_for(env):
    d = _delivery(_enqueue(_email_destination(), "alert", "alr_1"))
    assert d.status == "refused" and "does not accept alerts" in d.last_error


def test_a_draft_report_is_never_delivered(env):
    _report(env)
    with session_scope() as s:
        s.get(Artifact, "art_rep").status = "draft"
    with session_scope() as s, pytest.raises(InvalidInput, match="final"):
        svc.enqueue(s, WS, destination_ids=[_email_destination()], subject_type="report", subject_id="art_rep")


# ------------------------------------------------------------------------------ schedules, monitors, revoke
def test_a_scheduled_report_is_queued_for_its_destinations(env):
    from analystos.services import schedules

    dest_id = _email_destination()
    report = _report(env)
    with session_scope() as s:
        with pytest.raises(InvalidInput, match="not found"):
            schedules._check_definition(s, WS, {"deliver_to": ["dst_missing"]})
        schedules._check_definition(s, WS, {"deliver_to": [dest_id]})
        s.add(Schedule(id="sch_1", workspace_id=WS, name="weekly", kind="report", cron="0 8 * * 1", timezone="UTC",
                       config={"deliver_to": [dest_id]}, owner_id="usr_owner", enabled=True, revision=1, pins={},
                       pin_status={}))
        s.add(ScheduleRun(id="srn_1", schedule_id="sch_1", workspace_id=WS, fire_key="k", scheduled_for=utcnow(),
                          status="running", result={}))
    schedules._finish("srn_1", "succeeded", {"report_artifact_id": report})
    with session_scope() as s:
        (d,) = s.scalars(select(Delivery))
        assert (d.subject_type, d.subject_id, d.status, d.origin["schedule_run_id"]) == ("report", report, "queued", "srn_1")
        assert s.get(ScheduleRun, "srn_1").result["deliveries"] == [d.id]


def test_monitor_destinations_do_not_change_what_the_monitor_watches(env):
    from analystos.services import monitors

    dest_id = _authorized_webhook()
    base = {"metric": "late_orders", "grain": "week"}
    assert monitors.condition_key(WS, "metric_drift", base) == \
        monitors.condition_key(WS, "metric_drift", {**base, "deliver_to": [dest_id]})
    with session_scope() as s:
        monitors.validate_delivery(s, WS, {"deliver_to": [dest_id]})
        from analystos.services.notifications import deliver

        assert len(deliver(s, WS, [dest_id], subject_type="alert", subject_id="alr_1", origin={"monitor_id": "mon_1"})) == 1


def test_revoking_refuses_queued_deliveries_and_blocks_new_ones(env):
    dest_id = _authorized_webhook()
    queued = _enqueue(dest_id)
    with session_scope() as s:
        svc.revoke_destination(s, _user(s, "usr_owner"), dest_id, "vendor offboarded")
        with pytest.raises(Conflict):
            svc.update_destination(s, _user(s, "usr_owner"), dest_id, name="again")
        with pytest.raises(InvalidInput, match="revoked"):
            svc.check_targets(s, WS, [dest_id], "alert")
    assert _delivery(queued).status == "refused"


def test_a_tightened_operator_policy_applies_to_authorized_destinations(env, monkeypatch):
    queued = _enqueue(_email_destination(), "report", _report(env))
    monkeypatch.setattr(get_settings(), "delivery_email_allowed_domains", "corp.example")
    assert svc.attempt(queued, transports={"email": svc.SmtpTransport(factory=FakeSMTP)}) == "refused"
    assert "allowed email domains" in _delivery(queued).last_error and FakeSMTP.sent == []


def test_a_lapsed_authorization_is_shown_and_can_be_requested_again(env):
    dest_id = _authorized_webhook()
    queued = _enqueue(dest_id)
    with session_scope() as s:  # a policy change after the approval: verify_for_execution invalidates it
        from analystos.db.models import Workspace

        s.get(Workspace, WS).policy_version = 2
    assert svc.attempt(queued, transports={"webhook": Recorder().transport}) == "refused"
    with session_scope() as s:
        dest = s.get(DeliveryDestination, dest_id)
        assert dest.status == "lapsed" and not svc._authorized(dest)
        old = dest.approval_id
        with pytest.raises(Forbidden):
            svc.reauthorize_destination(s, _user(s, "usr_viewer"), dest_id)
        dest = svc.reauthorize_destination(s, _user(s, "usr_owner"), dest_id)
        assert dest.status == "pending" and dest.approval_id != old
        assert s.get(Approval, dest.approval_id).policy_version == 2


def test_smtp_credentials_never_travel_without_tls(env, monkeypatch):
    s = get_settings()
    for key, value in {"smtp_host": "smtp.example.com", "smtp_from": "analystos@example.com", "smtp_username": "relay",
                       "smtp_password_ref": "env:N3_SMTP_PASSWORD", "smtp_starttls": False}.items():
        monkeypatch.setattr(s, key, value)
    queued = _enqueue(_email_destination(), "report", _report(env))
    assert svc.attempt(queued, transports={"email": svc.SmtpTransport(factory=FakeSMTP)}) == "refused"
    assert FakeSMTP.calls == [] and "without TLS" in _delivery(queued).last_error


def test_a_short_signing_secret_is_refused(env, monkeypatch):
    monkeypatch.setenv("ANALYSTOS_WEBHOOK_N3", "short")
    queued = _enqueue(_authorized_webhook())
    rec = Recorder()
    assert svc.attempt(queued, transports={"webhook": svc.WebhookTransport(resolver=_public, http_transport=rec.transport)}) \
        == "dead_letter"
    assert rec.requests == [] and "short" not in _delivery(queued).last_error


def test_the_api_shows_whether_a_secret_is_set_not_which(env):
    from analystos.api.routers.deliveries import destination_out

    dest_id = _authorized_webhook()
    with session_scope() as s:
        out = destination_out(s, s.get(DeliveryDestination, dest_id))
    assert out["has_secret"] and "secret_ref" not in out["config"] and "ANALYSTOS_WEBHOOK_N3" not in json.dumps(out)
    assert out["authorized"] and out["approval"]["status"] == "approved"


def test_event_types_are_declared():
    assert {"delivery.destination_requested", "delivery.destination_authorized", "delivery.queued", "delivery.sent",
            "delivery.retrying", "delivery.dead_lettered", "delivery.refused"} <= EVENT_TYPES


def test_the_api_routes_exist():
    from tests.unit.test_route_workspace_binding import _api_routes

    paths = {path for path, _ in _api_routes()}
    assert {"/api/workspaces/{workspace_id}/delivery-destinations", "/api/delivery-destinations/{destination_id}",
            "/api/delivery-destinations/{destination_id}/revoke", "/api/workspaces/{workspace_id}/deliveries",
            "/api/deliveries/{delivery_id}/redrive"} <= paths
