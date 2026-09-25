"""OIDC SSO (authorization code + PKCE, JWKS validation, group -> role mapping) and ABAC scope
narrowing (P4-S04, SEC-001..003), against a local fake IdP. No services."""
from __future__ import annotations

import base64
import hashlib
import json
import time

import jwt
import pytest
from tests.oidc_fake import CLIENT_ID, ISSUER, FakeIdP, new_key

from analystos.contracts.policy import AttributeRule, DataScope
from analystos.core.errors import Unauthenticated
from analystos.governance.policy import apply_attribute_rules, attributes_satisfy
from analystos.security import oidc


@pytest.fixture
def idp():
    return FakeIdP()


def _provider(idp: FakeIdP, **kw) -> oidc.OidcProvider:
    return oidc.OidcProvider(issuer=ISSUER, client_id=CLIENT_ID, client_secret=kw.pop("client_secret", None),
                             redirect_uri="https://analystos.test/api/auth/oidc/callback", scopes="openid email groups",
                             transport=idp.transport(), **kw)


# ----------------------------------------------------------------------------- PKCE + transaction
def test_pkce_pair_is_s256():
    verifier, challenge = oidc.pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert oidc.pkce_pair()[0] != verifier


def test_transaction_cookie_is_signed_bound_to_state_and_expires(monkeypatch):
    sealed = oidc.seal_transaction({"state": "s1", "nonce": "n", "verifier": "v", "return_to": "/"})
    assert oidc.open_transaction(sealed, "s1")["verifier"] == "v"
    with pytest.raises(Unauthenticated, match="state mismatch"):
        oidc.open_transaction(sealed, "s2")
    with pytest.raises(Unauthenticated, match="invalid"):
        oidc.open_transaction(sealed[:-3] + "abc", "s1")
    with pytest.raises(Unauthenticated, match="missing"):
        oidc.open_transaction(None, "s1")
    forged = jwt.encode({"state": "s1", "aud": oidc.TX_AUDIENCE, "exp": int(time.time()) + 60}, "not-the-secret-0123456789abcdef!",
                        algorithm="HS256")
    with pytest.raises(Unauthenticated):
        oidc.open_transaction(forged, "s1")
    expired = jwt.encode({"state": "s1", "aud": oidc.TX_AUDIENCE, "exp": int(time.time()) - 5},
                         oidc.get_settings().jwt_secret, algorithm="HS256")
    with pytest.raises(Unauthenticated, match="expired"):
        oidc.open_transaction(expired, "s1")


@pytest.mark.parametrize("value, expected", [("/workspaces/ws_1", "/workspaces/ws_1"), ("https://evil.example", "/"),
                                             ("//evil.example/x", "/"), ("/\\evil", "/"), (None, "/")])
def test_return_path_is_never_an_open_redirect(value, expected):
    assert oidc.safe_return_path(value) == expected


# ----------------------------------------------------------------------------- code flow against the fake IdP
def test_authorization_code_flow_with_pkce(idp):
    p = _provider(idp, client_secret="shh")
    verifier, challenge = oidc.pkce_pair()
    url = p.authorization_url(state="st", nonce="no", challenge=challenge)
    assert url.startswith(f"{ISSUER}/protocol/openid-connect/auth?")
    code, state = idp.authorize(url, {"sub": "u-1", "email": "Ana@corp.test"})
    assert state == "st" and idp.last_authorize["code_challenge"] == challenge
    assert "code_verifier" not in url and verifier not in url  # the verifier never travels through the browser
    tokens = p.exchange(code, verifier)
    sent = idp.token_requests[-1]
    assert sent["form"]["code_verifier"] == verifier and sent["form"]["grant_type"] == "authorization_code"
    assert sent["auth"].startswith("Basic ")  # confidential client: client_secret_basic
    claims = p.validate_id_token(tokens["id_token"], nonce="no")
    assert claims["sub"] == "u-1" and claims["email"] == "Ana@corp.test"


def test_wrong_verifier_is_refused_by_the_idp(idp):
    p = _provider(idp)
    _, challenge = oidc.pkce_pair()
    code, _ = idp.authorize(p.authorization_url(state="s", nonce="n", challenge=challenge), {"sub": "u", "email": "a@b.c"})
    with pytest.raises(Unauthenticated, match="refused the code"):
        p.exchange(code, "some-other-verifier-" + "x" * 40)
    assert idp.token_requests[-1]["auth"] is None  # public client: PKCE only


