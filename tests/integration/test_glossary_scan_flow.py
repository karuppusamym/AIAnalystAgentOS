"""Stream E against Postgres: a crawl queues glossary terms and description questions (rules only), a person
accepts a term (edited) -> a trusted glossary entry that the next crawl links, a rejected term is never
suggested again, an answered description question writes the column text with origin `user`, an unanswered
Ask question becomes a "Define X?" draft, and the on-demand scan endpoint needs an editor."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select

from analystos.core.config import get_settings
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import ContextEntry, KnowledgeSuggestion, Source, SourceAsset, SourceColumn, User

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"


def _make(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE assignment_group (id INTEGER PRIMARY KEY, name VARCHAR(40) NOT NULL);
        CREATE TABLE service_offering (id INTEGER PRIMARY KEY, name VARCHAR(40) NOT NULL);
        CREATE TABLE ticket (id INTEGER PRIMARY KEY, priority INTEGER, state INTEGER, made_sla BOOLEAN, u_flag2 INTEGER,
                             category VARCHAR(20),
                             assignment_group_id INTEGER REFERENCES assignment_group(id),
                             service_offering_id INTEGER REFERENCES service_offering(id));
        CREATE TABLE ticket_task (id INTEGER PRIMARY KEY, ticket_id INTEGER REFERENCES ticket(id),
                                  assignment_group_id INTEGER REFERENCES assignment_group(id),
                                  service_offering_id INTEGER REFERENCES service_offering(id));
        INSERT INTO assignment_group VALUES (1, 'Network'), (2, 'Service Desk');
        INSERT INTO service_offering VALUES (1, 'Email'), (2, 'VPN');
        INSERT INTO ticket VALUES (1, 1, 1, 1, 0, 'hardware', 1, 1), (2, 2, 2, 0, 1, 'software', 2, 2),
                                  (3, 3, 6, 1, 0, 'hardware', 1, 1), (4, 4, 7, 1, 1, 'network', 2, 2),
                                  (5, 5, 1, 0, 0, 'software', 1, 1), (6, 1, 2, 1, 1, 'hardware', 2, 2),
                                  (7, 2, 6, 1, 0, 'hardware', 1, 2), (8, 3, 7, 0, 1, 'network', 2, 1);
        INSERT INTO ticket_task VALUES (1, 1, 1, 1), (2, 2, 2, 2), (3, 3, 1, 1);
        """
    )
    conn.commit()
    conn.close()


def _admin(s):
    return s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))


@pytest.fixture()
def world(control_db):
    from analystos.services.workspaces import add_member, create_workspace

    upload = Path(get_settings().upload_dir)
    upload.mkdir(parents=True, exist_ok=True)
    fname = f"glossary-{new_id('t')}.db"
    _make(upload / fname)
    with session_scope() as s:
        admin = _admin(s)
        ws = create_workspace(s, admin, name=f"Glossary {new_id('t')}", objective="SLA breaches by group", autonomy_level=3)
        s.flush()
        add_member(s, admin, ws.id, "analyst@analystos.local", "analyst")
        src = Source(id=new_id("src"), workspace_id=ws.id, kind="sqlite", name="Desk", config={"path": fname},
                     status="registered", execution_mode="staged")
        s.add(src)
        ids = {"ws": ws.id, "src": src.id, "admin": admin.id, "db": upload / fname}
    yield ids
    from analystos.staging.loader import StagingLoader

    StagingLoader(get_settings()).drop_source(ids["src"])
    ids["db"].unlink(missing_ok=True)


def _user(uid: str) -> User:
    with session_scope() as s:
        u = s.get(User, uid)
        s.expunge(u)
        return u


