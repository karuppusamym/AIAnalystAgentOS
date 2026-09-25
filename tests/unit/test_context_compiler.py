"""P4-T03 context compiler: selection by relevance, purpose budgets, receipts, NO_MATCH, omitted
list, and failing visibly when the mandatory context is over budget."""
import json
from types import SimpleNamespace

import pytest
from tests.fakes import FakeTransport, chat_json

from analystos.agents import common
from analystos.context.compiler import NO_MATCH, KnowledgeItem, compile_context, excerpt, terms
from analystos.contracts.platform import ContextSettings, LLMSettings, PlatformSettings, PurposeProfile
from analystos.contracts.policy import WorkspacePolicyDoc
from analystos.core.errors import ContextOverBudget
from analystos.llm.cache import ResponseCache
from analystos.llm.router import CallContext, ModelRouter, normalize_messages

KEY = {"OPENROUTER_API_KEY": "sk-test-000000000000000000000000"}


def catalog():
    noise = [{"name": f"attr_{i}", "type": "text", "semantic_type": "text"} for i in range(40)]
    return [
        {"asset": "sn.incident", "business_name": "Incidents", "row_count": 5000,
         "columns": [*noise[:20],
                     {"name": "made_sla", "type": "boolean", "semantic_type": "boolean", "distinct": 2, "null_rate": 0.0},
                     {"name": "priority", "type": "text", "semantic_type": "categorical", "distinct": 5},
                     {"name": "assignment_group", "type": "text", "semantic_type": "categorical", "distinct": 12},
                     {"name": "sys_id", "type": "text", "semantic_type": "id", "distinct": 5000}]},
        {"asset": "sn.change_request", "business_name": "Changes", "row_count": 900,
         "columns": [*noise[20:], {"name": "risk", "type": "text", "semantic_type": "categorical", "distinct": 4}]},
    ]


def knowledge():
    return [
        KnowledgeItem(id="ctx_sla", section="glossary", name="SLA breach",
                      text="An incident breaches its SLA when made_sla is false. Breach rate = share of incidents with made_sla false.",
                      source="pack:itsm", mapped_columns=("sn.incident.made_sla",)),
        KnowledgeItem(id="ctx_cab", section="glossary", name="CAB", text="Change advisory board approves normal changes.",
                      source="pack:itsm"),
        KnowledgeItem(id="ctx_rule", section="business_rules", name="Priority 1 escalation",
                      text="Priority 1 incidents are escalated after 30 minutes.", source="user"),
        KnowledgeItem(id="ins_1", section="prior_findings", name="Network group breaches more",
                      text="Incidents assigned to Network breach SLA 2x more often.", source="run:run_old"),
        KnowledgeItem(id="hyp_1", section="negative_knowledge", name="Weekday drives SLA breach",
                      text="Weekday drives SLA breach — tested, not supported.", source="run:run_old"),
    ]


HYP = PurposeProfile(sections=["catalog", "glossary", "business_rules", "metrics", "prior_findings", "negative_knowledge"],
                     catalog_detail="profile", max_columns_per_table=6, max_chars=40_000)


def compile_(profile=HYP, objective="What drives SLA breaches for incidents by priority?", **kw):
    args = {"objective": objective, "required": {"objective": objective}, "catalog": catalog(), "knowledge": knowledge(),
            "header": {"workspace": "ITSM", "dialects": ["postgres"]}} | kw
    return compile_context("hypothesis_generation", profile, **args)


def test_relevant_columns_first_and_capped_with_omitted_list():
    c = compile_()
    incident = next(t for t in c.body["catalog"] if t["asset"] == "sn.incident")
    names = [col["name"] for col in incident["columns"]]
    assert len(names) == 6
    assert names[:2] == ["made_sla", "priority"]  # mapped by the matching glossary entry, then named by the objective
    assert "sys_id" not in names  # identifiers go last
    dropped = next(o for o in c.omitted if o.get("asset") == "sn.incident" and o.get("columns"))
    assert set(dropped["columns"]) | set(names) == {col["name"] for col in catalog()[0]["columns"]}
    assert c.body["omitted"]["columns_not_sent"]["sn.incident"] == len(dropped["columns"])
    assert c.body["catalog"][0]["asset"] == "sn.incident"  # most relevant table first
    assert incident["columns"][0] == {"name": "made_sla", "type": "boolean", "semantic_type": "boolean", "distinct": 2,
                                      "null_rate": 0.0}  # profile detail


