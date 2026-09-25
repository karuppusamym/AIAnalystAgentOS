"""OIDC single sign-on (SEC-001..003): authorization-code flow with PKCE, ID-token validation against
the IdP's JWKS, and IdP groups mapped to workspace roles and ABAC attributes.

The IdP authenticates; AnalystOS still authorizes. What an ID token can change is bounded by the
mapping file (config/oidc.yaml): which workspaces a group grants and at which role, whether a group
makes a platform admin, and which claims become user attributes (the ABAC inputs `resolve_scope`
reads). After the callback the user holds an ordinary AnalystOS token, so every authorization path
is the same for password and SSO users.

Login transaction state (state, nonce, PKCE verifier, return path) lives in a short-lived signed
HttpOnly cookie scoped to the OIDC paths: the verifier never appears in a URL and no server-side
table is needed. The client secret, when the IdP needs one, comes from the environment only.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.config import get_settings
from analystos.core.errors import InvalidInput, Unauthenticated, UpstreamUnavailable
from analystos.core.ids import new_id, utcnow
from analystos.core.logging import get_logger
from analystos.db.models import User, UserIdentity, Workspace, WorkspaceMember
from analystos.governance.audit import audit
from analystos.security.auth import ROLE_RANK

log = get_logger(__name__)

ALLOWED_ALGS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")  # never none/HS*
TX_COOKIE = "aos_oidc_tx"
TX_AUDIENCE = "analystos-oidc-tx"
TX_TTL_SECONDS = 600
CLOCK_SKEW_SECONDS = 60
UNUSABLE_PASSWORD = "!sso"  # not a bcrypt hash: password login can never succeed for an SSO-created user


# ----------------------------------------------------------------------------- configuration
@dataclass
class RoleGrant:
    group: str
    workspace: str  # workspace id or name
    role: str


@dataclass
class Mapping:
    platform_admin_groups: list[str] = field(default_factory=list)
    workspace_roles: list[RoleGrant] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)  # user attribute -> claim name
    require_group: bool = False  # a user none of whose groups is mapped cannot sign in


def load_mapping(path: Path | None = None) -> Mapping:
    path = path or get_settings().oidc_mapping_file
    data = yaml.safe_load(path.read_text()) if path and Path(path).exists() else {}
    data = data or {}
    grants = []
    for g in data.get("workspace_roles") or []:
        if g.get("role") not in ROLE_RANK:
            raise InvalidInput(f"oidc mapping: unknown role {g.get('role')!r} for group {g.get('group')!r}")
        grants.append(RoleGrant(group=str(g["group"]), workspace=str(g["workspace"]), role=str(g["role"])))
    return Mapping(platform_admin_groups=[str(x) for x in data.get("platform_admin_groups") or []], workspace_roles=grants,
                   attributes={str(k): str(v) for k, v in (data.get("attributes") or {}).items()},
                   require_group=bool(data.get("require_group", False)))


def enabled() -> bool:
    s = get_settings()
    return bool(s.oidc_issuer and s.oidc_client_id)


# ----------------------------------------------------------------------------- PKCE and transaction cookie
def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge), RFC 7636."""
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def seal_transaction(tx: dict[str, Any]) -> str:
    now = int(time.time())
    return jwt.encode({**tx, "aud": TX_AUDIENCE, "iat": now, "exp": now + TX_TTL_SECONDS}, get_settings().jwt_secret,
                      algorithm="HS256")


def open_transaction(cookie: str | None, state: str | None) -> dict[str, Any]:
    if not cookie or not state:
        raise Unauthenticated("sign-in session missing or expired; start again")
    try:
        tx = jwt.decode(cookie, get_settings().jwt_secret, algorithms=["HS256"], audience=TX_AUDIENCE)
    except jwt.PyJWTError as exc:
        raise Unauthenticated(f"sign-in session invalid: {exc}") from exc
    if not secrets.compare_digest(str(tx.get("state", "")), state):
        raise Unauthenticated("sign-in state mismatch")
    return tx