@pytest.mark.parametrize("change, match", [
    ({"aud": "someone-else"}, "Audience"),
    ({"iss": "https://evil.example"}, "(?i)issuer"),
    ({"exp": int(time.time()) - 3600}, "expired"),
    ({"nonce": "replayed"}, "nonce"),
    ({"aud": [CLIENT_ID, "other"]}, "authorized party"),
])
def test_id_token_claims_are_enforced(idp, change, match):
    p = _provider(idp)
    token = idp.id_token({"sub": "u", "email": "a@b.c", "nonce": "n", **change})
    with pytest.raises(Unauthenticated, match=match):
        p.validate_id_token(token, nonce="n")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _hs256_forgery(claims: dict, secret: bytes) -> str:
    """The classic confusion attack: HMAC-sign with the IdP's *public* key material."""
    import hmac

    signing = f"{_b64(json.dumps({'alg': 'HS256', 'kid': 'k1'}).encode())}.{_b64(json.dumps(claims).encode())}"
    return f"{signing}.{_b64(hmac.new(secret, signing.encode(), hashlib.sha256).digest())}"


def test_signature_key_and_algorithm_are_enforced(idp):
    p = _provider(idp)
    other_key, _ = new_key("k1")
    with pytest.raises(Unauthenticated, match="Signature"):
        p.validate_id_token(idp.id_token({"sub": "u", "nonce": "n"}, key=other_key), nonce="n")
    with pytest.raises(Unauthenticated, match="unknown key"):
        p.validate_id_token(idp.id_token({"sub": "u", "nonce": "n"}, kid="nope"), nonce="n")
    # algorithm confusion: an HMAC token keyed with the public JWK, and an unsigned token
    hs = _hs256_forgery({"iss": ISSUER, "aud": CLIENT_ID, "sub": "u", "nonce": "n", "iat": int(time.time()),
                         "exp": int(time.time()) + 60}, json.dumps(idp.jwks["keys"][0]).encode())
    with pytest.raises(Unauthenticated, match="not accepted"):
        p.validate_id_token(hs, nonce="n")
    header = base64.urlsafe_b64encode(b'{"alg":"none","kid":"k1"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps({"iss": ISSUER, "aud": CLIENT_ID, "sub": "u", "nonce": "n"}).encode()).rstrip(b"=").decode()
    with pytest.raises(Unauthenticated, match="not accepted"):
        p.validate_id_token(f"{header}.{body}.", nonce="n")


def test_key_rotation_refreshes_the_jwks_once(idp):
    p = _provider(idp)
    p.validate_id_token(idp.id_token({"sub": "u", "nonce": "n"}), nonce="n")
    assert idp.jwks_fetches == 1
    new_private, new_jwk = new_key("k2")
    idp.jwks = {"keys": [new_jwk]}
    p._jwks_at -= 120  # the refresh rate limit has passed
    claims = p.validate_id_token(idp.id_token({"sub": "u2", "nonce": "n"}, key=new_private, kid="k2"), nonce="n")
    assert claims["sub"] == "u2" and idp.jwks_fetches == 2


def test_static_jwks_file_needs_no_jwks_endpoint(idp, tmp_path):
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps(idp.jwks))
    p = _provider(idp, jwks_file=path)
    assert p.validate_id_token(idp.id_token({"sub": "u", "nonce": "n"}), nonce="n")["sub"] == "u"
    assert idp.jwks_fetches == 0


def test_discovery_issuer_must_match(idp):
    p = oidc.OidcProvider(issuer="https://other.test", client_id=CLIENT_ID, client_secret=None, redirect_uri="x",
                          scopes="openid", discovery_url=f"{ISSUER}/.well-known/openid-configuration", transport=idp.transport())
    with pytest.raises(Exception, match="does not match"):
        p.metadata()


# ----------------------------------------------------------------------------- claims -> roles and attributes
MAPPING = oidc.Mapping(
    platform_admin_groups=["aos-admins"],
    workspace_roles=[oidc.RoleGrant("itsm-viewers", "ITSM", "viewer"), oidc.RoleGrant("itsm-analysts", "ITSM", "analyst"),
                     oidc.RoleGrant("fin-approvers", "ws_fin", "approver")],
    attributes={"department": "department", "pii_clearance": "pii_clearance"})


