"""P4-U04 knowledge studio API against Postgres: packs, documents (read, human edit -> revision,
optimistic concurrency, trust-field rules, read-only platform/imported packs), revision history,
import/export over HTTP, the push approval over HTTP, and the semantic graph (governed edges vs
inferred)."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import User
from analystos.knowledge import okf, store, suggestions
from analystos.knowledge.entries import render_entry

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"
ATLAS_ZIP = Path(__file__).resolve().parents[1] / "fixtures" / "okf" / "atlas-sample.zip"


def _admin(s):
    from analystos.core.config import get_settings

    return s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))


@pytest.fixture()
def workspace(control_db):
    from analystos.services.workspaces import add_member, create_workspace

    with session_scope() as s:
        admin = _admin(s)
        ws = create_workspace(s, admin, name=f"u04 {new_id('t')}", objective="SLA breaches")
        s.flush()
        add_member(s, admin, ws.id, "analyst@analystos.local", "analyst")
        return ws.id


@pytest.fixture()
def api(control_db):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    with TestClient(app) as client:
        yield client


def _login(api, email: str) -> dict:
    r = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _approved_draft(api, ws: str, owner: dict) -> str:
    """A model draft approved through the review queue: a document the queue may replace."""
    with session_scope() as s:
        row = suggestions.propose(s, ws, kind="term", subject="term:reopen rate", title="Reopen rate",
                                  fields={"body": suggestions.field("Share of resolved incidents reopened within 7 days.", 0.55,
                                                                    source="model", model="m1")},
                                  origin="crawler.enrichment", proposed_by="model:m1")
        sid, path = row.id, row.path
    r = api.post(f"/api/workspaces/{ws}/knowledge/suggestions/review", headers=owner,
                 json={"decisions": [{"id": sid, "action": "approve"}]})
    assert r.status_code == 200 and r.json()["approved"][0]["path"] == path, r.text
    return path


def test_browse_edit_history_and_trust_fields(api, workspace):
    owner, analyst = _login(api, "admin@analystos.local"), _login(api, "analyst@analystos.local")
    path = _approved_draft(api, workspace, owner)
    base = f"/api/workspaces/{workspace}/knowledge"

    packs = api.get(f"{base}/packs", headers=owner).json()
    assert packs[0]["kind"] == "workspace" and packs[0]["writable"] and packs[-1]["kind"] == "platform"
    assert not packs[-1]["writable"] and packs[-1]["read_only"]
    assert not api.get(f"{base}/packs", headers=analyst).json()[0]["writable"]  # analysts read, editors write
    ws_pack, platform = packs[0]["id"], packs[-1]["id"]

    listed = api.get(f"{base}/packs/{ws_pack}/documents", headers=analyst).json()["documents"]
    entry = next(d for d in listed if d["path"] == path)
    assert entry["authorship"] == "review_queue" and entry["trust_tier"] == "human-reviewed" and entry["type"] == "Glossary Term"
    doc = api.get(f"{base}/packs/{ws_pack}/document", headers=analyst, params={"path": path}).json()
    assert doc["trust"]["tier"] == "human-reviewed" and doc["frontmatter"]["analystos"]["review"]["origin"] == "crawler.enrichment"
    assert "reopened within 7 days" in doc["body"]
    located = api.get(f"{base}/locate", headers=analyst, params={"document_id": entry["document_id"]}).json()
    assert (located["pack_id"], located["path"]) == (ws_pack, path)

    fm = dict(doc["frontmatter"])
    edit = {"path": path, "frontmatter": {**fm, "status": "draft", "stale_after": "2027-01-01T00:00:00+00:00"},
            "body": "# Definition\n\nShare of resolved incidents reopened within 14 days.", "base_sha256": doc["sha256"]}
    url = f"{base}/packs/{ws_pack}/document"
    assert api.put(url, headers=analyst, json=edit).status_code == 403  # editor role
    assert api.put(url, headers=owner, json={**edit, "base_sha256": "0" * 64}).status_code == 409  # stale base
    assert api.put(url, headers=owner, json={**edit, "base_sha256": None}).status_code == 409  # a new path cannot hide an edit
    forged = {**edit, "frontmatter": {**edit["frontmatter"], "verified": [*fm["verified"], {"by": "human:someone-else"}]}}
    bad = api.put(url, headers=owner, json=forged)
    assert bad.status_code == 422 and "mark_reviewed" in bad.text  # nobody writes another's verification
    assert api.put(url, headers=owner, json={**edit, "frontmatter": {**fm, "type": ""}}).status_code == 422
    assert api.put(url, headers=owner, json={**edit, "frontmatter": {**fm, "stale_after": "next week"}}).status_code == 422

    saved = api.put(url, headers=owner, json={**edit, "mark_reviewed": True, "reason": "14-day window"})
    assert saved.status_code == 200, saved.text
    out = saved.json()
    after = out["document"]
    assert out["changed"] and after["trust"]["status"] == "draft" and after["trust"]["stale_after"].startswith("2027-01-01")
    admin_id = api.get("/api/auth/me", headers=owner).json()["id"]
    assert [e["by"] for e in after["trust"]["verified"]] == [f"human:{admin_id}"]  # one entry per person, re-stamped
    ext = after["frontmatter"]["analystos"]
    assert "review" not in ext and ext["reviewed_draft"]["origin"] == "crawler.enrichment"
    assert after["authorship"] == "owner"
    with session_scope() as s:  # the human edit is owner content now: no draft may replace it
        assert suggestions.protected(s, workspace, path)
        assert suggestions.propose(s, workspace, kind="term", subject="term:reopen rate v2", title="Reopen rate",
                                   fields={"body": suggestions.field("Another definition.", 0.5, source="model")},
                                   origin="crawler.enrichment", proposed_by="model:m1") is None
    same = api.put(url, headers=owner, json={**edit, "frontmatter": after["frontmatter"], "body": after["body"],
                                             "base_sha256": after["sha256"]})
    assert same.status_code == 200 and not same.json()["changed"]  # nothing changed: no revision

    history = api.get(f"{base}/packs/{ws_pack}/revisions", headers=analyst, params={"path": path}).json()
    assert [h["number"] for h in history] == [out["revision"], out["revision"] - 1]
    assert history[0]["origin"] == "studio" and history[0]["changed"] == [path] and history[0]["reason"] == "14-day window"
    assert history[1]["origin"] == "review" and path in history[1]["added"]
    old = api.get(f"{base}/packs/{ws_pack}/document", headers=analyst, params={"path": path, "revision": history[1]["number"]}).json()
    assert "7 days" in old["body"] and old["sha256"] == doc["sha256"]

    new = {"path": "rules/p1-first.md", "frontmatter": {"type": "Business Rule", "title": "P1 first"},
           "body": "# Rule\n\nP1 incidents are worked before any other. See [reopen rate](/glossary/reopen-rate.md)."}
    created = api.put(url, headers=owner, json=new).json()["document"]
    assert created["trust"]["tier"] == "unverified" and created["links"][0]["exists"]
    assert api.put(url, headers=owner, json={**new, "path": "../escape.md"}).status_code == 422
    assert api.put(url, headers=owner, json={**new, "path": "index.md"}).status_code == 422
    gone = api.delete(url, headers=owner, params={"path": "rules/p1-first.md", "base_sha256": created["sha256"]})
    assert gone.status_code == 200 and gone.json()["deleted"] == "rules/p1-first.md"

    # platform and imported packs are read-only for everyone, owners included
    pdocs = api.get(f"{base}/packs/{platform}/documents", headers=owner).json()["documents"]
    first = next(d for d in pdocs if d["markdown"] and not d["reserved"] and d["type"])
    pdoc = api.get(f"{base}/packs/{platform}/document", headers=owner, params={"path": first["path"]}).json()
    r = api.put(f"{base}/packs/{platform}/document", headers=owner,
                json={"path": first["path"], "frontmatter": pdoc["frontmatter"], "body": "changed", "base_sha256": pdoc["sha256"]})
    assert r.status_code == 403
    assert api.get(f"/api/workspaces/{new_id('ws')}/knowledge/packs", headers=owner).status_code == 404


def test_import_and_export_over_http(api, workspace):
    owner, analyst = _login(api, "admin@analystos.local"), _login(api, "analyst@analystos.local")
    base = f"/api/workspaces/{workspace}/knowledge"
    data = ATLAS_ZIP.read_bytes()
    files = {"file": ("atlas-sample.zip", data, "application/zip")}
    assert api.post(f"{base}/import", headers=analyst, files=files, data={"slug": "atlas"}).status_code == 403
    r = api.post(f"{base}/import", headers=owner, files=files, data={"slug": "atlas"})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert (rep["format"], rep["okf_root"], rep["documents"], rep["changed"]) == ("atlas", "bundle", 7, True)
    again = api.post(f"{base}/import", headers=owner, files=files, data={"slug": "atlas"}).json()
    assert not again["changed"] and again["revision"] == rep["revision"]
    hostile = io.BytesIO()
    with zipfile.ZipFile(hostile, "w") as zf:
        zf.writestr("../evil.md", "---\ntype: T\n---\n")
    bad = api.post(f"{base}/import", headers=owner, files={"file": ("evil.zip", hostile.getvalue(), "application/zip")},
                   data={"slug": "evil"})
    assert bad.status_code == 422 and "PATH" in bad.text.upper()

    packs = {p["slug"]: p for p in api.get(f"{base}/packs", headers=owner).json()}
    atlas = packs["atlas"]
    assert atlas["kind"] == "imported" and atlas["read_only"] and not atlas["writable"]
    exported = api.get(f"{base}/packs/{atlas['id']}/export", headers=analyst)
    assert exported.status_code == 200 and exported.headers["content-type"] == "application/zip"
    assert 'filename="atlas-r' in exported.headers["content-disposition"]
    original = zipfile.ZipFile(io.BytesIO(data))
    out = zipfile.ZipFile(io.BytesIO(exported.content))
    assert {i.filename: out.read(i) for i in out.infolist()} == {i.filename: original.read(i) for i in original.infolist()}
    doc = next(d for d in api.get(f"{base}/packs/{atlas['id']}/documents", headers=owner).json()["documents"] if d["type"])
    read = api.get(f"{base}/packs/{atlas['id']}/document", headers=owner, params={"path": doc["path"]}).json()
    r = api.put(f"{base}/packs/{atlas['id']}/document", headers=owner,
                json={"path": doc["path"], "frontmatter": read["frontmatter"], "body": "edited", "base_sha256": read["sha256"]})
    assert r.status_code == 403  # imported packs change only by re-import

    # the workspace pack: export is a download; a push out needs the K01 approval first
    ws_pack = packs["workspace"]["id"]
    with session_scope() as s:
        store.commit(s, store.workspace_pack(s, workspace), {"glossary/backlog.md": render_entry(
            kind="term", name="Backlog", body="Open work not yet started.").encode()}, author="human:test", reason="t",
            origin="user", merge=True)
    zipped = api.get(f"{base}/packs/{ws_pack}/export", headers=owner)
    assert zipped.status_code == 200 and "glossary/backlog.md" in zipfile.ZipFile(io.BytesIO(zipped.content)).namelist()
    assert api.post(f"{base}/packs/{ws_pack}/push/request", headers=owner).status_code == 409  # no remote configured
    with session_scope() as s:
        from analystos.knowledge import remote

        remote.set_remote(s, store.workspace_pack(s, workspace), "file:///tmp/nowhere.git", "knowledge")
    apr = api.post(f"{base}/packs/{ws_pack}/push/request", headers=owner)
    assert apr.status_code == 200 and apr.json()["status"] == "pending" and apr.json()["action"] == "knowledge.push"
    pushed = api.post(f"{base}/packs/{ws_pack}/push", headers=owner, json={"approval_id": apr.json()["id"]})
    assert pushed.status_code in (403, 409) and "approv" in pushed.text.lower()  # still pending: nothing leaves


def test_semantic_graph_draws_governed_edges_solid_and_inferred_dashed(api, workspace):
    from analystos.db.models import Relationship, Source, SourceAsset
    from analystos.semantic import service as sem

    owner = _login(api, "admin@analystos.local")
    with session_scope() as s:
        src = Source(id=new_id("src"), workspace_id=workspace, kind="postgres", name="pg", config={}, status="ready")
        s.add(src)
        s.flush()
        inc = SourceAsset(id=new_id("ast"), source_id=src.id, workspace_id=workspace, schema_name="sn", name="incident",
                          source_name="incident", selected=True)
        grp = SourceAsset(id=new_id("ast"), source_id=src.id, workspace_id=workspace, schema_name="sn", name="sys_user_group",
                          source_name="sys_user_group", selected=True)
        usr = SourceAsset(id=new_id("ast"), source_id=src.id, workspace_id=workspace, schema_name="sn", name="sys_user",
                          source_name="sys_user", selected=True)
        s.add_all([inc, grp, usr])
        s.flush()
        s.add_all([
            Relationship(id=new_id("rel"), workspace_id=workspace, from_asset_id=inc.id, from_column="assignment_group",
                         to_asset_id=grp.id, to_column="sys_id", confidence=0.95, validated=True, origin="discovered"),
            Relationship(id=new_id("rel"), workspace_id=workspace, from_asset_id=inc.id, from_column="caller_id",
                         to_asset_id=usr.id, to_column="sys_id", confidence=0.62, validated=False, origin="discovered"),
        ])
        inc_id = inc.id
        pack = store.workspace_pack(s, workspace)
        store.commit(s, pack, {
            "glossary/breach.md": okf.render_document(
                {"type": "Glossary Term", "title": "Breach", "verified": [{"by": "human:u1", "at": "2026-09-01T00:00:00Z"}],
                 "analystos": {"kind": "term", "mapped_columns": ["sn.incident.made_sla"]}},
                "# Definition\n\nA missed SLA. See [priority](/glossary/priority.md).").encode(),
            "glossary/priority.md": okf.render_document(
                {"type": "Glossary Term", "title": "Priority", "analystos": {"kind": "term", "mapped_columns": ["sn.incident.priority"]}},
                "# Definition\n\nUrgency times impact. See [breach](/glossary/breach.md).").encode(),
        }, author="human:test", reason="t", origin="user", merge=True)
        suggestions.propose(s, workspace, kind="table_description", subject=f"asset:{inc_id}", title="incident",
                            fields={"description": suggestions.field("One row per incident.", 0.7, source="model")},
                            origin="crawler.enrichment", proposed_by="model:m1")
        admin = _admin(s)
        from analystos.contracts.semantic import DialectExpression, SemanticMetricDef

        sem.propose_metric(s, workspace, SemanticMetricDef(name="breach_rate", expressions=[DialectExpression(expression="AVG(made_sla)")],
                                                             source_columns=["sn.incident.made_sla"]),
                           proposed_by=admin.id, via="user")
    g = api.get(f"/api/workspaces/{workspace}/knowledge/graph", headers=owner)
    assert g.status_code == 200, g.text
    body = g.json()
    nodes = {n["id"]: n for n in body["nodes"]}
    label = {n["id"]: n["label"] for n in body["nodes"]}

    def edges(kind):
        return {(label[e["source"]], label[e["target"]], e["governed"]) for e in body["edges"] if e["kind"] == kind}

    assert edges("join") == {("sn.incident", "sn.sys_user_group", True), ("sn.incident", "sn.sys_user", False)}
    assert ("Breach", "sn.incident", True) in edges("maps") and ("Priority", "sn.incident", False) in edges("maps")
    assert edges("links") == {("Breach", "Priority", True), ("Priority", "Breach", False)}
    assert ("breach_rate", "sn.incident", False) in edges("reads")  # proposed metric: inferred until approved
    assert edges("suggests") == {("incident", "sn.incident", False)}
    assert nodes["metric:breach_rate"]["status"] == "proposed"
    assert body["governed"] == 3 and body["inferred"] == sum(1 for e in body["edges"] if not e["governed"])