def _login(api, email: str) -> dict:
    r = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _pending(ws: str, kind: str) -> dict[str, KnowledgeSuggestion]:
    with session_scope() as s:
        rows = list(s.scalars(select(KnowledgeSuggestion).where(KnowledgeSuggestion.workspace_id == ws,
                                                                KnowledgeSuggestion.kind == kind,
                                                                KnowledgeSuggestion.status == "pending")))
        s.expunge_all()
        return {r.subject: r for r in rows}


def _column(ws: str, table: str, name: str) -> SourceColumn:
    with session_scope() as s:
        c = s.scalar(select(SourceColumn).join(SourceAsset, SourceAsset.id == SourceColumn.asset_id)
                     .where(SourceAsset.workspace_id == ws, SourceAsset.name == table, SourceColumn.name == name))
        s.expunge(c)
        return c


def test_crawl_suggests_accept_links_reject_sticks_and_answers_write_user_text(world):
    from fastapi.testclient import TestClient

    from analystos.api.app import app
    from analystos.services.crawler import crawl_source
    from analystos.services.sources import discover_source, select_assets

    admin = _user(world["admin"])
    discover_source(admin, world["src"])
    select_assets(admin, world["src"], ["ticket", "ticket_task", "assignment_group", "service_offering"])
    out = crawl_source(admin, world["src"], mode="full", profile=True)
    assert out["stats"]["facets"]["glossary_scan"]["status"] == "ok"
    assert out["stats"]["glossary_suggestions"] >= 2 and out["stats"]["model_calls"] == 0

    terms = _pending(world["ws"], "glossary_term")
    pri = terms["glossary:priority"]
    assert pri.fields["placeholder"]["value"] is True and pri.fields["body"]["value"].startswith("Priority codes: 1 = ?")
    assert pri.fields["evidence"]["value"][0]["where"] == "ticket.priority"
    assert pri.fields["mapped_columns"]["value"][0].endswith(".ticket.priority")
    assert "glossary:made-sla" in terms and "glossary:service-offering" in terms
    assert "glossary:assignment-group" not in terms and out["stats"]["code_value_columns"] >= 1
    state = terms["glossary:state"]  # codes with gaps: read through the gateway, never guessed
    assert state.fields["body"]["value"] == "State codes: 1 = ?; 2 = ?; 6 = ?; 7 = ?."
    assert "glossary:category" not in terms  # words describe themselves
    questions = _pending(world["ws"], "description_question")
    flag_q = next(q for s, q in questions.items() if s.endswith(":u_flag2"))
    assert flag_q.fields["question"]["value"] == "What does “u_flag2” in Ticket mean?"

    with TestClient(app) as api:
        owner = _login(api, "admin@analystos.local")
        analyst = _login(api, "analyst@analystos.local")
        # a skeleton cannot be approved as it stands; edited, it becomes a trusted glossary term
        r = api.post(f"/api/workspaces/{world['ws']}/knowledge/suggestions/review", headers=owner,
                     json={"decisions": [{"id": pri.id, "action": "approve"}]})
        assert r.json()["errors"] == [{"id": pri.id, "error": "definition_required"}]
        r = api.post(f"/api/workspaces/{world['ws']}/knowledge/suggestions/review", headers=owner, json={"decisions": [
            {"id": pri.id, "action": "edit", "fields": {"body": "1 = Critical, 2 = High, 3 = Moderate, 4 = Low, 5 = Planning.",
                                                         "synonyms": ["urgency level", "P-level"]}},
            {"id": terms["glossary:made-sla"].id, "action": "reject", "reason": "Not a business term here"},
            {"id": flag_q.id, "action": "edit", "fields": {"description": "Set when the caller is a VIP."}}]})
        body = r.json()
        assert body["errors"] == [] and len(body["approved"]) == 2 and body["rejected"][0]["path"] is None
        assert "glossary term" in body["approved"][0]["catalog"] and body["approved"][0]["path"] is None
        with session_scope() as s:
            entry = s.scalar(select(ContextEntry).where(ContextEntry.workspace_id == world["ws"], ContextEntry.name == "Priority"))
            assert entry.kind == "term" and entry.origin == "agent" and entry.trusted
            assert entry.body.startswith("1 = Critical") and entry.synonyms == ["urgency level", "P-level"]
            assert entry.mapped_columns[0].endswith(".ticket.priority")
            entry_id = entry.id
        flag = _column(world["ws"], "ticket", "u_flag2")
        assert flag.description == "Set when the caller is a VIP." and flag.description_origin == "user"
        assert _column(world["ws"], "ticket", "priority").semantics["glossary"]["term_id"] == entry_id

        # the next crawl links the column through the glossary facet (the immediate link is cleared first)
        with session_scope() as s:
            c = s.get(SourceColumn, _column(world["ws"], "ticket", "priority").id)
            c.semantics = {k: v for k, v in (c.semantics or {}).items() if k != "glossary"}
        again = crawl_source(admin, world["src"], mode="full", profile=True)
        assert again["stats"]["glossary_links"] >= 1
        link = _column(world["ws"], "ticket", "priority").semantics["glossary"]
        assert link["term_id"] == entry_id and link["score"] == 1.0
        # decided candidates are never proposed again; the answered column is not asked about again
        assert "glossary:made-sla" not in _pending(world["ws"], "glossary_term")
        assert "glossary:priority" not in _pending(world["ws"], "glossary_term")
        assert not any(s.endswith(":u_flag2") for s in _pending(world["ws"], "description_question"))
        assert flag.description == _column(world["ws"], "ticket", "u_flag2").description  # a crawl never overwrites it

        # the on-demand scan: editors only; counts; idempotent
        assert api.post(f"/api/workspaces/{world['ws']}/knowledge/glossary/scan", headers=analyst, json={}).status_code == 403
        r = api.post(f"/api/workspaces/{world['ws']}/knowledge/glossary/scan", headers=owner, json={"use_model": False})
        assert r.status_code == 200, r.text
        scan = r.json()
        assert scan["glossary_terms"] == 0 and scan["description_questions"] == 0 and scan["skipped_decided"] >= 2
        summary = api.get(f"/api/workspaces/{world['ws']}/knowledge/suggestions/summary", headers=analyst).json()
        assert summary["questions"] == summary["pending"].get("glossary_term", 0) + summary["pending"].get("description_question", 0)
        assert summary["questions"] >= 1