def test_knowledge_selected_by_relevance_with_receipts():
    c = compile_()
    assert [i["id"] for i in c.body["glossary"]] == ["ctx_sla"]  # CAB is unrelated to the objective
    assert [i["id"] for i in c.body["prior_findings"]] == ["ins_1"]
    assert [i["id"] for i in c.body["negative_knowledge"]] == ["hyp_1"]
    by_id = {r["id"]: r for r in c.receipts}
    assert by_id["ctx_sla"]["source"] == "pack:itsm" and by_id["ctx_sla"]["section"] == "glossary"
    assert len(by_id["ctx_sla"]["sha256"]) == 16 and by_id["ctx_sla"]["score"] > 0
    assert {"asset:sn.incident", "asset:sn.change_request"} <= set(by_id)
    assert "ctx_cab" not in by_id
    # every included knowledge item has a receipt, and nothing without a receipt is included
    included = {i["id"] for s in ("glossary", "business_rules", "prior_findings", "negative_knowledge")
                for i in (c.body[s] if isinstance(c.body[s], list) else [])}
    assert included == {r["id"] for r in c.receipts if r["section"] != "catalog"}


def test_no_match_instead_of_padding():
    c = compile_(objective="What drives SLA breaches for incidents by priority?")
    assert c.body["metrics"] == NO_MATCH and "metrics" in c.no_match  # no metric candidates at all
    c2 = compile_(objective="quarterly revenue by region")
    for section in ("glossary", "business_rules", "prior_findings", "negative_knowledge"):
        assert c2.body[section] == NO_MATCH
    assert not [r for r in c2.receipts if r["section"] != "catalog"]


def test_budget_keeps_whole_items_and_lists_the_rest():
    many = [KnowledgeItem(id=f"g{i}", section="glossary", name=f"SLA breach term {i}", text="SLA breach priority " * 30,
                          source="user") for i in range(30)]
    profile = HYP.model_copy(update={"max_chars": 4_000, "max_items_per_section": 20, "sections": ["catalog", "glossary"]})
    c = compile_(profile=profile, knowledge=many)
    text = json.dumps(c.header, separators=(",", ":")) + json.dumps(c.body, separators=(",", ":"))
    assert c.chars <= 4_000 and len(text) <= 4_000 + 2
    sent = c.body["glossary"]
    assert 0 < len(sent) < 20
    assert all(len(i["text"]) <= profile.item_chars + 2 for i in sent)
    budget_omitted = [o for o in c.omitted if o["section"] == "glossary"]
    assert len(sent) + len(budget_omitted) == 30  # 20 cap + budget, everything accounted for
    assert {o["reason"] for o in budget_omitted} == {"budget", "section item cap"}
    assert c.body["omitted"]["glossary"] == len(budget_omitted)


def test_mandatory_part_over_budget_fails_visibly():
    profile = PurposeProfile(sections=["catalog"], max_chars=1_000)
    with pytest.raises(ContextOverBudget) as exc:
        compile_context("sql_repair", profile, objective="x", required={"sql": "select " + "a," * 2000}, catalog=catalog())
    assert exc.value.details["mandatory_chars"] > exc.value.details["budget_chars"] == 1_000


def test_sql_generation_gets_only_referenced_tables():
    profile = PurposeProfile(sections=["catalog"], catalog_detail="columns", referenced_only=True, max_chars=20_000)
    c = compile_context("sql_generation", profile, objective="how many changes by risk?", required={"question": "q"},
                        catalog=catalog(), reference_text="how many changes by risk?")
    assert [t["asset"] for t in c.body["catalog"]] == ["sn.change_request"]
    assert {"section": "catalog", "asset": "sn.incident", "reason": "not referenced"} in c.omitted
    none = compile_context("sql_generation", profile, objective="hello there", required={"question": "q"}, catalog=catalog())
    assert len(none.body["catalog"]) == 2 and "catalog" in none.no_match


def test_header_is_stable_and_separate_from_the_volatile_body():
    a = compile_(objective="SLA breaches by priority")
    b = compile_(objective="resolution by assignment group")
    assert a.header == b.header == {"workspace": "ITSM", "dialects": ["postgres"]}
    assert a.body != b.body and "workspace" not in a.body


