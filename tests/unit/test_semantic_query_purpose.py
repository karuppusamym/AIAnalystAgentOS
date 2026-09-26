"""P7-02: the `semantic_query` purpose (rules rung first, then a small model that only *chooses* a
SemanticQuery), the governed/ad_hoc label on answers and charts, and the Ossie round trip of the
relationship cardinality extension."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from analystos.agents import sql_agent
from analystos.contracts.platform import DETERMINISTIC_CAPABLE
from analystos.contracts.policy import DataScope, WorkspacePolicyDoc
from analystos.contracts.semantic import SemanticDataset, SemanticModelDoc, SemanticRelationship
from analystos.core.errors import InvalidInput
from analystos.semantic import ossie

ROOT = Path(__file__).resolve().parents[2]


class Router:
    def __init__(self, mode="auto"):
        self._mode, self.skips = mode, []

    def mode(self, purpose):
        return self._mode

    def record_skip(self, purpose, ctx, *, estimated_tokens, reason, rung="rules"):
        self.skips.append((purpose, rung, reason))


@pytest.fixture
def ctx(monkeypatch):
    catalog = {"id": "m", "version": 5, "hash": "h", "synonyms": {}, "relationships": [
        {"name": "lines_order", "from": "lines", "to": "orders", "from_columns": ["order_id"], "to_columns": ["order_id"],
         "cardinality": "many_to_one", "validated_at": "2026-09-26", "validated_by": "a"}],
        "datasets": [
            {"name": "orders", "source": "sales.orders", "fields": [
                {"name": n, "expressions": [{"expression": n}], "dimension": d} for n, d in
                [("order_id", None), ("amount", None), ("region", {"is_time": False}), ("day", {"is_time": True})]]},
            {"name": "lines", "source": "sales.lines", "fields": [
                {"name": n, "expressions": [{"expression": n}], "dimension": {"is_time": False}} for n in ("order_id", "product")]}],
        "metrics": {"revenue": {"id": "r", "version": 2, "hash": "rh", "definition": {
            "name": "revenue", "dataset": "orders", "display_name": "Revenue", "expressions": [{"expression": "SUM(amount)"}],
            "dimensions": ["region", "day", "lines.product"]}}}}
    scope = DataScope(workspace_id="w", user_id="u", role="analyst", source_ids=["s"], assets=["sales.orders", "sales.lines"],
                      asset_sources={"sales.orders": "s", "sales.lines": "s"}, source_dialects={"s": "duckdb"},
                      columns={"sales.orders": ["order_id", "amount", "region", "day"], "sales.lines": ["order_id", "product"]})
    executed = []

    def execute(sc, sql, **kw):
        executed.append(sql)
        return SimpleNamespace(query_id="q", columns=["revenue"], rows=[[1]], row_count=1, truncated=False)

    monkeypatch.setattr(sql_agent, "_authorize_ask", lambda _: None)
    monkeypatch.setattr(sql_agent, "_check_budget", lambda _: None)
    return SimpleNamespace(semantic_catalog=catalog, scope=scope, policy=WorkspacePolicyDoc(), user=SimpleNamespace(id="u"),
                           router=Router(), call_ctx=lambda **_: None, turn_id="t", executed=executed,
                           services=SimpleNamespace(gateway=SimpleNamespace(execute=execute)))


def test_the_purpose_is_configured_rules_first():
    cfg = yaml.safe_load((ROOT / "config" / "models.yaml").read_text())
    ladder = cfg["ladders"]["semantic_query"]
    assert ladder.index("rules") < ladder.index("llm_small") and cfg["routing"]["semantic_query"] == "low_cost"
    assert "semantic_query" in DETERMINISTIC_CAPABLE
    from analystos.agents.prompts import PROMPTS

    assert "never write SQL" in PROMPTS["semantic_query.v1"]


def test_rules_rung_answers_governed_with_a_labelled_chart_and_records_the_avoided_calls(ctx, monkeypatch):
    monkeypatch.setattr(sql_agent, "llm_json", lambda *a, **k: pytest.fail("no model call on the rules rung"))
    out = sql_agent.ask(ctx, "revenue by region")
    assert out["governance"] == "governed" and out["semantic"]["chosen_by"] == "rules"
    assert out["chart"] == {"type": "bar", "x": "region", "y": "revenue", "governance": "governed",
                            "semantic_model_version": 5, "compiler_version": "semantic.v2"}
    assert ("semantic_query", "rules", "rules: exact metric match") in ctx.router.skips


def test_model_rung_chooses_a_query_the_compiler_validates(ctx, monkeypatch):
    calls = []

    def llm(c, purpose, prompt_name, payload, **kw):
        calls.append((purpose, prompt_name, payload))
        choice = {"semantic_query": {"metrics": ["revenue"], "time": {"dimension": "day", "grain": "week"}}}
        assert kw["validate"](choice) is None and kw["validate"]({"semantic_query": {"metrics": ["nope"]}})
        return choice, "small-model"

    monkeypatch.setattr(sql_agent, "llm_json", llm)
    out = sql_agent.ask(ctx, "how did revenue develop week over week?")
    assert calls and calls[0][:2] == ("semantic_query", "semantic_query.v1") and "revenue" in calls[0][2]["metrics"]
    assert "SELECT" not in str(calls[0][2])  # the model sees names, never SQL
    assert out["governance"] == "governed" and out["semantic"]["chosen_by"] == "model"
    assert "WEEK" in ctx.executed[0].upper()


def test_model_rung_is_skipped_when_the_question_names_no_metric(ctx, monkeypatch):
    monkeypatch.setattr(sql_agent, "llm_json", lambda *a, **k: pytest.fail("gated: no metric word in the question"))
    assert sql_agent._semantic_answer(ctx, "how many tickets were opened", None) is None
    assert any(p == "semantic_query" and "deterministic" in r for p, _, r in ctx.router.skips)
    ctx.router._mode = "off"
    assert sql_agent._semantic_answer(ctx, "revenue trend lately", None) is None


def test_model_saying_null_or_naming_unknown_fields_falls_through_to_ad_hoc(ctx, monkeypatch):
    monkeypatch.setattr(sql_agent, "llm_json", lambda *a, **k: ({"semantic_query": None}, "m"))
    assert sql_agent._semantic_answer(ctx, "revenue excluding refunds", None) is None
    monkeypatch.setattr(sql_agent, "llm_json", lambda *a, **k: ({"semantic_query": {"metrics": ["revenue"], "dimensions": ["colour"]}}, "m"))
    assert sql_agent._semantic_answer(ctx, "revenue by colour", None) is None
    assert not ctx.executed


def test_a_governance_refusal_is_the_answer_even_when_a_model_chose_the_query(ctx, monkeypatch):
    monkeypatch.setattr(sql_agent, "llm_json", lambda *a, **k: (
        {"semantic_query": {"metrics": ["revenue"], "dimensions": ["lines.product"]}}, "m"))
    with pytest.raises(InvalidInput, match="fan-out") as refused:
        sql_agent._semantic_answer(ctx, "revenue split per product", None)
    assert refused.value.details["edge"] == "lines_order" and not ctx.executed


def test_ad_hoc_answers_and_charts_are_labelled():
    out = sql_agent.label({"status": "answered", "chart": {"type": "bar", "x": "a", "y": "b"}})
    assert out["governance"] == "ad_hoc" and out["chart"]["governance"] == "ad_hoc"
    assert "compiler_version" not in out["chart"]


def test_chart_contract_requires_versions_for_governed_and_keeps_old_hashes():
    from analystos.contracts.bi import ChartSpec
    from analystos.core.ids import stable_hash

    base = {"key": "k", "title": "t", "chart_type": "bar", "intent": "comparison", "dataset": "d"}
    assert "governance" not in ChartSpec(**base).model_dump() and ChartSpec(**base).governance == "ad_hoc"
    assert stable_hash(ChartSpec(**base).model_dump()) == stable_hash(ChartSpec.model_validate(ChartSpec(**base).model_dump()).model_dump())
    with pytest.raises(ValueError):
        ChartSpec(**base, governance="governed")
    governed = ChartSpec(**base, governance="governed", semantic_model_version=3, compiler_version="semantic.v2")
    assert governed.model_dump()["governance"] == "governed"


def test_relationship_cardinality_round_trips_through_ossie():
    rel = SemanticRelationship.model_validate({"name": "o_c", "from": "orders", "to": "customers", "from_columns": ["customer_id"],
                                               "to_columns": ["id"], "cardinality": "many_to_one",
                                               "validated_at": "2026-09-26T10:00:00+00:00", "validated_by": "u1",
                                               "custom_extensions": [{"vendor_name": "DBT", "data": "{}"}]})
    doc = ossie.to_ossie([SemanticModelDoc(name="m", datasets=[SemanticDataset(name="orders", source="s.orders"),
                                                                 SemanticDataset(name="customers", source="s.customers")],
                                           relationships=[rel])])
    assert not ossie.validate(doc)  # still valid Ossie 0.1.1: the fields ride in the COMMON extension
    out = doc["semantic_model"][0]["relationships"][0]
    assert "cardinality" not in out and any(x["vendor_name"] == "COMMON" for x in out["custom_extensions"])
    back = ossie.from_ossie(deepcopy(doc))[0].relationships[0]
    assert (back.cardinality, back.validated_by, back.validated) == ("many_to_one", "u1", True)
    assert back.custom_extensions == [{"vendor_name": "DBT", "data": "{}"}]
