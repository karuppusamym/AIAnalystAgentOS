"""P4-K05/K07/K08 against Postgres: BM25 + vector retrieval with one hop over links, the context
compiler over pack sections (receipts, external and memory sections), the retrieval benchmark, the
knowledge review queue (batch approve / edit / reject -> revision, negative knowledge, never over
owner content), crawler enrichment drafts with per-field provenance, the learning loop (accepted
finding -> draft Attested Computation, approved KPI, feedback), the API, and migration 0024."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import KnowledgeIndexState, KnowledgeSuggestion, LineageEdge, User
from analystos.knowledge import index, okf, store
from analystos.knowledge.entries import render_entry

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"


def _admin(s):
    from analystos.core.config import get_settings

    return s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))


@pytest.fixture()
def workspace(control_db):
    from analystos.services.workspaces import add_member, create_workspace

    with session_scope() as s:
        admin = _admin(s)
        ws = create_workspace(s, admin, name=f"k05 {new_id('t')}", objective="SLA breaches")
        s.flush()
        add_member(s, admin, ws.id, "approver@analystos.local", "approver")
        add_member(s, admin, ws.id, "analyst@analystos.local", "analyst")
        return ws.id


def _commit(ws: str, files: dict[str, str]) -> None:
    with session_scope() as s:
        store.commit(s, store.workspace_pack(s, ws), {p: b.encode() for p, b in files.items()}, author="human:test",
                     reason="test", origin="user", merge=True)


# ------------------------------------------------------------------------------------ K05 retrieval
def test_bm25_uses_corpus_statistics_and_one_hop_follows_links(workspace):
    _commit(workspace, {
        "glossary/zebra.md": render_entry(kind="term", name="Zebra queue",
                                          body="Zebra tickets wait in the zebra queue. See [triage](/rules/triage.md)."),
        "rules/triage.md": render_entry(kind="rule", name="Triage", body="Every item is sorted within one hour."),
        "glossary/common.md": render_entry(kind="term", name="Ticket", body="A ticket is a ticket is a ticket."),
    })
    with session_scope() as s:
        pack = store.workspace_pack(s, workspace)
        state = s.get(KnowledgeIndexState, f"pack:{pack.id}")
        assert state.value["bm25"]["sections"] >= 3 and state.value["bm25"]["tokens"] > 0
        hits = index.retrieve(s, workspace, "zebra ticket", pack_ids=[pack.id])
        assert hits[0].path == "glossary/zebra.md"  # the rare term outweighs the common one
        assert hits[0].lexical_share == 1.0 and hits[0].lexical_rank == 1
        plain = index.retrieve(s, workspace, "zebra queue", pack_ids=[pack.id], limit=10)
        hop = index.retrieve(s, workspace, "zebra queue", pack_ids=[pack.id], limit=10, hop=True)
        triage = [h for h in hop if h.path == "rules/triage.md"]
        assert triage and triage[0].via == "glossary/zebra.md"
        assert all(h.via is None for h in plain)
        old = index.retrieve(s, workspace, "zebra ticket", pack_ids=[pack.id], lexical="ts_rank_cd")
        assert old and old[0].lexical_share is None  # the K01 leg stays selectable for the benchmark


def test_retrieval_benchmark_before_and_after(workspace):
    """The labelled benchmark (tests/fixtures/okf/context-benchmark.json). Set
    ANALYSTOS_CONTEXT_BENCH_OUT=<file.json> to write the report (evidence input)."""
    from analystos.context import benchmark

    data = benchmark.load()
    with session_scope() as s:
        pack_id = benchmark.install(s, workspace, data)
    with session_scope() as s:
        report = benchmark.run(s, workspace, [pack_id], data["questions"])
        report["paraphrase_platform_pack"] = benchmark.paraphrase_run(s, workspace, store.platform_pack(s, create=False).id)
        report["embedding"] = index.index_embedding(s)
    out = os.environ.get("ANALYSTOS_CONTEXT_BENCH_OUT")
    if out:
        with open(out, "w") as f:
            json.dump(report, f, indent=2, default=str)
    cfg = report["configs"]
    assert report["questions"] == 32
    # K05 acceptance: the compiler's section retrieval is at least as good as T03's whole-document overlap
    assert cfg["compiler_k05"]["all"]["section_mrr"] >= cfg["compiler_t03"]["all"]["section_mrr"]
    assert cfg["compiler_k05"]["all"]["doc_mrr"] >= cfg["compiler_t03"]["all"]["doc_mrr"]
    assert cfg["index_bm25_hop"]["hop"]["doc_recall_at_3"] >= cfg["index_bm25"]["hop"]["doc_recall_at_3"]


def test_compiler_over_pack_sections_with_receipts_external_and_episodes(workspace):
    from analystos.artifacts.registry import save_artifact
    from analystos.context.compiler import compile_context, load_knowledge
    from analystos.context.service import add_entry
    from analystos.contracts.platform import PurposeProfile

    _commit(workspace, {"glossary/breach-window.md": okf.render_document(
        {"type": "Glossary Term", "title": "Breach window", "analystos": {"kind": "term", "mapped_columns": ["sn.incident.made_sla"]}},
        "# Definition\n\nThe breach window is the time between the SLA target and the actual resolution.\n\n"
        "# Reporting\n\nBreach windows are reported in hours per priority.")})
    run_id = new_id("run")
    with session_scope() as s:
        add_entry(s, workspace_id=workspace, kind="episode", name="Run old: breach window by priority",
                  body="The breach window was longest for priority 3.", origin="agent")
        save_artifact(s, workspace_id=workspace, run_id=run_id, type_="context_package", name="Context package",
                      content={"external": [{"provider": "mcp", "status": "MATCHED", "items": [
                          {"title": "Breach window (Atlas)", "heading": "", "text": "Atlas: breach window counts business hours.",
                           "path": "bundle/concepts/breach-window.md", "anchor": "definition", "sha256": "d" * 64}]}]})
    sections = ["glossary", "external", "episodes"]
    with session_scope() as s:
        items = load_knowledge(s, workspace, sections, run_id=run_id, query="how are breach windows reported per priority")
    pack_items = [i for i in items if i.path and i.path.startswith("glossary/breach-window")]
    assert {i.anchor for i in pack_items} >= {"definition", "reporting"} and all(i.retrieval_rank for i in pack_items)
    profile = PurposeProfile(sections=sections, max_chars=20_000)
    c = compile_context("planning", profile, objective="how are breach windows reported per priority", required={"q": 1},
                        knowledge=items)
    names = [i["name"] for i in c.body["glossary"]]
    assert "Breach window § Reporting" in names
    r = next(r for r in c.receipts if r.get("anchor") == "reporting")
    assert r["path"] == "glossary/breach-window.md" and len(r["sha256"]) == 64 and r["section_sha256"]
    assert c.body["external"][0]["trusted"] is False
    assert any(x["section"] == "external" and x["path"] == "bundle/concepts/breach-window.md" for x in c.receipts)
    assert c.body["episodes"][0]["name"].startswith("Run old")


# ------------------------------------------------------------------------------------ K07 review queue
def _propose_terms(ws: str) -> list[str]:
    from analystos.knowledge.suggestions import field, propose

    ids = []
    with session_scope() as s:
        for title, body, conf in (("Resolution SLA", "Time allowed to resolve an incident.", 0.55),
                                  ("Escalation", "Moving an incident to a higher support tier.", 0.4),
                                  ("Zombie ticket", "A ticket nobody owns and nobody closes.", 0.3)):
            row = propose(s, ws, kind="term", subject=f"term:{title.lower()}", title=title,
                          fields={"body": field(body, conf, source="model", model="m1", purpose="metadata_enrichment"),
                                  "synonyms": field([title.split()[0]], 0.9, source="rule")},
                          origin="crawler.enrichment", proposed_by="model:m1", batch="crawl_1")
            ids.append(row.id)
    return ids


def test_review_queue_batch_approve_edit_reject_writes_one_revision(workspace):
    from fastapi.testclient import TestClient

    from analystos.api.app import app
    from analystos.context.compiler import load_knowledge

    ids = _propose_terms(workspace)
    with TestClient(app) as api:
        owner = _login(api, "admin@analystos.local")
        analyst = _login(api, "analyst@analystos.local")
        queue = api.get(f"/api/workspaces/{workspace}/knowledge/suggestions", headers=analyst).json()
        assert {q["id"] for q in queue} == set(ids)
        queue.sort(key=lambda q: ids.index(q["id"]))
        first = queue[0]["fields"]
        assert first["body"]["confidence"] == 0.55 and first["body"]["provenance"]["model"] == "m1"
        assert first["synonyms"]["provenance"]["source"] == "rule" and queue[0]["confidence"] == 0.55
        decisions = {"decisions": [{"id": ids[0], "action": "approve"},
                                   {"id": ids[1], "action": "edit", "fields": {"body": "Handing an incident to tier 2 or 3."}},
                                   {"id": ids[2], "action": "reject", "reason": "Not a term we use."}]}
        assert api.post(f"/api/workspaces/{workspace}/knowledge/suggestions/review", headers=analyst,
                        json=decisions).status_code == 403  # editors decide
        with session_scope() as s:
            before = store.workspace_pack(s, workspace).head_revision
        r = api.post(f"/api/workspaces/{workspace}/knowledge/suggestions/review", headers=owner, json=decisions)
        assert r.status_code == 200, r.text
        out = r.json()
        assert len(out["approved"]) == 2 and len(out["rejected"]) == 1 and out["errors"] == []
        assert out["revision"] == (before or 0) + 1  # one revision for the whole batch
        again = api.post(f"/api/workspaces/{workspace}/knowledge/suggestions/review", headers=owner, json=decisions).json()
        assert {e["error"] for e in again["errors"]} == {"not_pending"} and again["revision"] is None
    with session_scope() as s:
        pack = store.workspace_pack(s, workspace)
        files = store.revision_files(s, pack)
        doc = okf.parse_document("glossary/escalation.md", files["glossary/escalation.md"])
        review = doc.extension["review"]
        assert doc.trust_tier == "human-reviewed" and doc.status == "stable"
        assert review["fields"]["body"]["provenance"]["source"] == "human"  # edited field
        assert review["fields"]["synonyms"]["provenance"]["source"] == "rule"  # untouched field keeps its provenance
        assert "tier 2 or 3" in doc.body
        negative = [p for p in files if p.startswith("negative/")]
        assert len(negative) == 1 and "Not a term we use" in files[negative[0]].decode()
        rows = {r.id: r for r in s.scalars(select(KnowledgeSuggestion).where(KnowledgeSuggestion.id.in_(ids)))}
        assert [rows[i].status for i in ids] == ["approved", "approved", "rejected"]
        assert {rows[i].revision for i in ids} == {pack.head_revision}
        # the rejection is negative knowledge later prompts see
        items = load_knowledge(s, workspace, ["glossary", "negative_knowledge"], query="zombie ticket nobody owns")
        assert any(i.section == "negative_knowledge" and "Zombie ticket" in i.name for i in items)
        assert any(i.section == "glossary" and i.path == "glossary/resolution-sla.md" for i in
                   load_knowledge(s, workspace, ["glossary"], query="resolution SLA time allowed"))
    # the suggester never proposes rejected content again
    from analystos.knowledge.suggestions import field, propose

    with session_scope() as s:
        assert propose(s, workspace, kind="term", subject="term:zombie ticket", title="Zombie ticket",
                       fields={"body": field("A ticket nobody owns and nobody closes.", 0.9, source="model")},
                       origin="crawler.enrichment", proposed_by="model:m2") is None


def test_drafts_never_overwrite_owner_content(workspace):
    from analystos.knowledge.suggestions import field, propose, review

    _commit(workspace, {"glossary/backlog.md": render_entry(kind="term", name="Backlog", body="Owner definition.")})
    with session_scope() as s:
        assert propose(s, workspace, kind="term", subject="term:backlog", title="Backlog",
                       fields={"body": field("Model definition.", 0.5, source="model")}, origin="crawler.enrichment",
                       proposed_by="model:m1") is None  # skipped at proposal
        row = propose(s, workspace, kind="term", subject="term:backlog2", title="Backlog two",
                      fields={"body": field("Another.", 0.5, source="model")}, origin="crawler.enrichment",
                      proposed_by="model:m1", path="glossary/backlog-two.md")
        rid = row.id
    _commit(workspace, {"glossary/backlog-two.md": render_entry(kind="term", name="Backlog two", body="Owner wrote it meanwhile.")})
    with session_scope() as s:
        out = review(s, workspace, _admin(s), [{"id": rid, "action": "approve"}])
        assert out["errors"] == [{"id": rid, "error": "owner_content_exists", "path": "glossary/backlog-two.md"}]
        assert b"Owner wrote it meanwhile" in store.revision_files(s, store.workspace_pack(s, workspace))["glossary/backlog-two.md"]


# ------------------------------------------------------------------------------------ K07 crawler enrichment
def test_crawler_enrichment_queues_drafts_and_avoids_rejected_text(workspace):
    from analystos.db.models import Source, SourceAsset
    from analystos.knowledge.suggestions import review
    from analystos.services.crawler import _Crawl
    from analystos.skills.catalog import TableSemantics

    with session_scope() as s:
        src = Source(id=new_id("src"), workspace_id=workspace, kind="postgres", name="db", config={})
        s.add(src)
        s.flush()
        asset = SourceAsset(id=new_id("ast"), source_id=src.id, workspace_id=workspace, schema_name="public", name="tbl_x1",
                            source_name="tbl_x1", description=None, description_origin=None)
        s.add(asset)
        asset_id = asset.id
    sem = TableSemantics(key="public.tbl_x1", business_name="Tbl X1", entity="x1", domain="unknown", role="unknown",
                         grain="one row per x1", description="", confidence=0.3)
    sent: list[dict] = []

    class Router:
        def __init__(self, text):
            self.text = text

        def complete_json(self, purpose, system, user, *, ctx=None, max_tokens=None):
            sent.append(json.loads(user))
            return SimpleNamespace(data={"tables": [{"key": "public.tbl_x1", "business_name": "Shipment exceptions",
                                                     "description": self.text, "confidence": 0.95}]}, model="m-test")

    crawl = _Crawl.__new__(_Crawl)
    crawl.source = SimpleNamespace(workspace_id=workspace)
    crawl.run = SimpleNamespace(id="crawl_test")
    crawl.stats = {"model_calls": 0}
    crawl.log = SimpleNamespace(stage=lambda *a, **k: None)
    crawl._call_ctx = lambda: None
    batch = [{"key": "public.tbl_x1", "name": "tbl_x1"}]
    by_key = {"public.tbl_x1": {"asset_id": asset_id, "semantics": sem}}
    text = "Shipments that missed their promised delivery date, one row per exception."
    assert crawl._enrich_batch(Router(text), batch, by_key) == 1
    with session_scope() as s:
        row = s.scalar(select(KnowledgeSuggestion).where(KnowledgeSuggestion.subject == f"asset:{asset_id}"))
        f = row.fields
        assert row.kind == "table_description" and row.origin == "crawler.enrichment" and row.proposed_by == "model:m-test"
        assert f["description"]["confidence"] == 0.9  # the model's 0.95 is capped
        assert f["description"]["provenance"] == {"source": "model", "model": "m-test", "purpose": "metadata_enrichment",
                                                   "prompt_version": "crawl-enrich-v2", "crawl_run": "crawl_test"}
        assert f["description"]["before"] == {"value": None, "origin": None}
        assert f["role"]["provenance"]["source"] == "rule" and f["role"]["confidence"] == 0.3
        assert s.get(SourceAsset, asset_id).description == text  # placeholder filled, unreviewed
        out = review(s, workspace, _admin(s), [{"id": row.id, "action": "reject", "reason": "wrong table"}])
        assert out["rejected"][0]["catalog"] == "catalog restored"
        a = s.get(SourceAsset, asset_id)
        assert (a.description, a.description_origin, a.reviewed) == (None, None, False)
    # the next crawl tells the model what was rejected and never applies it again
    assert crawl._enrich_batch(Router(text), batch, by_key) == 0
    assert sent[-1]["tables"][0]["rejected"] == [text.lower()]
    other = "Exceptions raised for late shipments."
    assert crawl._enrich_batch(Router(other), batch, by_key) == 1
    with session_scope() as s:
        row = s.scalar(select(KnowledgeSuggestion).where(KnowledgeSuggestion.subject == f"asset:{asset_id}",
                                                         KnowledgeSuggestion.status == "pending"))
        out = review(s, workspace, _admin(s), [{"id": row.id, "action": "approve"}])
        assert out["approved"][0]["catalog"] == "catalog updated"
        a = s.get(SourceAsset, asset_id)
        assert a.description == other and a.reviewed  # a human reviewed it: crawls never touch it again
        doc = okf.parse_document(row.path, store.revision_files(s, store.workspace_pack(s, workspace))[row.path])
        assert doc.type == "Table" and doc.extension["review"]["fields"]["description"]["provenance"]["model"] == "m-test"


# ------------------------------------------------------------------------------------ K08 learning loop
def _finding(ws: str, *, verified: bool = True) -> str:
    from analystos.db.models import Experiment, Hypothesis, Insight, QueryExecution

    run_id = new_id("run")
    with session_scope() as s:
        q = QueryExecution(id=new_id("q"), workspace_id=ws, run_id=run_id, actor="agent:investigator",
                           sql="select priority, avg(made_sla::int) from sn.incident group by 1", status="ok",
                           result_hash="e" * 64)
        h = Hypothesis(id=new_id("hyp"), workspace_id=ws, run_id=run_id, code="H-1", statement="Priority drives SLA breach",
                       status="supported")
        s.add_all([q, h])
        s.flush()
        s.add(Experiment(id=new_id("exp"), workspace_id=ws, run_id=run_id, hypothesis_id=h.id, method="segment_compare",
                         result={"test": "chi_square", "n": 5000, "p_value": 0.0001, "p_adjusted": 0.0004,
                                 "effect_size": 0.31, "effect_label": "cramers_v"}, query_ids=[q.id]))
        ins = Insight(id=new_id("ins"), workspace_id=ws, run_id=run_id, hypothesis_id=h.id, code="I-1",
                      title="P1 incidents breach SLA less often", finding="P1 incidents breach SLA 3x less often than P3.",
                      confidence=0.8, verified=verified, status="verified" if verified else "failed_verification")
        s.add(ins)
        return ins.id


def test_accepted_finding_becomes_a_draft_attested_computation(workspace):
    from fastapi.testclient import TestClient

    from analystos.api.app import app
    from analystos.knowledge import attested

    insight_id = _finding(workspace)
    with TestClient(app) as api:
        analyst = _login(api, "analyst@analystos.local")
        r = api.post(f"/api/insights/{insight_id}/outcome", headers=analyst, json={"signal": "accept"})
        assert r.status_code == 200, r.text
        draft_id = r.json()["knowledge_draft"]
        assert draft_id
        with session_scope() as s:
            row = s.get(KnowledgeSuggestion, draft_id)
            comp = row.fields["computation"]["value"]
            assert row.kind == "attested_computation" and row.status == "pending" and row.subject == f"insight:{insight_id}"
            assert comp["query_hash"] == attested.sql_hash("select priority, avg(made_sla::int) from sn.incident group by 1")
            assert comp["result_hash"] == "e" * 64 and comp["q_value"] == 0.0004
            assert comp["effect_size"] == {"value": 0.31, "label": "cramers_v"} and comp["verified_by"] == attested.REV
            assert row.fields["stale_after"]["value"] and attested.missing(comp) == []
            lineage = s.scalars(select(LineageEdge).where(LineageEdge.from_id == draft_id)).all()
            assert [(e.relation, e.to_type, e.to_id) for e in lineage] == [("derived_from", "insight", insight_id)]
        owner = _login(api, "admin@analystos.local")
        out = api.post(f"/api/workspaces/{workspace}/knowledge/suggestions/review", headers=owner,
                       json={"decisions": [{"id": draft_id, "action": "approve"}]}).json()
        assert out["approved"][0]["path"] == f"findings/{insight_id.replace('_', '-')}.md"
    with session_scope() as s:
        data = store.revision_files(s, store.workspace_pack(s, workspace))[out["approved"][0]["path"]]
        doc = okf.parse_document(out["approved"][0]["path"], data)
        assert attested.validate(doc) == [] and doc.status == "stable"
        assert [v["by"] for v in okf.verified_entries(doc.frontmatter)] == [attested.REV, f"human:{_admin(s).id}"]
        assert okf.parse_instant(doc.frontmatter["stale_after"]) is not None
    unverified = _finding(workspace, verified=False)
    with TestClient(app) as api:
        r = api.post(f"/api/insights/{unverified}/outcome", headers=_login(api, "analyst@analystos.local"), json={"signal": "accept"})
        assert r.json()["knowledge_draft"] is None  # only verified findings are attested


def test_approved_kpi_and_user_feedback_become_drafts(workspace):
    from analystos.contracts.semantic import DialectExpression, SemanticMetricDef
    from analystos.db.models import Feedback, Insight
    from analystos.knowledge.learning import draft_from_feedback
    from analystos.semantic import service as semantic

    with session_scope() as s:
        admin = _admin(s)
        defn = SemanticMetricDef(name="breach_rate", expressions=[DialectExpression(expression="avg(case when made_sla then 0 else 1 end)")],
                                 description="Share of incidents that missed their SLA.", source_columns=["sn.incident.made_sla"])
        semantic.propose_metric(s, workspace, defn, proposed_by=admin.id, via="user")
    with session_scope() as s:
        approver = s.scalar(select(User).where(User.email == "approver@analystos.local"))
        assert semantic.decide_metric(s, workspace, "breach_rate", approver, approve=True).status == "approved"
    with session_scope() as s:
        row = s.scalar(select(KnowledgeSuggestion).where(KnowledgeSuggestion.workspace_id == workspace,
                                                         KnowledgeSuggestion.origin == "learning.metric"))
        assert row.kind == "metric" and row.subject == "metric:breach_rate@v1" and row.path == "metrics/breach-rate.md"
        assert "missed their SLA" in row.fields["body"]["value"] and row.fields["body"]["provenance"]["approval_id"]
        assert row.fields["mapped_columns"]["value"] == ["sn.incident.made_sla"]
        ins = Insight(id=new_id("ins"), workspace_id=workspace, run_id="run_fb", code="I-9", title="Weekend spike",
                      finding="Weekend incidents spike.", status="rejected")
        s.add(ins)
        drafts = {}
        for kind, text in (("add_context", "Weekend tickets are handled by the on-call team only."),
                           ("redirect", "focus on priority 1 incidents"), ("reject_finding", "The spike is a data load artefact."),
                           ("question", "why?")):
            fb = Feedback(id=new_id("fb"), workspace_id=workspace, run_id="run_fb", user_id=_admin(s).id, kind=kind, text=text,
                          data={"interpretation": {"filters": [{"column": "priority", "op": "=", "value": "1"}]}})
            s.add(fb)
            s.flush()
            d = draft_from_feedback(s, fb, objective="SLA breaches", target=ins if kind == "reject_finding" else None)
            drafts[kind] = d
        assert drafts["question"] is None
        assert (drafts["add_context"].kind, drafts["redirect"].kind, drafts["reject_finding"].kind) == ("note", "note", "negative")
        assert "priority = 1" in drafts["redirect"].fields["body"]["value"]
        assert drafts["reject_finding"].fields["body"]["provenance"]["kind"] == "reject_finding"


# ------------------------------------------------------------------------------------ API
def _login(api, email: str) -> dict:
    r = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_context_preview_and_run_receipts_api(workspace):
    from fastapi.testclient import TestClient

    from analystos.api.app import app
    from analystos.db.models import AnalysisRun, ModelCall

    _commit(workspace, {"glossary/reassignment.md": render_entry(kind="term", name="Reassignment",
                                                                  body="Moving an incident to another assignment group.")})
    run_id = new_id("run")
    with session_scope() as s:
        s.add(AnalysisRun(id=run_id, workspace_id=workspace, objective="reassignments", status="COMPLETED",
                          requested_by=_admin(s).id))
        s.add(ModelCall(workspace_id=workspace, run_id=run_id, purpose="planning", profile="p", provider="x", model="m",
                        status="ok", context_receipts=[{"id": "kdoc#definition", "section": "glossary", "path": "glossary/x.md"}]))
        s.add(ModelCall(workspace_id=workspace, run_id=run_id, purpose="summarization", profile="p", provider="x", model="m",
                        status="ok"))
    with TestClient(app) as api:
        viewer = _login(api, "analyst@analystos.local")
        r = api.post(f"/api/workspaces/{workspace}/knowledge/context", headers=viewer,
                     json={"purpose": "planning", "question": "why are incidents reassigned to another group"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert any(x.get("path") == "glossary/reassignment.md" for x in body["receipts"])
        assert "catalog" not in body["body"] and body["body"]["episodes"] == "NO_MATCH"
        receipts = api.get(f"/api/runs/{run_id}/context-receipts", headers=viewer).json()
        assert [(x["purpose"], x["receipts"][0]["path"]) for x in receipts] == [("planning", "glossary/x.md")]
        other = _login(api, "approver@analystos.local")
        assert api.get(f"/api/workspaces/{new_id('ws')}/knowledge/suggestions", headers=other).status_code in (403, 404)


# ------------------------------------------------------------------------------------ migration
def test_migration_0024_applies_and_reverts(control_db):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    from analystos.core.config import REPO_ROOT

    base, name = control_db.rsplit("/", 1)
    mig_db = f"{name}_mig24"
    admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        c.execute(text(f"CREATE DATABASE {mig_db}"))
    url = f"{base}/{mig_db}"
    engine = create_engine(url)
    try:
        with engine.begin() as c:
            c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "0024")
        assert "knowledge_suggestion" in inspect(engine).get_table_names()
        command.downgrade(cfg, "0018")
        assert "knowledge_suggestion" not in inspect(engine).get_table_names()
        command.upgrade(cfg, "0024")
        cols = {c["name"] for c in inspect(engine).get_columns("knowledge_suggestion")}
        assert {"fields", "confidence", "origin", "proposed_by", "status", "content_hash", "revision"} <= cols
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{mig_db}'"))
            c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        admin.dispose()
