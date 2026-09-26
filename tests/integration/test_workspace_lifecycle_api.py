"""The workspace status API gates access without discarding retained data."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from analystos.api.app import app

pytestmark = pytest.mark.integration


def test_owner_can_disable_and_reactivate_workspace(control_db):
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": "analyst@analystos.local", "password": "ChangeMe123!"})
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        made = client.post("/api/workspaces", headers=headers,
                           json={"name": "Lifecycle API", "objective": "Measure lifecycle behavior"})
        assert made.status_code == 200
        ws_id = made.json()["id"]
        mapped = client.get("/api/capabilities", headers=headers, params={"kind": "Agent", "workspace_id": ws_id})
        assert mapped.status_code == 200
        metadata = next(a for a in mapped.json()["capabilities"] if a["id"] == "agent.metadata")
        assert "metadata.read" in metadata["bindings"]["tools"]
        assert any(c["id"] == "skill.relationship_detection" for c in metadata["bindings"]["capabilities"])

        disabled = client.patch(f"/api/workspaces/{ws_id}", headers=headers, json={"status": "disabled"})
        assert disabled.status_code == 200 and disabled.json()["status"] == "disabled"
        listing = client.get("/api/workspaces", headers=headers)
        assert any(w["id"] == ws_id and w["status"] == "disabled" and w["role"] == "owner"
                   for w in listing.json())
        assert client.get(f"/api/workspaces/{ws_id}", headers=headers).status_code == 404
        assert client.post(f"/api/workspaces/{ws_id}/analysis", headers=headers,
                           json={"objective": "Measure lifecycle behavior"}).status_code == 404

        active = client.patch(f"/api/workspaces/{ws_id}", headers=headers, json={"status": "active"})
        assert active.status_code == 200 and active.json()["status"] == "active"
        assert client.get(f"/api/workspaces/{ws_id}", headers=headers).status_code == 200
