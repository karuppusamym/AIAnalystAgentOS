"""Stream B token economy (P4-T04/T06, CTX-005): cache-stable layout, compact catalog text, the shared
context cache, compile-after-gate, batching of per-finding calls, the L0 key on the answering model,
cached-token pricing and the token-savings view."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from tests.fakes import FakeTransport, chat_json

from analystos.agents import common
from analystos.context import cache as context_cache
from analystos.context.compiler import KnowledgeItem, compile_context, render_catalog, render_stable
from analystos.contracts.platform import ContextSettings, LLMSettings, PlatformSettings, PurposeProfile
from analystos.contracts.policy import WorkspacePolicyDoc
from analystos.llm.cache import ResponseCache
from analystos.llm.config import load_models_config
from analystos.llm.router import CallContext, ModelRouter

pytestmark = pytest.mark.usefixtures("pinned_models_config")

KEY = {"OPENROUTER_API_KEY": "sk-test-000000000000000000000000"}


class Sink:
    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)

    def check_budget(self, ctx, purpose):
        return None


def _router(transport, platform, sink=None):
    return ModelRouter(transport=transport, sink=sink or Sink(), api_key_lookup=KEY.get, max_retries=0,
                       settings_provider=lambda: platform, cache=ResponseCache(None))


# ------------------------------------------------------------------ store: the local fallback honours the TTL
def test_local_cache_entries_expire_with_their_ttl():
    now = [100.0]
    cache = ResponseCache(None, clock=lambda: now[0])
    cache.set("k", {"v": 1}, 10)
    assert cache.get("k") == {"v": 1}
    now[0] += 11
    assert cache.get("k") is None  # the Redis TTL, now also in-process
    cache.set("z", {"v": 1}, 0)
    assert cache.get("z") is None  # ttl 0 = not cached


def test_counters_are_kept_per_field():
    cache = ResponseCache(None)
    cache.incr("s", "retrieval|planning|hits")
    cache.incr("s", "retrieval|planning|hits", 2)
    assert cache.counters("s") == {"retrieval|planning|hits": 3}


# ------------------------------------------------------------------ L0 key on the answering model
def test_response_cache_is_keyed_on_the_model_that_answered_not_the_candidate_list():
    assert ResponseCache.key("p", "m", {"x": 1}) == ResponseCache.key("p", ["m"], {"x": 1})
    calls = []
    transport = FakeTransport(chat=lambda p: calls.append(p["model"]) or chat_json({"ok": 1}, model=p["model"]))
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    first = PlatformSettings(llm=LLMSettings(cache_enabled=True))
    router = _router(transport, first)
    router.complete("planning", messages, ctx=CallContext(workspace_id="ws1"), json_output=True)
    answered = calls[-1]
    # another candidate order (a budget downgrade or an admin change of the tier): the stored answer of an
    # allowed model is still a hit
    models = load_models_config().profiles["reasoning_strong"].models
    swapped = PlatformSettings(llm=LLMSettings(cache_enabled=True, profile_models={"reasoning_strong": list(reversed(models))}))
    router.settings_provider = lambda: swapped
    again = router.complete("planning", messages, ctx=CallContext(workspace_id="ws1"), json_output=True)
    assert again.cached and again.model == answered and len(calls) == 1


# ------------------------------------------------------------------ price-table fallback: cached tokens are discounted
def test_price_table_cost_applies_the_cache_read_discount():
    config = load_models_config()
    model = "anthropic/claude-sonnet-5"
    full = config.estimate_cost(model, 1_000_000, 0)
    cached = config.estimate_cost(model, 1_000_000, 0, cached_input_tokens=1_000_000)
    assert config.cached_discount(model) == 0.9 and cached == pytest.approx(full * 0.1)
    assert config.cached_discount("openai/gpt-5.4-mini") == 0.5  # default for automatic prefix caching
    assert config.cached_saving(model, 1_000_000) == pytest.approx(full * 0.9)
    router = _router(FakeTransport(), PlatformSettings())
    cost, source = router._cost(model, {"prompt_tokens_details": {"cached_tokens": 1_000_000}}, 1_000_000, 0)
    assert source.startswith("price_table@") and cost == pytest.approx(cached)


# ------------------------------------------------------------------ compact, stable catalog text
def _catalog():
    return [{"asset": "sn.incident", "business_name": "Incident", "description": "One row per incident", "role": "fact",
             "grain": "incident", "row_count": 20000,
             "columns": [{"name": "priority", "type": "integer", "semantic_type": "categorical", "role": "dimension",
                          "values": ["1", "2"], "null_rate": 0.12, "distinct": 2, "meaning": "Priority"},
                         {"name": "made_sla", "type": "boolean", "semantic_type": "boolean", "role": "flag"},
                         {"name": "opened_at", "type": "timestamp", "semantic_type": "datetime", "distinct": 999,
                          "min": "2025-09-01T02:23:30", "max": "2026-08-31T10:00:00"}]},
            {"asset": "sn.change_request", "row_count": 900, "columns": ["risk", "state"]}]


def test_catalog_renders_as_sorted_lines_and_says_what_a_cap_dropped():
    text = render_catalog(_catalog(), [{"section": "catalog", "asset": "sn.incident", "columns": ["a", "b"], "reason": "column cap"},
                                       {"section": "catalog", "asset": "sn.problem", "reason": "not referenced"}])
    lines = text.splitlines()
    assert lines[0] == "TABLE sn.change_request [rows≈900]" and lines[1] == "  columns: risk, state"
    assert lines[2] == "TABLE sn.incident [fact, grain: incident, rows≈20,000] — Incident — One row per incident"
    assert lines[3] == "  made_sla (boolean) flag"  # columns by name
    assert lines[4] == "  opened_at (timestamp, datetime) range=2025-09-01..2026-08-31"
    assert lines[5] == "  priority (integer, categorical) dimension values=[1|2] nulls=12% distinct=2 — Priority"
    assert lines[6] == "  … 2 more columns not shown" and lines[7] == "… 1 more tables not shown: sn.problem"
    assert render_catalog(list(reversed(_catalog()))) == render_catalog(_catalog())  # order-independent: a stable prefix


def test_preamble_puts_catalog_and_knowledge_inside_the_untrusted_envelope():
    text = render_stable({"workspace": "W", "dialects": ["postgres"]},
                         {"objective": "Why?", "catalog": _catalog()[:1],
                          "glossary": [{"id": "g1", "name": "SLA", "label": "percent", "aka": ["service level"],
                                        "text": "Share met.", "columns": ["sn.incident.made_sla"], "trusted": False}],
                          "metrics": "NO_MATCH"})
    assert text.startswith("Workspace: W · SQL dialect: postgres\nOBJECTIVE: Why?\n<untrusted_context>\nCATALOG\n")
    assert "- SLA (percent) aka: service level — Share met. [columns: sn.incident.made_sla] (unreviewed)" in text
    assert "METRICS: NO_MATCH" in text and text.endswith("</untrusted_context>") and "g1" not in text


def test_a_sql_question_that_names_no_table_gets_every_table_by_name_only():
    profile = PurposeProfile(sections=["catalog"], catalog_detail="profile", referenced_only=True, max_chars=20_000)
    full = [{"asset": "sn.incident", "role": "fact", "row_count": 5, "description": "long text " * 20,
             "columns": [{"name": f"c{i}", "type": "text", "semantic_type": "categorical", "distinct": 3} for i in range(30)]}]
    c = compile_context("sql_generation", profile, objective="hello there", required={"question": "hello there"}, catalog=full)
    assert "catalog" in c.no_match and c.body["catalog"][0]["columns"][0] == "c0"  # names, no profile
    assert c.body["catalog"][0]["role"] == "fact" and "description" not in c.body["catalog"][0]
    assert any("names only" in str(o.get("detail")) for o in c.omitted)


def test_stable_keys_come_first_and_are_listed():
    profile = PurposeProfile(sections=["catalog", "glossary"], catalog_detail="stats", max_chars=20_000)
    c = compile_context("hypothesis_generation", profile, objective="sla", required={"results": [1], "objective": "sla"},
                        catalog=_catalog()[:1], knowledge=[KnowledgeItem(id="g", section="glossary", name="SLA",
                                                                         text="service level", source="user")])
    assert c.stable == ["objective", "catalog", "glossary"] and list(c.body)[:3] == c.stable and "results" in c.body


def test_synonyms_score_once_and_render_once():
    from analystos.context.compiler import item_score, terms

    item = KnowledgeItem(id="g", section="glossary", name="Breach", text="Missed target.", source="user", synonyms=("violation",))
    assert item_score(item, terms("violation rate"), set()) > 0


# ------------------------------------------------------------------ catalog for prompts: drafts and denied columns
def _asset(**kw):
    base = dict(schema_name="sn", name="incident", business_name="Incidents (model)", business_name_origin="model",
                description="A model's guess", description_origin="model", reviewed=False, row_count=10,
                semantics={"description": "Rule text: incident records", "business_name": "Incident", "role": "fact",
                           "grain": "incident"})
    return SimpleNamespace(**{**base, **kw})


def _column(name, **kw):
    base = dict(name=name, data_type="text", semantic_type="categorical", business_name=None, description=None,
                description_origin=None, business_name_origin=None, tags=[], profile={"distinct": 3},
                semantics={"semantic_role": "dimension"})
    return SimpleNamespace(**{**base, **kw})


def _catalog_ctx(monkeypatch, asset, cols, denied=()):
    monkeypatch.setattr(common, "asset_rows", lambda ctx: [(asset, cols)])
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: PlatformSettings())
    return SimpleNamespace(run=SimpleNamespace(objective="x"), scope=SimpleNamespace(denied_columns=list(denied)),
                           policy=WorkspacePolicyDoc())


def test_unreviewed_model_drafts_never_reach_a_prompt(monkeypatch):
    cols = [_column("priority", description="Model says: urgency", description_origin="model"),
            _column("state", description="Owner text", description_origin="user")]
    table = common.catalog_for_prompt(_catalog_ctx(monkeypatch, _asset(), cols))[0]
    assert table["description"] == "Rule text: incident records" and table["business_name"] == "Incident"
    assert table["role"] == "fact" and table["grain"] == "incident"
    by = {c["name"]: c for c in table["columns"]}
    assert "meaning" not in by["priority"] and by["state"]["meaning"] == "Owner text" and by["priority"]["role"] == "dimension"
    reviewed = common.catalog_for_prompt(_catalog_ctx(monkeypatch, _asset(reviewed=True), cols))[0]
    assert reviewed["description"] == "A model's guess"  # once a person reviewed it, it is the description


def test_denied_columns_are_left_out_of_prompt_catalogs_and_the_context_package(monkeypatch, sqlite_db):
    cols = [_column("priority"), _column("caller_email")]
    table = common.catalog_for_prompt(_catalog_ctx(monkeypatch, _asset(), cols, denied=["*.caller_email"]))[0]
    assert [c["name"] for c in table["columns"]] == ["priority"]

    from analystos.context import service
    from analystos.db.models import SourceAsset, SourceColumn

    with sqlite_db() as s:
        s.add(SourceAsset(id="a1", source_id="src", workspace_id="ws1", schema_name="sn", name="incident", source_name="incident",
                          description="draft", description_origin="model", reviewed=False, semantics={"description": "rule"}))
        for i, n in enumerate(("priority", "caller_email")):
            s.add(SourceColumn(asset_id="a1", name=n, ordinal=i, data_type="text", tags=[], profile={}, semantics={}))
        s.commit()
        monkeypatch.setattr(service, "neighborhood", lambda *a, **k: [])
        monkeypatch.setattr(service, "search", lambda *a, **k: [])
        monkeypatch.setattr(service, "external_context", lambda *a, **k: [])
        package = service.build_context_package(s, "ws1", "why", ["sn.incident"], denied_columns=["sn.incident.caller_email"])
    assert [c["name"] for c in package["tables"][0]["columns"]] == ["priority"]
    assert package["tables"][0]["description"] == "rule"


# ------------------------------------------------------------------ compile after the gate; shared retrieval
def test_a_skipped_call_compiles_nothing_and_records_a_cheap_estimate(monkeypatch):
    monkeypatch.setattr(common, "compile_for", lambda *a, **k: pytest.fail("compiled although the model was not asked"))
    records = []
    router = SimpleNamespace(mode=lambda p: "auto", record_skip=lambda p, c, **kw: records.append(kw))
    ctx = SimpleNamespace(router=router, call_ctx=lambda **k: None,
                          scope=SimpleNamespace(columns={"sn.incident": ["priority", "secret"]}, denied_columns=["sn.incident.secret"]))
    deferred = common.defer_compile(ctx, "planning", {"objective": "why"})
    assert common.model_gate(ctx, "planning", deferred, deterministic_ok=True) is False
    assert records and records[0]["estimated_tokens"] > 500 and "secret" not in deferred.estimate_text()


def test_knowledge_retrieval_is_shared_across_purposes_with_the_same_query(monkeypatch):
    platform = PlatformSettings(llm=LLMSettings(cache_enabled=False), context=ContextSettings())
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: platform)
    monkeypatch.setattr("analystos.context.version.workspace_knowledge_version", lambda ws, policy=None: "kv:1")
    loads = []
    item = KnowledgeItem(id="g", section="glossary", name="SLA breach", text="made_sla false", source="user", synonyms=("miss",))
    monkeypatch.setattr("analystos.context.compiler.load_knowledge", lambda s, ws, sections, **kw: loads.append(sections) or [item])
    monkeypatch.setattr(common, "session_scope", _NullSession)
    monkeypatch.setattr(common, "context_header", lambda ctx: {})

    def ctx():
        return SimpleNamespace(workspace=SimpleNamespace(id="ws1"), policy=WorkspacePolicyDoc(), run=SimpleNamespace(id="r", objective="sla breach"),
                               scope=SimpleNamespace(assets=["sn.incident"], columns={}, denied_columns=[], scope_hash=lambda: "h"))

    profile = PurposeProfile(sections=["glossary"], max_chars=20_000)
    for purpose in ("semantic_modeling", "sql_generation"):
        compiled, complete = common._compile(ctx(), purpose, profile, platform, objective="sla breach", required={"q": 1},
                                             catalog=None, reference_text=None, header={}, limit=20_000)
        assert complete and compiled.body["glossary"][0]["aka"] == ["miss"]
    assert len(loads) == 1  # the second purpose reused the retrieval
    assert context_cache.stats()["retrieval"]["sql_generation"]["hits"] == 1


class _NullSession:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


# ------------------------------------------------------------------ layout of a plain payload with a stable part
def test_plain_payload_stable_part_is_a_cached_second_message(monkeypatch):
    platform = PlatformSettings(llm=LLMSettings(cache_enabled=False))
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: platform)
    sink, transport = Sink(), FakeTransport(chat=lambda p: chat_json({"actions": [], "done": True}))
    router = _router(transport, platform, sink)
    ctx = SimpleNamespace(router=router, policy=WorkspacePolicyDoc(), run=SimpleNamespace(objective="o"),
                          call_ctx=lambda exclude_families=None: CallContext(workspace_id="ws1"), say=lambda *a, **k: None)
    common.llm_json(ctx, "agent_actions", "agent_actions.v1", {"history": [], "remaining_rounds": 2},
                    stable={"role": "r", "goal": "g", "capabilities": [{"id": "x"}]})
    logical = sink.records[-1]["request"]["messages"]
    assert [m.get("cache", False) for m in logical] == [True, True, False]
    assert json.loads(logical[1]["content"])["goal"] == "g" and json.loads(logical[2]["content"]) == {"history": [],
                                                                                                        "remaining_rounds": 2}


# ------------------------------------------------------------------ batching per-finding calls
def _prepared(n):
    return [("h", f"I-{i}", f"statement {i}", {"method": "rate_by_segment"}, {}, "e", [], {}, [], {"n": i}) for i in range(1, n + 1)]


def test_narratives_are_one_call_per_run_and_a_malformed_batch_falls_back_per_finding(monkeypatch):
    from analystos.agents import insight

    calls = []

    def fake(ctx, purpose, prompt_name, payload, **kw):
        calls.append(prompt_name)
        if prompt_name == "insight_narrative_batch.v1":
            return answer, "m"
        return {"title": "t", "finding": "f"}, "m"

    monkeypatch.setattr(common, "llm_json", fake)
    monkeypatch.setattr(insight, "llm_json", fake)
    monkeypatch.setattr(insight, "model_gate", lambda *a, **k: True)
    monkeypatch.setattr(insight, "binding_check", lambda *a: (lambda d: None))
    ctx = SimpleNamespace(say=lambda *a, **k: None)
    answer = {"findings": [{"id": "I-1", "title": "a", "finding": "b"}, {"id": "I-9", "finding": "stray"}]}
    out = insight.draft_narratives(ctx, _prepared(3))
    assert calls == ["insight_narrative_batch.v1"] and set(out) == {"I-1"}  # I-2/I-3 keep their template text
    calls.clear()
    answer = {"oops": True}
    out = insight.draft_narratives(ctx, _prepared(3))
    assert calls == ["insight_narrative_batch.v1"] + ["insight_narrative.v1"] * 3 and set(out) == {"I-1", "I-2", "I-3"}


def test_independent_reviews_are_one_call_per_run(monkeypatch):
    from analystos.agents import critic

    calls = []

    def fake(ctx, purpose, prompt_name, payload, **kw):
        calls.append((prompt_name, kw.get("exclude_families")))
        return {"reviews": [{"id": "I-1", "supports": True, "confidence": 0.8}, {"id": "I-2", "supports": "yes"}]}, "m"

    monkeypatch.setattr(common, "llm_json", fake)
    ctx = SimpleNamespace(policy=WorkspacePolicyDoc(independent_model_verification=True), say=lambda *a, **k: None)
    states = [{"code": f"I-{i}", "primary_family": fam, "review_payload": {"claim": "c"}} for i, fam in ((1, "openai"), (2, "google"))]
    out = critic.independent_reviews(ctx, states)
    assert calls == [("verification_batch.v1", ["google", "openai"])]  # no reviewer shares a family with any author
    assert out["I-1"][0] == {"supports": True, "confidence": 0.8} and out["I-2"][0] is None


def test_follow_up_results_send_specs_only_for_supported_and_inconclusive_tests():
    from analystos.agents.investigator import results_for_prompt

    spec = {"asset": "a.t", "method": "rate_by_segment", "top_k": 12, "min_group_size": 30, "time": None, "filters": [],
            "outcome": {"type": "equals", "column": "made_sla", "value": False, "start_hour": 8, "end_hour": 18, "edges": None}}
    result = {"test": "chi", "n": 10, "p_value": 0.01234567, "effect_size": 0.2, "highlights": {"top_segment": "3+", "cramers_v": 1}}
    out = results_for_prompt([{"code": "H-1", "status": "supported", "statement": "s1", "spec": spec, "result": result},
                              {"code": "H-2", "status": "rejected", "statement": "s2", "spec": spec, "result": result}])
    assert out[1] == "H-2 rejected: s2"
    assert out[0]["spec"] == {"asset": "a.t", "method": "rate_by_segment", "outcome": {"type": "equals", "column": "made_sla",
                                                                                      "value": False}}
    assert out[0]["stats"] == {"test": "chi", "n": 10, "p_value": 0.0123, "effect_size": 0.2, "top_segment": "3+"}


def test_follow_up_prompt_carries_the_method_vocabulary():
    from analystos.agents.prompts import prompt

    text = prompt("follow_up_generation.v1")
    assert "rate_by_segment" in text and "{method_vocabulary}" not in text and "same closed spec vocabulary as before" not in text


def test_deterministic_input_purposes_are_cacheable():
    assert {"sql_repair", "semantic_query", "verification", "follow_up_generation"} <= set(LLMSettings().cacheable_purposes)


# ------------------------------------------------------------------ savings view
def test_token_savings_reports_provider_cached_tokens_and_their_saving(sqlite_db):
    from analystos.api.routers.admin import token_savings
    from analystos.db.models import ModelCall

    with sqlite_db() as s:
        s.add(ModelCall(workspace_id="ws1", purpose="planning", profile="p", provider="openrouter", model="anthropic/claude-sonnet-5",
                        status="ok", attempt=1, latency_ms=1, input_tokens=1_000_000, output_tokens=0, cost_usd=0.3,
                        cached_input_tokens=800_000, answered_by="llm_large"))
        s.commit()
        out = token_savings(days=30, _=None, session=s)
    t = out["totals"]
    assert t["cached_input_tokens"] == 800_000 and t["input_tokens"] == 1_000_000 and t["cached_input_share"] == 0.8
    assert t["cached_input_saved_usd"] == pytest.approx(800_000 * 3.0 * 0.9 / 1_000_000)
    assert out["by_purpose"]["planning"]["cached_input_tokens"] == 800_000
    assert out["by_model"]["anthropic/claude-sonnet-5"]["cached_input_saved_usd"] == pytest.approx(2.16)
    assert set(out["context_cache"]) == {"shared", "by_kind"}