def test_terms_and_excerpts():
    assert {"sla", "breach", "priority"} <= terms("What drives SLA breaches by priority?")
    assert "resolution" in terms("resolution_hours") and "hour" in terms("resolution_hours")
    text = "First sentence here. Second sentence is longer than the limit allows for sure."
    assert excerpt(text, 30) == "First sentence here. …"
    assert excerpt(text, 60) == "First sentence here. Second sentence is longer than the …"
    assert excerpt("short", 40) == "short"


# ------------------------------------------------------------------ agents: compile_for + llm_json
class Sink:
    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)

    def check_budget(self, ctx, purpose):
        return None


def _ctx(router, run_objective="What drives SLA breaches by priority?"):
    return SimpleNamespace(router=router, policy=WorkspacePolicyDoc(), run=SimpleNamespace(objective=run_objective, id="run1"),
                           workspace=SimpleNamespace(id="ws1", name="ITSM"),
                           scope=SimpleNamespace(assets=["sn.incident"], columns={}, source_dialects={"src": "postgres"}),
                           call_ctx=lambda exclude_families=None: CallContext(workspace_id="ws1", run_id="run1",
                                                                              exclude_families=exclude_families or []),
                           say=lambda *a, **k: said.append((a, k)))


said: list = []


def _platform(**context):
    return PlatformSettings(llm=LLMSettings(cache_enabled=False), context=ContextSettings(**context))


def test_llm_json_sends_compiled_context_in_cache_stable_order_with_receipts(monkeypatch):
    platform = _platform()
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: platform)
    monkeypatch.setattr("analystos.context.compiler.load_knowledge", lambda s, ws, sections, run_id=None: knowledge())
    monkeypatch.setattr(common, "session_scope", _null_session)
    sink, transport = Sink(), FakeTransport(chat=lambda p: chat_json({"hypotheses": []}))
    router = ModelRouter(transport=transport, sink=sink, api_key_lookup=KEY.get, max_retries=0,
                         settings_provider=lambda: platform, cache=ResponseCache(None))
    ctx = _ctx(router)
    compiled = common.compile_for(ctx, "hypothesis_generation", {"objective": ctx.run.objective}, catalog=catalog())
    data, _ = common.llm_json(ctx, "hypothesis_generation", "hypothesis_generation.v1", compiled)
    assert data == {"hypotheses": []}
    turns = normalize_messages(transport.chat_calls[0]["messages"])
    system, user = turns[0]["content"], turns[1]["content"]
    assert "closed analysis vocabulary" in system  # static text + method vocabulary first
    header, _, variable = user.partition("\n\n")
    head = json.loads(header)["workspace_context"]
    assert head["workspace"] == "ITSM" and head["dialects"] == ["postgres"] and "objective" not in head
    body = json.loads(variable)
    assert body["glossary"][0]["id"] == "ctx_sla" and body["metrics"] == NO_MATCH
    assert sink.records[-1]["status"] == "ok"
    assert {r["id"] for r in sink.records[-1]["ctx"].context_receipts} >= {"ctx_sla", "asset:sn.incident"}


def test_over_budget_context_is_refused_and_recorded_without_a_call(monkeypatch):
    profiles = {"sql_repair": PurposeProfile(sections=["catalog"], max_chars=1_000)}
    platform = _platform(profiles=profiles)
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: platform)
    sink, transport = Sink(), FakeTransport(chat=lambda p: chat_json({"sql": "select 1"}))
    router = ModelRouter(transport=transport, sink=sink, api_key_lookup=KEY.get, max_retries=0,
                         settings_provider=lambda: platform, cache=ResponseCache(None))
    ctx = _ctx(router)
    said.clear()
    compiled = common.compile_for(ctx, "sql_repair", {"sql": "select " + "a," * 2000, "error": "boom"}, catalog=catalog())
    assert compiled.refused and compiled.mandatory_chars > compiled.budget_chars
    data, reason = common.llm_json(ctx, "sql_repair", "sql_repair.v1", compiled)
    assert (data, reason) == (None, "context_over_budget") and transport.chat_calls == []
    assert sink.records[-1]["status"] == "refused" and sink.records[-1]["provider"] == "context_compiler"
    assert any("not sent" in a[0] for a, _ in said)


def test_compiler_kill_switch_sends_the_increment3_payload(monkeypatch):
    platform = _platform(compiler_enabled=False)
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: platform)
    compiled = common.compile_for(_ctx(None), "planning", {"objective": "x"}, catalog=catalog())
    assert compiled.body == {"objective": "x", "catalog": catalog()} and compiled.receipts == []


class _null_session:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False
