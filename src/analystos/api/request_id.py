"""Request ids (P4-06): every response carries `X-Request-ID`, and every error envelope carries `request_id`.

The id is the caller's `X-Request-ID` (or `X-Correlation-ID`) when it is a short token, else a new `req_...`. It
is set as the logging correlation id before any dependency runs, so the log lines, the audit trail and the error
a client reports all name the same request.
"""
from __future__ import annotations

import re
from typing import Any

from analystos.core.ids import new_id
from analystos.core.logging import correlation_id

HEADER = "X-Request-ID"
_TOKEN = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")


def from_headers(headers: dict[bytes, bytes]) -> str:
    raw = (headers.get(b"x-request-id") or headers.get(b"x-correlation-id") or b"").decode("latin-1").strip()
    return raw if _TOKEN.match(raw) else new_id("req")


def current(request: Any = None) -> str | None:
    state = getattr(request, "state", None)
    return getattr(state, "request_id", None) or correlation_id.get()


class RequestIdMiddleware:
    """Pure ASGI (no BaseHTTPMiddleware): streaming responses pass through untouched."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = from_headers(dict(scope.get("headers") or []))
        scope.setdefault("state", {})["request_id"] = rid
        token = correlation_id.set(rid)

        async def send_with_id(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers") or [] if k.lower() != b"x-request-id"]
                message = {**message, "headers": [*headers, (b"x-request-id", rid.encode("latin-1"))]}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            correlation_id.reset(token)


def envelope(error: dict[str, Any], request: Any = None) -> dict[str, Any]:
    return {"error": {**error, "request_id": current(request)}}
