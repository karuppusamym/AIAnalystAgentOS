"""P4-09 named owners through the HTTP API on the compose Postgres: only a workspace owner names the business and
technical owner of the workspace and of a source; pilot readiness lists them missing until they are named; the
admin overview lists every workspace's gaps. Skips cleanly without the stack."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def _h(client, email: str) -> dict[str, str]:
    r = client.post("/api/auth/login", json={"email": email, "password": "ChangeMe123!"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_named_owners_and_readiness_through_the_api(control_db):
    from analystos.api.app import app
    from analystos.core.ids import new_id

    with TestClient(app) as client:
        admin, analyst = _h(client, "admin@analystos.local"), _h(client, "analyst@analystos.local")
        ws = client.post("/api/workspaces", headers=admin, json={"name": f"Pilot owners {new_id('p')}"}).json()["id"]
        assert client.post(f"/api/workspaces/{ws}/members", headers=admin,
                           json={"email": "analyst@analystos.local", "role": "analyst"}).status_code == 200
        from analystos.db.base import session_scope
        from analystos.db.models import Source

        src = new_id("src")
        with session_scope() as s:  # registered as the pilot would; the kind's connection settings do not matter here
            s.add(Source(id=src, workspace_id=ws, kind="servicenow", name="ServiceNow ITSM", config={}, status="registered"))
        before = client.get(f"/api/workspaces/{ws}/pilot-readiness", headers=analyst).json()
        gaps = {(c["check"], c["subject"].split(" ")[0]) for c in before["checks"] if c["status"] != "pass"}
        assert {("owner.business", "workspace"), ("owner.technical", "workspace"), ("owner.business", f"source:{src}"),
                ("owner.technical", f"source:{src}"), ("connector.certified", f"source:{src}")} <= gaps
        body = {"business": {"name": "Priya Service Owner", "email": "priya@pilot.test"},
                "technical": {"name": "Tom Platform", "email": "tom@pilot.test"}}
        assert client.put(f"/api/workspaces/{ws}/owners", headers=analyst, json=body).status_code == 403
        assert client.put(f"/api/workspaces/{ws}/owners", headers=admin, json={"business": {"name": "x", "email": "nope"}}
                          ).status_code == 422
        put = client.put(f"/api/workspaces/{ws}/owners", headers=admin, json=body)
        assert put.status_code == 200 and put.json()["owners"]["business"]["email"] == "priya@pilot.test"
        assert client.put(f"/api/workspaces/{ws}/sources/{src}/owners", headers=admin, json=body).status_code == 200
        after = client.get(f"/api/workspaces/{ws}/pilot-readiness", headers=admin).json()
        owners = [c for c in after["checks"] if c["check"].startswith("owner.")]
        assert owners and all(c["status"] == "pass" for c in owners)
        cert = next(c for c in after["checks"] if c["check"] == "connector.certified")
        assert cert["status"] == "missing" and "servicenow" in cert["reason"]  # the mock never certifies it
        assert client.get(f"/api/workspaces/{ws}", headers=admin).json()["owners"]["technical"]["name"] == "Tom Platform"
        overview = client.get("/api/admin/pilot-readiness", headers=admin).json()
        mine = next(o for o in overview if o["workspace_id"] == ws)
        assert mine["verdict"] == "not_ready" and not any(g.startswith("owner.") for g in mine["gaps"])
        assert client.get("/api/admin/pilot-readiness", headers=analyst).status_code == 403