def safe_return_path(value: str | None) -> str:
    """Only same-site relative paths: the callback must never become an open redirect."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value[:500]


# ----------------------------------------------------------------------------- IdP client
_transport: httpx.BaseTransport | None = None


def set_http_transport(transport: httpx.BaseTransport | None) -> None:
    """Tests and a local fake IdP inject the transport; production uses the network."""
    global _transport
    _transport = transport
    provider.cache_clear()


class OidcProvider:
    def __init__(self, *, issuer: str, client_id: str, client_secret: str | None, redirect_uri: str, scopes: str,
                 discovery_url: str | None = None, jwks_file: Path | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self.discovery_url = discovery_url or f"{self.issuer}/.well-known/openid-configuration"
        self.jwks_file = jwks_file
        self._http = httpx.Client(timeout=10, transport=transport)
        self._meta: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None
        self._jwks_at = 0.0

    def _get(self, url: str) -> dict[str, Any]:
        try:
            resp = self._http.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"identity provider unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise UpstreamUnavailable(f"identity provider HTTP {resp.status_code} for {url}")
        return resp.json()

    def metadata(self) -> dict[str, Any]:
        if self._meta is None:
            meta = self._get(self.discovery_url)
            if str(meta.get("issuer", "")).rstrip("/") != self.issuer:
                raise UpstreamUnavailable(f"discovery issuer {meta.get('issuer')!r} does not match {self.issuer!r}")
            for key in ("authorization_endpoint", "token_endpoint"):
                if not meta.get(key):
                    raise UpstreamUnavailable(f"discovery document has no {key}")
            self._meta = meta
        return self._meta

    def jwks(self, *, refresh: bool = False) -> dict[str, Any]:
        if self.jwks_file is not None:
            return json.loads(Path(self.jwks_file).read_text())
        # Refresh at most every 60 s: an unknown `kid` (key rotation) triggers one reload, not a flood.
        if self._jwks is None or (refresh and time.monotonic() - self._jwks_at > 60):
            uri = self.metadata().get("jwks_uri")
            if not uri:
                raise UpstreamUnavailable("discovery document has no jwks_uri and no static JWKS is configured")
            self._jwks, self._jwks_at = self._get(uri), time.monotonic()
        return self._jwks

    def authorization_url(self, *, state: str, nonce: str, challenge: str) -> str:
        query = {"response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri,
                 "scope": self.scopes, "state": state, "nonce": nonce, "code_challenge": challenge,
                 "code_challenge_method": "S256"}
        endpoint = self.metadata()["authorization_endpoint"]
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{urlencode(query)}"

    def exchange(self, code: str, verifier: str) -> dict[str, Any]:
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
                "code_verifier": verifier, "client_id": self.client_id}
        auth = (self.client_id, self.client_secret) if self.client_secret else None
        try:
            resp = self._http.post(self.metadata()["token_endpoint"], data=data, auth=auth,
                                   headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"identity provider unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise Unauthenticated(f"identity provider refused the code (HTTP {resp.status_code})")
        body = resp.json()
        if not body.get("id_token"):
            raise Unauthenticated("identity provider returned no id_token")
        return body

    def _key(self, header: dict[str, Any]) -> Any:
        kid = header.get("kid")
        for refresh in (False, True):
            keys = [k for k in (self.jwks(refresh=refresh).get("keys") or []) if k.get("use", "sig") == "sig"]
            match = [k for k in keys if k.get("kid") == kid] if kid else keys
            if len(match) == 1:
                return jwt.PyJWK(match[0], algorithm=header.get("alg")).key
            if self.jwks_file is not None:
                break
        raise Unauthenticated("ID token signed with an unknown key")

    def validate_id_token(self, token: str, *, nonce: str) -> dict[str, Any]:
        """Signature (JWKS, asymmetric algorithms only), issuer, audience, expiry, issued-at, nonce;
        with several audiences the authorized party must be this client."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise Unauthenticated(f"ID token unreadable: {exc}") from exc
        alg = header.get("alg")
        if alg not in ALLOWED_ALGS:
            raise Unauthenticated(f"ID token algorithm {alg!r} is not accepted")
        try:
            claims = jwt.decode(token, self._key(header), algorithms=[alg], audience=self.client_id, issuer=self.issuer,
                                leeway=CLOCK_SKEW_SECONDS, options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.PyJWTError as exc:
            raise Unauthenticated(f"ID token rejected: {exc}") from exc
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise Unauthenticated("ID token nonce mismatch")
        aud = claims.get("aud")
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != self.client_id:
            raise Unauthenticated("ID token authorized party is not this client")
        return claims


@lru_cache
def provider() -> OidcProvider:
    s = get_settings()
    if not enabled():
        raise InvalidInput("single sign-on is not configured (ANALYSTOS_OIDC_ISSUER / ANALYSTOS_OIDC_CLIENT_ID)")
    return OidcProvider(issuer=s.oidc_issuer or "", client_id=s.oidc_client_id or "", client_secret=s.oidc_client_secret,
                        redirect_uri=s.oidc_redirect_uri, scopes=s.oidc_scopes, discovery_url=s.oidc_discovery_url,
                        jwks_file=s.oidc_jwks_file, transport=_transport)


# ----------------------------------------------------------------------------- claims -> identity
@dataclass
class MappedIdentity:
    subject: str
    email: str
    name: str
    groups: list[str]
    is_admin: bool
    roles: dict[str, str]  # workspace (id or name, as mapped) -> role
    attributes: dict[str, Any]


def _groups(claims: dict[str, Any], claim: str) -> list[str]:
    value = claims.get(claim)
    if isinstance(value, str):
        value = [value]
    return sorted({str(g) for g in value or []})


def map_claims(claims: dict[str, Any], mapping: Mapping, *, groups_claim: str = "groups") -> MappedIdentity:
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise Unauthenticated("ID token has no email claim (request the `email` scope)")
    groups = _groups(claims, groups_claim)
    roles: dict[str, str] = {}
    for grant in mapping.workspace_roles:
        if grant.group in groups and ROLE_RANK[grant.role] >= ROLE_RANK.get(roles.get(grant.workspace, ""), -1):
            roles[grant.workspace] = grant.role
    is_admin = bool(set(groups) & set(mapping.platform_admin_groups))
    if mapping.require_group and not roles and not is_admin:
        raise Unauthenticated("none of your identity provider groups is mapped to AnalystOS")
    attributes: dict[str, Any] = {"groups": groups}
    for attr, claim in mapping.attributes.items():
        if claim in claims:
            attributes[attr] = claims[claim]
    return MappedIdentity(subject=str(claims["sub"]), email=email, name=str(claims.get("name") or email.split("@")[0]),
                          groups=groups, is_admin=is_admin, roles=roles, attributes=attributes)


def _workspace_ids(session: Session, keys: list[str]) -> dict[str, str]:
    """Mapped workspace key (id or name) -> workspace id, for live workspaces only."""
    out: dict[str, str] = {}
    for ws in session.scalars(select(Workspace).where(Workspace.deleted_at.is_(None),
                                                      (Workspace.id.in_(keys)) | (Workspace.name.in_(keys)))):
        for key in keys:
            if key in (ws.id, ws.name) and key not in out:
                out[key] = ws.id
    return out


def provision(session: Session, claims: dict[str, Any], *, issuer: str, mapping: Mapping | None = None) -> User:
    """Find or create the local user for this identity and bring its IdP-managed state up to date:
    attributes from claims, platform admin from groups (SSO-created users only), and the workspace
    memberships the groups grant. Manual memberships are never touched; SSO grants no longer backed
    by a group are revoked."""
    settings = get_settings()
    mapping = mapping or load_mapping()
    ident = map_claims(claims, mapping, groups_claim=settings.oidc_groups_claim)
    link = session.scalar(select(UserIdentity).where(UserIdentity.issuer == issuer, UserIdentity.subject == ident.subject))
    user = session.get(User, link.user_id) if link else None
    if user is None:
        existing = session.scalar(select(User).where(User.email == ident.email))
        if existing is not None:
            # Linking by email is only safe when the IdP vouches for the address.
            if claims.get("email_verified") is not True:
                raise Unauthenticated("an account with this email exists; the identity provider did not verify the email")
            user = existing
        else:
            user = User(id=new_id("usr"), email=ident.email, name=ident.name, password_hash=UNUSABLE_PASSWORD,
                        is_admin=False, attributes={"sso_managed": True})
            session.add(user)
            session.flush()
            audit(f"user:{user.id}", "auth.sso_user_created", target=ident.email, decision="allow", session=session)
        link = UserIdentity(user_id=user.id, issuer=issuer, subject=ident.subject, groups=[], managed_memberships={})
        session.add(link)
    if not user.active:
        raise Unauthenticated("user inactive")
    attrs = dict(user.attributes or {})
    for key in [*mapping.attributes, "groups"]:
        attrs.pop(key, None)  # IdP-sourced attributes are replaced wholesale: a revoked claim must disappear
    attrs.update(ident.attributes)
    user.attributes = attrs
    if ident.is_admin:
        user.is_admin = True
    elif attrs.get("sso_managed"):
        user.is_admin = False
    managed = dict(link.managed_memberships or {})
    resolved = _workspace_ids(session, list(ident.roles))
    desired = {ws_id: ident.roles[key] for key, ws_id in resolved.items()}
    members = {m.workspace_id: m for m in session.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id))}
    changes: list[dict[str, Any]] = []
    for ws_id, role in desired.items():
        member = members.get(ws_id)
        if member is None:
            session.add(WorkspaceMember(workspace_id=ws_id, user_id=user.id, role=role))
            managed[ws_id] = role
            changes.append({"workspace": ws_id, "granted": role})
        elif ws_id in managed and member.role != role:
            changes.append({"workspace": ws_id, "from": member.role, "to": role})
            member.role, managed[ws_id] = role, role
    for ws_id in [w for w in managed if w not in desired]:
        member = members.get(ws_id)
        if member is not None and member.role == managed[ws_id]:
            session.delete(member)
            changes.append({"workspace": ws_id, "revoked": managed[ws_id]})
        managed.pop(ws_id)
    link.managed_memberships = managed
    link.groups = ident.groups
    link.email = ident.email
    link.last_login_at = utcnow()
    session.flush()
    audit(f"user:{user.id}", "auth.sso_login", target=issuer, decision="allow",
          details={"groups": ident.groups, "membership_changes": changes, "is_admin": user.is_admin}, session=session)
    return user


__all__ = ["ALLOWED_ALGS", "TX_COOKIE", "Mapping", "OidcProvider", "RoleGrant", "enabled", "load_mapping", "map_claims",
           "open_transaction", "pkce_pair", "provider", "provision", "safe_return_path", "seal_transaction",
           "set_http_transport"]
