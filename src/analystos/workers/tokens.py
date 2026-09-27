"""Scoped capability tokens for isolated workers (ADR-0022 decision 3, P7-06).

A token is `<claims>.<mac>`: base64url JSON claims and an HMAC-SHA256 over them with a platform
secret that only the control plane and the artifact store hold. It is short-lived and bound to one
task: the artifacts it may read, the outputs it may write, its verbs and model purposes. The worker
cannot mint or widen one; it only presents it. Tokens are domain-separated from session JWTs, so
neither is accepted in place of the other.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from analystos.core.errors import Forbidden, Unauthenticated

VERSION = "aos-wt1"
VERBS = frozenset({"read", "write", "model"})
_DOMAIN = b"analystos.worker-token.v1"


def secret_from_settings(settings: Any) -> bytes:
    """`worker_token_secret` when set, else a key derived from the JWT secret (never the JWT secret itself)."""
    explicit = getattr(settings, "worker_token_secret", None)
    if explicit:
        return explicit.encode()
    return hmac.new(settings.jwt_secret.encode(), _DOMAIN, hashlib.sha256).digest()


@dataclass(frozen=True)
class TokenClaims:
    task_id: str
    workspace_id: str
    idempotency_key: str
    reads: frozenset[str]
    writes: frozenset[str]
    verbs: frozenset[str]
    purposes: frozenset[str] = frozenset()
    run_id: str | None = None
    max_output_bytes: int = 0
    max_model_calls: int = 0
    issued_at: int = 0
    expires_at: int = 0
    token_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def require(self, verb: str) -> None:
        if verb not in self.verbs:
            raise Forbidden(f"this task token does not grant '{verb}'", details={"verb": verb, "task_id": self.task_id})

    def require_read(self, artifact_id: str) -> None:
        self.require("read")
        if artifact_id not in self.reads:
            raise Forbidden("this task token does not grant reading that artifact",
                            details={"artifact_id": artifact_id, "task_id": self.task_id})

    def require_write(self, task_id: str, name: str) -> None:
        self.require("write")
        if task_id != self.task_id:
            raise Forbidden("this token belongs to another task", details={"task_id": task_id})
        if name not in self.writes:
            raise Forbidden(f"this task token does not grant writing output '{name}'",
                            details={"output": name, "task_id": self.task_id})

    def require_purpose(self, purpose: str) -> None:
        self.require("model")
        if purpose not in self.purposes:
            raise Forbidden(f"this task token does not grant model purpose '{purpose}'",
                            details={"purpose": purpose, "task_id": self.task_id})


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(secret: bytes, payload: str) -> str:
    return _b64(hmac.new(secret, f"{VERSION}.{payload}".encode(), hashlib.sha256).digest())


def issue(secret: bytes, *, task_id: str, workspace_id: str, idempotency_key: str, reads: list[str] | tuple[str, ...] = (),
          writes: list[str] | tuple[str, ...] = (), verbs: list[str] | tuple[str, ...] = ("read", "write"),
          purposes: list[str] | tuple[str, ...] = (), run_id: str | None = None, max_output_bytes: int = 0,
          max_model_calls: int = 0, ttl_seconds: int = 900, now: float | None = None) -> str:
    unknown = set(verbs) - VERBS
    if unknown:
        raise ValueError(f"unknown verbs {sorted(unknown)}")
    issued = int(now if now is not None else time.time())
    claims = {"v": VERSION, "tid": task_id, "ws": workspace_id, "idem": idempotency_key, "run": run_id,
              "art": sorted(set(reads)), "out": sorted(set(writes)), "verbs": sorted(set(verbs)),
              "pur": sorted(set(purposes)), "mob": int(max_output_bytes), "mmc": int(max_model_calls),
              "iat": issued, "exp": issued + int(ttl_seconds), "jti": secrets.token_hex(8)}
    payload = _b64(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    return f"{payload}.{_mac(secret, payload)}"


def verify(secret: bytes, token: str, *, now: float | None = None) -> TokenClaims:
    """Claims of a valid, unexpired token; Unauthenticated for anything else (the reason is not echoed)."""
    try:
        payload, mac = token.split(".", 1)
    except (AttributeError, ValueError):
        raise Unauthenticated("not a worker task token") from None
    if not hmac.compare_digest(mac, _mac(secret, payload)):
        raise Unauthenticated("worker task token signature is invalid")
    try:
        c = json.loads(_unb64(payload))
    except ValueError:
        raise Unauthenticated("worker task token is malformed") from None
    if c.get("v") != VERSION:
        raise Unauthenticated("worker task token version is not supported")
    if int(c["exp"]) < (now if now is not None else time.time()):
        raise Unauthenticated("worker task token has expired", details={"task_id": c.get("tid")})
    return TokenClaims(task_id=c["tid"], workspace_id=c["ws"], idempotency_key=c["idem"], run_id=c.get("run"),
                       reads=frozenset(c.get("art") or ()), writes=frozenset(c.get("out") or ()),
                       verbs=frozenset(c.get("verbs") or ()), purposes=frozenset(c.get("pur") or ()),
                       max_output_bytes=int(c.get("mob") or 0), max_model_calls=int(c.get("mmc") or 0),
                       issued_at=int(c["iat"]), expires_at=int(c["exp"]), token_id=str(c.get("jti") or ""))
