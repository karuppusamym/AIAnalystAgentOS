"""Domain assistance receives only screened nonsensitive metadata and creates a review draft."""
import json
from types import SimpleNamespace

from analystos.connectors.base import DiscoveredAsset, DiscoveredColumn
from analystos.db.base import session_scope
from analystos.db.models import KnowledgeDocument, SourceAsset, SourceColumn
from analystos.knowledge.suggestions import _domain_keyword_error, field, render, reviewed_domain_keywords
from analystos.services import crawler
from analystos.skills.catalog import infer_table_semantics


def test_only_full_populations_confirm_classification():
    assert crawler.profile_is_full_population(None)
    assert crawler.profile_is_full_population({"sampling_method": "full", "truncated": False})
    assert not crawler.profile_is_full_population({"sampling_method": "full", "truncated": True})
    assert not crawler.profile_is_full_population({"sampling_method": "tablesample", "truncated": False})
    assert not crawler.profile_is_full_population({"sampling_method": "time_window", "truncated": False})


def test_approved_candidate_carries_reviewed_keyword():
    row = SimpleNamespace(id="ksug_1", kind="domain_candidate", title="Review domain for data.t_01",
                          subject="asset:ast_1", origin="crawler.domain", proposed_by="model:test", confidence=0.6)
    document = render(row, {"domain": field("sales", 0.6), "body": field("Order name suggests sales", 0.6),
                            "keyword": field("orders", 1.0, source="human")},
                      "reviewer", "2026-09-27T00:00:00Z").decode()
    assert "Reviewed domain: sales. Keyword: orders." in document
    assert "domain_keyword: orders" in document
    assert "candidate_domain: sales" in document


def test_keyword_review_requires_human_safe_metadata_and_workspace_isolation(sqlite_db, monkeypatch):
    from analystos.knowledge import suggestions

    monkeypatch.setattr(suggestions.store, "workspace_pack",
                        lambda session, workspace_id, **kwargs: SimpleNamespace(id="pack_1"))
    KnowledgeDocument.__table__.create(bind=sqlite_db().bind)
    row = SimpleNamespace(id="ksug_1", workspace_id="ws_1", subject="asset:ast_1")
    with session_scope() as s:
        s.add(SourceAsset(id="ast_1", source_id="src_1", workspace_id="ws_1", schema_name="data",
                          name="t_01", source_name="t_01", semantics={"domain": "generic"}, stats={}))
        s.add(SourceColumn(asset_id="ast_1", name="orders", ordinal=0, data_type="text", tags=[], profile={}, semantics={}))
        s.add(SourceColumn(asset_id="ast_1", name="customer_email", ordinal=1, data_type="text", tags=["pii"],
                           profile={}, semantics={}))
        s.add(KnowledgeDocument(id="doc_1", pack_id="pack_1", workspace_id="ws_1", revision=1,
                                path="notes/review.md", concept_id="review", type="Note", kind="note", title="rule",
                                status="stable", trust_tier="human-reviewed", sha256="a" * 64, size=1,
                                frontmatter={"analystos": {"candidate_domain": "sales", "domain_keyword": "orders",
                                                            "review": {"suggestion_id": "ksug_1", "decision": "approved"}}}, body=""))
    with session_scope() as s:
        fields = {"domain": field("sales", 0.6), "body": field("reason", 0.6)}
        assert _domain_keyword_error(s, row, fields) == "reviewed_keyword_required"
        fields["keyword"] = field("orders", 0.6, source="model")
        assert _domain_keyword_error(s, row, fields) == "reviewed_keyword_required"
        fields["keyword"] = field("email", 1, source="human")
        assert _domain_keyword_error(s, row, fields) == "keyword_not_in_safe_metadata"
        fields["keyword"] = field("orders", 1, source="human")
        assert _domain_keyword_error(s, row, fields) is None
        assert reviewed_domain_keywords(s, "ws_1") == {"sales": frozenset({"orders"})}
        assert reviewed_domain_keywords(s, "ws_2") == {}


def test_reviewed_keyword_reclassifies_only_on_next_inference():
    table = DiscoveredAsset(source_name="t_01", name="t_01", schema_name="data",
                            columns=[DiscoveredColumn(name="orders", data_type="text")])
    assert infer_table_semantics(table).domain == "generic"
    classified = infer_table_semantics(table, reviewed_keywords={"sales": frozenset({"orders"})})
    assert classified.domain == "sales"
    assert classified.confidence < 1.0


def test_domain_assist_proposes_without_changing_catalog(sqlite_db, monkeypatch):
    with session_scope() as s:
        s.add(SourceAsset(id="ast_1", source_id="src_1", workspace_id="ws_1", schema_name="data",
                          name="t_01", source_name="t_01", semantics={"domain": "generic"}, stats={}))
        s.add(SourceColumn(asset_id="ast_1", name="orders", ordinal=0, data_type="text", tags=[], profile={}, semantics={}))
        s.add(SourceColumn(asset_id="ast_1", name="customer_email", ordinal=1, data_type="text",
                           tags=["pii"], profile={}, semantics={}))
        s.add(SourceColumn(asset_id="ast_1", name="ssn", ordinal=2, data_type="text",
                           tags=[], profile={}, semantics={}))

    sent = []
    drafts = []
    proposed_domain = ["sales"]

    class Router:
        def mode(self, purpose):
            assert purpose == "domain_classification_assist"
            return "auto"

        def available(self, purpose):
            return True

        def complete_json(self, purpose, system, payload, **kwargs):
            sent.append(json.loads(payload))
            return SimpleNamespace(model="test-model", data={"tables": [
                {"key": "data.t_01", "domain": proposed_domain[0], "rationale": "The orders column is a sales signal.",
                 "confidence": 0.8}]})

    from analystos.knowledge import suggestions
    from analystos.runtime import context

    monkeypatch.setattr(suggestions, "rejected_values", lambda *args: set())
    monkeypatch.setattr(suggestions, "propose", lambda *args, **kwargs: drafts.append(kwargs) or object())
    monkeypatch.setattr(context, "workspace_call_ctx", lambda *args, **kwargs: SimpleNamespace())

    job = crawler._Crawl.__new__(crawler._Crawl)
    job.run = SimpleNamespace(options={"enrich": True}, id="crl_1")
    job.source = SimpleNamespace(workspace_id="ws_1")
    job.settings = SimpleNamespace(crawl=SimpleNamespace(enrichment_max_columns=12, enrichment_batch_tables=25))
    job.stats = {"model_calls": 0}
    job.log = SimpleNamespace(stage=lambda *args, **kwargs: None)
    job._suggest_domains(Router(), {"data.t_01": "ast_1"}, {"data.t_01"})

    assert sent and sent[0]["tables"][0]["columns"] == [{"name": "orders", "type": "text"}]
    assert "customer_email" not in json.dumps(sent) and '"ssn"' not in json.dumps(sent)
    assert drafts[0]["kind"] == "domain_candidate"
    assert drafts[0]["fields"]["domain"]["value"] == "sales"
    assert drafts[0]["fields"]["domain"]["confidence"] == 0.7
    with session_scope() as s:
        assert s.get(SourceAsset, "ast_1").semantics["domain"] == "generic"
    proposed_domain[0] = "made_up_domain"
    job._suggest_domains(Router(), {"data.t_01": "ast_1"}, {"data.t_01"})
    assert len(drafts) == 1
