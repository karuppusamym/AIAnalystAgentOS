"""Explicit, approval-bound report and alert delivery.

The approval is the durable delivery record. A request freezes the content, recipient and
destination; an execution claims it before network I/O. Failed attempts can be retried up to
three times while the approval remains valid. Webhook receivers should deduplicate the stable
Idempotency-Key: a transport timeout cannot prove whether the remote side accepted a message.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import smtplib
import ssl
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from analystos.core.config import get_settings
from analystos.core.errors import ApprovalRequired, Conflict, InvalidInput, NotFound, PolicyDenied, UpstreamUnavailable
from analystos.core.ids import stable_hash, utcnow
from analystos.db.base import session_scope
from analystos.db.models import Alert, Approval, Artifact, User, Workspace
from analystos.events.bus import emit
from analystos.governance import approvals
from analystos.governance.audit import audit
from analystos.governance.policy import require_role
from analystos.services.reports import report_file
from analystos.tools import http as outbound

ACTION = "notify_external"
MAX_ATTEMPTS = 3


def _items(value: str) -> set[str]:
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def _destination(channel: str, destination: str) -> None:
    settings = get_settings()
    if len(destination) > 120:
        raise InvalidInput("destination exceeds 120 characters")
    if channel == "webhook":
        parts = urlsplit(destination)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise InvalidInput("webhook destination must be an HTTPS URL without credentials or a fragment")
        if parts.hostname.lower() not in _items(settings.external_webhook_hosts):
            raise PolicyDenied("webhook host is not operator-allowed")
        if not settings.external_webhook_secret:
            raise PolicyDenied("webhook signing secret is not configured")
    elif channel == "email":
        if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", destination):
            raise InvalidInput("destination must be one email address")
        if destination.rsplit("@", 1)[1].lower() not in _items(settings.external_email_domains):
            raise PolicyDenied("email recipient domain is not operator-allowed")
        if not settings.external_smtp_host or not settings.external_smtp_sender:
            raise PolicyDenied("SMTP host and sender are not configured")
    else:
        raise InvalidInput("channel must be email or webhook")


def request(session: Session, user: User, workspace_id: str, *, subject_type: str, subject_id: str,
            channel: str, destination: str, format: str = "pdf") -> Approval:
    require_role(session, user, workspace_id, "editor")
    _destination(channel, destination)
    if subject_type == "report":
        subject = session.get(Artifact, subject_id)
        if subject is None or subject.workspace_id != workspace_id or subject.type != "report" or subject.status != "final":
            raise NotFound("final report not found")
        content, _, _ = report_file(subject, format)
        if len(content) > 10_000_000:
            raise InvalidInput("report exceeds the 10 MB delivery limit")
        if channel == "webhook" and len(content) > 1_000_000:
            raise InvalidInput("webhook report exceeds the 1 MB delivery limit; use email")
        snapshot = {"artifact_id": subject.id, "artifact_hash": subject.content_hash,
                    "format": format, "file_sha256": hashlib.sha256(content).hexdigest(),
                    "title": subject.name}
        run_id = subject.run_id
    elif subject_type == "alert":
        subject = session.get(Alert, subject_id)
        if subject is None or subject.workspace_id != workspace_id:
            raise NotFound("alert not found")
        snapshot = {"alert_id": subject.id, "title": subject.title, "message": subject.message,
                    "severity": subject.severity, "data": subject.data}
        run_id = subject.investigation_run_id
    else:
        raise InvalidInput("subject_type must be report or alert")
    payload = {"subject_type": subject_type, "subject": snapshot, "channel": channel,
               "destination": destination}
    if len(json.dumps(payload, ensure_ascii=False, default=str).encode()) > 1_000_000:
        raise InvalidInput("delivery content exceeds the 1 MB message limit")
    ws = session.get(Workspace, workspace_id)
    return approvals.request_approval(session, workspace_id=workspace_id, run_id=run_id, action=ACTION,
                                      payload=payload, plan_hash=None, policy_version=ws.policy_version,
                                      requested_by=user.id, risk_tier="high", destination=destination,
                                      affected_assets=[subject_id], evidence={"attempts": 0})


def _current_subject(session: Session, approval: Approval) -> bytes | None:
    payload = approval.payload
    subject = payload["subject"]
    if payload["subject_type"] == "report":
        art = session.get(Artifact, subject["artifact_id"])
        if art is None or art.workspace_id != approval.workspace_id or art.status != "final" or art.content_hash != subject["artifact_hash"]:
            raise ApprovalRequired("report changed after approval; request a new delivery")
        content, _, _ = report_file(art, subject["format"])
        if hashlib.sha256(content).hexdigest() != subject["file_sha256"]:
            raise ApprovalRequired("report file changed after approval; request a new delivery")
        return content
    alert = session.get(Alert, subject["alert_id"])
    if alert is None or alert.workspace_id != approval.workspace_id:
        raise ApprovalRequired("alert is no longer available")
    current = {"alert_id": alert.id, "title": alert.title, "message": alert.message,
               "severity": alert.severity, "data": alert.data}
    if stable_hash(current) != stable_hash(subject):
        raise ApprovalRequired("alert changed after approval; request a new delivery")
    return None


def _webhook(approval_id: str, payload: dict, attachment: bytes | None) -> None:
    settings = get_settings()
    body = {"delivery_id": approval_id, "subject_type": payload["subject_type"], "subject": payload["subject"]}
    if attachment is not None:
        if len(attachment) > 1_000_000:
            raise InvalidInput("webhook report exceeds the 1 MB delivery limit; use email")
        body["report_base64"] = base64.b64encode(attachment).decode("ascii")
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    signature = hmac.new(settings.external_webhook_secret.encode(), encoded, hashlib.sha256).hexdigest()
    outbound.call(payload["destination"], method="POST", parameters=body,
                  allowlist=_items(settings.external_webhook_hosts),
                  private_hosts=_items(settings.outbound_private_hosts), timeout=10, max_bytes=16_384,
                  headers={"X-AnalystOS-Signature": f"sha256={signature}", "Idempotency-Key": approval_id})


def _email(approval_id: str, payload: dict, attachment: bytes | None) -> None:
    settings = get_settings()
    host = settings.external_smtp_host
    target = outbound.pin(f"https://{host}:{settings.external_smtp_port}", allowlist={host},
                          private_hosts=_items(settings.outbound_private_hosts))
    msg = EmailMessage()
    msg["From"] = settings.external_smtp_sender
    msg["To"] = payload["destination"]
    msg["Subject"] = str(payload["subject"].get("title", "AnalystOS delivery")).replace("\r", " ").replace("\n", " ")[:300]
    msg["Message-ID"] = f"<{approval_id}@analystos.delivery>"
    msg["X-AnalystOS-Delivery-ID"] = approval_id
    subject = payload["subject"]
    msg.set_content(subject.get("message") or f"Approved report: {subject['title']}\nDelivery ID: {approval_id}")
    if attachment is not None:
        mime = {"pdf": ("application", "pdf"), "xlsx": ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                "html": ("text", "html")}.get(subject["format"], ("application", "octet-stream"))
        msg.add_attachment(attachment, maintype=mime[0], subtype=mime[1], filename=f"report.{subject['format']}")
    try:
        with smtplib.SMTP(str(target.address), target.port, timeout=10) as client:
            client._host = target.hostname  # STARTTLS verifies the original hostname, connection uses vetted IP.
            client.ehlo()
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
            if settings.external_smtp_username:
                if not settings.external_smtp_password:
                    raise PolicyDenied("SMTP password is not configured")
                client.login(settings.external_smtp_username, settings.external_smtp_password)
            client.send_message(msg)
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        raise UpstreamUnavailable(f"SMTP delivery failed: {type(exc).__name__}") from None


def execute(user: User, approval_id: str) -> dict:
    # Commit the claim before network I/O so another API process cannot send concurrently.
    exhausted = False
    with session_scope() as session:
        approval = session.get(Approval, approval_id, with_for_update=True)
        if approval is None or approval.action != ACTION:
            raise NotFound("delivery approval not found")
        require_role(session, user, approval.workspace_id, "editor")
        if approval.status == "executed":
            return {"approval_id": approval_id, "status": "delivered", "attempts": (approval.evidence or {}).get("attempts", 0)}
        if approval.status == "executing":
            started = (approval.evidence or {}).get("attempt_started_at")
            if not started or datetime.fromisoformat(started).astimezone(UTC) > utcnow() - timedelta(minutes=5):
                raise Conflict("delivery attempt is already in progress; retry after five minutes")
            approval.status = "approved"  # crashed or timed-out process; receiver must deduplicate the key.
        approvals.verify_for_execution(session, approval_id, payload=approval.payload, plan_hash=None)
        _destination(approval.payload["channel"], approval.payload["destination"])
        attachment = _current_subject(session, approval)
        attempts = int((approval.evidence or {}).get("attempts", 0))
        if attempts >= MAX_ATTEMPTS:
            approval.status = "invalidated"
            approval.reason = "delivery exhausted three attempts"
            exhausted = True
        else:
            approval.status = "executing"
            approval.evidence = {**(approval.evidence or {}), "attempts": attempts + 1,
                                 "attempt_started_at": utcnow().isoformat()}
            payload = approval.payload
    if exhausted:
        raise Conflict("delivery exhausted three attempts; request a new approval")
    try:
        if payload["channel"] == "webhook":
            _webhook(approval_id, payload, attachment)
        else:
            _email(approval_id, payload, attachment)
    except Exception as exc:
        with session_scope() as session:
            approval = session.get(Approval, approval_id, with_for_update=True)
            if approval.status == "executing":
                exhausted = int((approval.evidence or {}).get("attempts", 0)) >= MAX_ATTEMPTS
                approval.status = "invalidated" if exhausted else "approved"
                if exhausted:
                    approval.reason = "delivery exhausted three attempts"
                approval.evidence = {**(approval.evidence or {}), "last_error": type(exc).__name__}
                audit(f"user:{user.id}", "delivery.failed", workspace_id=approval.workspace_id,
                      target=approval_id, details={"attempt": approval.evidence["attempts"], "error": type(exc).__name__},
                      session=session)
        raise
    with session_scope() as session:
        approval = session.get(Approval, approval_id, with_for_update=True)
        if approval.status != "executing":
            raise Conflict("delivery state changed during execution")
        approval.status = "executed"
        approval.evidence = {**(approval.evidence or {}), "delivered_at": utcnow().isoformat()}
        audit(f"user:{user.id}", "delivery.sent", workspace_id=approval.workspace_id,
              target=approval_id, details={"channel": payload["channel"], "attempt": approval.evidence["attempts"]},
              session=session)
        emit(approval.workspace_id, "delivery.sent", {"approval_id": approval_id, "channel": payload["channel"]}, session=session)
        return {"approval_id": approval_id, "status": "delivered", "attempts": approval.evidence["attempts"]}
