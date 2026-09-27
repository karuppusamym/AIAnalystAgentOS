"""A local OpenID Connect identity provider for rehearsing single sign-on without a company IdP (P4-09).

    python scripts/fake_idp.py --port 8099 [--users users.yaml] [--client-id analystos-web]
    # then run AnalystOS with
    ANALYSTOS_OIDC_ISSUER=http://127.0.0.1:8099 ANALYSTOS_OIDC_CLIENT_ID=analystos-web \
    ANALYSTOS_OIDC_MAPPING_FILE=config/oidc.yaml ...

It is a real HTTP server with the endpoints AnalystOS uses — discovery, JWKS, authorization (PKCE S256 required)
and token — and signs RS256 ID tokens with a key generated at start-up. There is no password: the login page
lists the configured users and "signs in" the one you pick (or `login_hint=<user>` does it without a page). The
users file maps a login to its claims (sub, email, name, groups, any ABAC attribute):

    dana: {sub: idp-dana, email: dana@pilot.test, name: Dana, email_verified: true, groups: [pilot-analysts]}

For tests and rehearsals only: it authenticates nobody. `create_app` is imported by the integration test
(tests/integration/test_oidc_local_idp.py), which serves it on a loopback port.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import secrets
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

# Module level: FastAPI resolves the endpoints' (postponed) annotations against this module's globals.
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

DEFAULT_USERS: dict[str, dict[str, Any]] = {
    "dana": {"sub": "idp-dana", "email": "dana@pilot.test", "name": "Dana Analyst", "email_verified": True,
             "groups": ["pilot-analysts"], "department": "operations"},
    "omar": {"sub": "idp-omar", "email": "omar@pilot.test", "name": "Omar Approver", "email_verified": True,
             "groups": ["pilot-approvers"], "department": "finance"},
}


def create_app(issuer: str, *, client_id: str = "analystos-web", users: dict[str, dict[str, Any]] | None = None,
               token_ttl: int = 300):
    """The IdP as a FastAPI app. `app.state.users` is mutable: a test changes a user's groups between logins."""
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = f"local-{secrets.token_hex(4)}"
    jwk = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())), "kid": kid, "use": "sig", "alg": "RS256"}
    app = FastAPI(title="AnalystOS local identity provider (tests and rehearsals only)")
    app.state.users = dict(users if users is not None else DEFAULT_USERS)
    app.state.codes = {}
    app.state.issued = []
    base = issuer.rstrip("/")

    @app.get("/.well-known/openid-configuration")
    def discovery() -> dict[str, Any]:
        return {"issuer": issuer, "authorization_endpoint": f"{base}/authorize", "token_endpoint": f"{base}/token",
                "jwks_uri": f"{base}/certs", "response_types_supported": ["code"], "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"], "code_challenge_methods_supported": ["S256"]}

    @app.get("/certs")
    def certs() -> dict[str, Any]:
        return {"keys": [jwk]}

    @app.get("/authorize")
    def authorize(request: Request, response_type: str, redirect_uri: str = "", state: str = "", nonce: str = "",
                  code_challenge: str = "", code_challenge_method: str = "", login_hint: str | None = None):
        q = dict(request.query_params)
        if response_type != "code" or q.get("client_id") != client_id:
            raise HTTPException(400, "unsupported response_type or unknown client_id")
        if code_challenge_method != "S256" or not code_challenge:
            raise HTTPException(400, "PKCE S256 is required")
        if login_hint is None:
            links = "".join(f'<li><a href="{html.escape(f"{base}/authorize?" + urlencode({**q, "login_hint": u}))}">'
                            f"{html.escape(u)}</a> — {html.escape(str(c.get('email')))} {html.escape(str(c.get('groups')))}</li>"
                            for u, c in app.state.users.items())
            return HTMLResponse(f"<h1>Local identity provider</h1><p>Sign in as:</p><ul>{links}</ul>")
        if login_hint not in app.state.users:
            return RedirectResponse(f"{redirect_uri}?{urlencode({'error': 'access_denied', 'state': state})}", status_code=302)
        code = secrets.token_urlsafe(24)
        app.state.codes[code] = {"user": login_hint, "nonce": nonce, "challenge": code_challenge, "redirect_uri": redirect_uri,
                                 "at": time.time()}
        return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", status_code=302)

    @app.post("/token")
    def token(grant_type: str = Form(...), code: str = Form(...), redirect_uri: str = Form(...),
              code_verifier: str = Form(...), client_id_form: str | None = Form(default=None, alias="client_id")):
        grant = app.state.codes.pop(code, None)
        if grant_type != "authorization_code" or grant is None or time.time() - grant["at"] > 120:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if redirect_uri != grant["redirect_uri"] or (client_id_form and client_id_form != client_id):
            return JSONResponse({"error": "invalid_grant", "error_description": "redirect_uri or client mismatch"}, status_code=400)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
        if not secrets.compare_digest(challenge, grant["challenge"]):
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)
        now = int(time.time())
        claims = {**app.state.users[grant["user"]], "iss": issuer, "aud": client_id, "iat": now, "exp": now + token_ttl,
                  "nonce": grant["nonce"]}
        app.state.issued.append(claims)
        id_token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})
        return {"access_token": secrets.token_urlsafe(16), "token_type": "Bearer", "expires_in": token_ttl, "id_token": id_token}

    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--client-id", default="analystos-web")
    ap.add_argument("--users", default=None, help="YAML file: login -> claims (default: two pilot users)")
    args = ap.parse_args(argv)
    import uvicorn
    import yaml

    users = yaml.safe_load(Path(args.users).read_text()) if args.users else None
    issuer = f"http://{args.host}:{args.port}"
    print(f"local identity provider at {issuer} (client_id {args.client_id}); for tests and rehearsals only", file=sys.stderr)
    uvicorn.run(create_app(issuer, client_id=args.client_id, users=users), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
