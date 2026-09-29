"""Email delivery uses a vetted address and STARTTLS before authentication/send."""
import base64
import hashlib
import hmac
import json
from types import SimpleNamespace

from analystos.services import external_delivery


def test_email_uses_pinned_address_and_starttls(monkeypatch):
    settings = SimpleNamespace(external_smtp_host="smtp.example.com", external_smtp_port=587,
                               external_smtp_sender="reports@example.com", external_smtp_username="sender",
                               external_smtp_password="password", outbound_private_hosts="")
    monkeypatch.setattr(external_delivery, "get_settings", lambda: settings)
    monkeypatch.setattr(external_delivery.outbound, "pin", lambda *args, **kwargs: SimpleNamespace(
        address="93.184.215.14", hostname="smtp.example.com", port=587))
    calls = []

    class SMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def ehlo(self):
            calls.append(("ehlo",))

        def starttls(self, *, context):
            calls.append(("starttls", self._host, context.check_hostname))

        def login(self, username, password):
            calls.append(("login", username, password))

        def send_message(self, message):
            calls.append(("send", message["Message-ID"], message["To"], len(message.get_payload())))

    monkeypatch.setattr(external_delivery.smtplib, "SMTP", SMTP)
    external_delivery._email("apr_fixed", {"destination": "analyst@example.com",
                                             "subject": {"title": "Report", "format": "pdf"}}, b"%PDF")
    assert calls[0] == ("connect", "93.184.215.14", 587, 10)
    assert calls[2] == ("starttls", "smtp.example.com", True)
    assert calls[4] == ("login", "sender", "password")
    assert calls[5][1:3] == ("<apr_fixed@analystos.delivery>", "analyst@example.com")


def test_webhook_signature_covers_report_bytes_and_idempotency_key(monkeypatch):
    monkeypatch.setattr(external_delivery, "get_settings", lambda: SimpleNamespace(
        external_webhook_hosts="hooks.example.com", external_webhook_secret="secret",
        outbound_private_hosts=""))
    sent = []
    monkeypatch.setattr(external_delivery.outbound, "call", lambda *args, **kwargs: sent.append((args, kwargs)))
    external_delivery._webhook("apr_fixed", {"subject_type": "report", "subject": {"title": "Report"},
                                             "destination": "https://hooks.example.com/events"}, b"%PDF")
    args, kwargs = sent[0]
    assert args == ("https://hooks.example.com/events",)
    body = kwargs["parameters"]
    assert base64.b64decode(body["report_base64"]) == b"%PDF"
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert kwargs["headers"]["X-AnalystOS-Signature"] == "sha256=" + hmac.new(
        b"secret", canonical, hashlib.sha256).hexdigest()
    assert kwargs["headers"]["Idempotency-Key"] == "apr_fixed"