def test_groups_map_to_the_highest_role_and_attributes():
    ident = oidc.map_claims({"sub": "u", "email": "Ana@Corp.test", "groups": ["itsm-viewers", "itsm-analysts", "fin-approvers"],
                             "department": "finance", "pii_clearance": True, "salary": 1}, MAPPING)
    assert ident.email == "ana@corp.test" and ident.roles == {"ITSM": "analyst", "ws_fin": "approver"}
    assert not ident.is_admin
    assert ident.attributes == {"groups": ["fin-approvers", "itsm-analysts", "itsm-viewers"], "department": "finance",
                                "pii_clearance": True}  # only mapped claims become attributes
    assert oidc.map_claims({"sub": "a", "email": "a@x", "groups": "aos-admins"}, MAPPING).is_admin


def test_unmapped_users_can_be_refused_and_email_is_required():
    strict = oidc.Mapping(workspace_roles=MAPPING.workspace_roles, require_group=True)
    with pytest.raises(Unauthenticated, match="mapped"):
        oidc.map_claims({"sub": "u", "email": "a@x", "groups": ["random"]}, strict)
    with pytest.raises(Unauthenticated, match="email"):
        oidc.map_claims({"sub": "u", "groups": ["itsm-analysts"]}, MAPPING)


def test_mapping_file_rejects_unknown_roles(tmp_path):
    path = tmp_path / "oidc.yaml"
    path.write_text("workspace_roles: [{group: g, workspace: w, role: superuser}]\n")
    with pytest.raises(Exception, match="unknown role"):
        oidc.load_mapping(path)
    assert oidc.load_mapping(oidc.get_settings().oidc_mapping_file).attributes  # the shipped file parses


# ----------------------------------------------------------------------------- ABAC
def _scope() -> DataScope:
    return DataScope(workspace_id="w", user_id="u", role="analyst", assets=["fin.payroll", "fin.invoices", "ops.incident"],
                     asset_sources={"fin.payroll": "s", "fin.invoices": "s", "ops.incident": "s"},
                     columns={"fin.payroll": ["employee", "salary"], "fin.invoices": ["amount", "iban", "region"],
                              "ops.incident": ["priority", "caller_email"]},
                     denied_columns=["fin.payroll.salary"])


RULES = [AttributeRule(id="payroll-hr-only", assets=["*.payroll"], require={"department": ["hr"]}),
         AttributeRule(id="iban-finance", columns=["fin.invoices.iban"], require={"department": ["finance", "audit"]}),
         AttributeRule(id="emea-region", column_tags=["region_restricted"], require={"region": ["emea"]})]


def test_attribute_rules_only_narrow_the_scope():
    tags = {"fin.invoices.region": {"region_restricted"}}
    scope = _scope()
    applied = apply_attribute_rules(scope, {"department": "sales"}, RULES, tags)
    assert applied == ["payroll-hr-only", "iban-finance", "emea-region"]
    assert "fin.payroll" not in scope.assets and "fin.payroll" not in scope.columns and "fin.payroll" not in scope.asset_sources
    assert set(scope.denied_columns) == {"fin.invoices.iban", "fin.invoices.region"}  # payroll's denial went with the asset
    finance = _scope()
    assert apply_attribute_rules(finance, {"department": ["finance"], "region": "emea"}, RULES, tags) == ["payroll-hr-only"]
    assert "fin.invoices.iban" not in finance.denied_columns and "fin.invoices.region" not in finance.denied_columns
    hr = _scope()
    apply_attribute_rules(hr, {"department": "hr", "region": "emea"}, RULES, tags)
    assert "fin.payroll" in hr.assets and "fin.payroll.salary" in hr.denied_columns  # ABAC never lifts an existing denial


def test_attributes_satisfy_semantics():
    assert attributes_satisfy({"department": "hr"}, {})
    assert attributes_satisfy({"groups": ["a", "b"]}, {"groups": ["b"]})
    assert not attributes_satisfy({}, {"department": ["hr"]})
    assert not attributes_satisfy({"department": "hr"}, {"department": ["hr"], "region": ["emea"]})
    assert attributes_satisfy({"pii_clearance": True}, {"pii_clearance": ["True"]})