def test_unanswered_ask_question_becomes_a_define_draft(world):
    from analystos.services.ask import ask_in_thread, create_thread
    from analystos.services.sources import discover_source, select_assets

    admin = _user(world["admin"])
    discover_source(admin, world["src"])
    select_assets(admin, world["src"], ["ticket", "assignment_group"])
    with session_scope() as s:
        thread = create_thread(s, s.get(User, admin.id), world["ws"])

    def clarify(ctx, question, parameters=None):
        return {"status": "clarify", "missing": [], "explanation": "Say what to measure."}

    turn = ask_in_thread(admin, thread["id"], "How big is the backlog of P1 tickets for jane.doe@example.com?", ask_fn=clarify)
    assert turn["status"] == "clarify"
    drafts = _pending(world["ws"], "glossary_term")
    backlog = drafts["glossary:backlog"]
    assert backlog.title == "Define “backlog”?" and backlog.origin == "glossary.ask"
    ev = backlog.fields["evidence"]["value"][0]
    assert ev["kind"] == "ask" and ev["turn_id"] == turn["id"] and "example.com" not in ev["question"]
    # "P1" is already a glossary synonym (the ITSM domain pack's "Priority 1 (P1)"): not suggested again
    assert "glossary:p-1" not in drafts and not any("jane" in s or "example" in s for s in drafts)
    # the same question again adds nothing: pending candidates are not proposed twice
    ask_in_thread(admin, thread["id"], "How big is the backlog?", ask_fn=clarify)
    assert len(_pending(world["ws"], "glossary_term")) == len(drafts)
