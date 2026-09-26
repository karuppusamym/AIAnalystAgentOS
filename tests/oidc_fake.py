"""A local fake OIDC identity provider for tests: RSA keys, a JWKS, discovery, an authorization
endpoint that records the PKCE challenge and a token endpoint that checks the verifier. Served
through httpx.MockTransport, so no network and no services."""
from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://idp.test/realms/analystos"
CLIENT_ID = "analystos-web"


def new_key(kid: str) -> tuple[Any, dict]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return key, {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


class FakeIdP:
    def __init__(self) -> None:
        self.key, jwk = new_key("k1")
        self.jwks = {"keys": [jwk]}
        self.codes: dict[str, dict[str, Any]] = {}  # code -> {claims, challenge}
        self.token_requests: list[dict[str, Any]] = []
        self.jwks_fetches = 0
        self.last_authorize: dict[str, str] = {}

    def id_token(self, claims: dict[str, Any], *, key: Any = None, kid: str = "k1", alg: str = "RS256",
                 headers: dict | None = None) -> str:
        now = int(time.time())
        body = {"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300, **claims}
        return jwt.encode(body, key if key is not None else self.key, algorithm=alg, headers={"kid": kid, **(headers or {})})

    def authorize(self, url: str, claims: dict[str, Any]) -> tuple[str, str]:
        """What the browser + IdP login page do: returns (code, state) for the callback."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.last_authorize = q
        assert q["code_challenge_method"] == "S256" and q["client_id"] == CLIENT_ID and q["response_type"] == "code"
        code = f"code-{len(self.codes) + 1}"
        self.codes[code] = {"claims": {**claims, "nonce": q["nonce"]}, "challenge": q["code_challenge"]}
        return code, q["state"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
                                             "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
                                             "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs"})
        if path.endswith("/certs"):
            self.jwks_fetches += 1
            return httpx.Response(200, json=self.jwks)
        if path.endswith("/token"):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append({"form": form, "auth": request.headers.get("authorization")})
            grant = self.codes.pop(form.get("code", ""), None)
            if grant is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
            if challenge != grant["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "PKCE verification failed"})
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer", "id_token": self.id_token(grant["claims"])})
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
