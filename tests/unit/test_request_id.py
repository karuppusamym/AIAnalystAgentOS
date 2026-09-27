"""P4-06 remaining: every response names its request (`X-Request-ID`) and every error envelope carries `request_id`,
the caller's id when it sent a well-formed one."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(sqlite_db):
    from analystos.api.app import app

    return TestClient(app)  # no lifespan: no scheduler or runtime is started


def test_a_domain_error_carries_the_callers_request_id(client):
    r = client.get("/api/ask/threads/ask_nope", headers={"X-Request-ID": "trace-42.a"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated" and r.json()["error"]["request_id"] == "trace-42.a"
    assert r.headers["X-Request-ID"] == "trace-42.a"


def test_without_one_the_platform_names_the_request(client):
    r = client.get("/api/ask/threads/ask_nope")
    rid = r.json()["error"]["request_id"]
    assert rid.startswith("req") and r.headers["X-Request-ID"] == rid
    other = client.get("/api/ask/threads/ask_nope").json()["error"]["request_id"]
    assert other != rid


def test_a_malformed_id_is_replaced_and_validation_errors_carry_it_too(client):
    r = client.post("/api/auth/login", json={"email": 5}, headers={"X-Request-ID": "bad id with spaces"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_input"
    rid = r.json()["error"]["request_id"]
    assert rid != "bad id with spaces" and rid.startswith("req") and r.headers["X-Request-ID"] == rid
    assert client.get("/api/ask/threads/x", headers={"X-Correlation-ID": "corr-1"}).json()["error"]["request_id"] == "corr-1"


def test_a_successful_response_names_its_request_too(client):
    r = client.get("/api/health", headers={"X-Request-ID": "ok-1"})
    assert r.headers["X-Request-ID"] == "ok-1"
