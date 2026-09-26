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
        from analystos.core.ids import new_id
        from analystos.db.base import session_scope
        from analystos.db.models import Source, SourceAsset, SourceColumn

        source_id, asset_id = new_id("src"), new_id("ast")
        with session_scope() as session:
            session.add(Source(id=source_id, workspace_id=ws_id, kind="csv", name="inventory source",
                               config={}, status="ready", execution_mode="staged", staging_schema="src_inventory"))
            session.add(SourceAsset(id=asset_id, source_id=source_id, workspace_id=ws_id,
                                    schema_name="src_inventory", name="sample", source_name="sample"))
            session.add(SourceColumn(asset_id=asset_id, name="value", data_type="integer"))
        assert client.post(f"/api/workspaces/{ws_id}/members", headers=headers,
                           json={"email": "approver@analystos.local", "role": "viewer"}).status_code == 200
        viewer_login = client.post("/api/auth/login", json={"email": "approver@analystos.local",
                                                      "password": "ChangeMe123!"})
        viewer_headers = {"Authorization": f"Bearer {viewer_login.json()['access_token']}"}
        assert client.get(f"/api/workspaces/{ws_id}/inventory", headers=viewer_headers).status_code == 403
        before = client.get(f"/api/workspaces/{ws_id}/inventory", headers=headers)
        assert before.status_code == 200
        assert before.json()["control_rows"]["workspace"] == 1
        assert before.json()["control_rows"]["workspace_member"] == 2
        assert before.json()["control_rows"]["source_column"] == 1
        assert before.json()["staged_schemas"] == ["src_inventory"]
        assert before.json()["archived"] is False
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
        assert client.get(f"/api/workspaces/{ws_id}/inventory", headers=headers).json()["status"] == "disabled"
        assert client.post(f"/api/workspaces/{ws_id}/analysis", headers=headers,
                           json={"objective": "Measure lifecycle behavior"}).status_code == 404

        active = client.patch(f"/api/workspaces/{ws_id}", headers=headers, json={"status": "active"})
        assert active.status_code == 200 and active.json()["status"] == "active"
        assert client.get(f"/api/workspaces/{ws_id}", headers=headers).status_code == 200

        archived = client.delete(f"/api/workspaces/{ws_id}", headers=headers)
        assert archived.status_code == 200 and archived.json()["purged"] is False
        retained = client.get(f"/api/workspaces/{ws_id}/inventory", headers=headers)
        assert retained.status_code == 200 and retained.json()["archived"] is True
        assert retained.json()["control_rows"]["workspace_member"] == 2
        assert client.get(f"/api/workspaces/{ws_id}", headers=headers).status_code == 404
