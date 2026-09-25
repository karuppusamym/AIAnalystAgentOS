"""P4-C01: the four model-related workspace policy fields change behaviour (review C3)."""
from types import SimpleNamespace

import pytest
from tests.fakes import FakeTransport, chat_json

from analystos.agents import common
from analystos.contracts.platform import PlatformSettings
from analystos.contracts.policy import WorkspacePolicyDoc
from analystos.core.errors import ApprovalRequired, ModelRouteUnavailable
from analystos.llm.cache import ResponseCache
from analystos.llm.config import ModelMeta, load_models_config
from analystos.llm.router import CallContext, ModelRouter

KEY = {"OPENROUTER_API_KEY": "sk-test-000000000000000000000000"}


class Sink:
    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)

    def check_budget(self, ctx, purpose):
        return None


def make(transport, sink=None, config=None):
    platform = PlatformSettings()
    return ModelRouter(config=config, transport=transport, sink=sink or Sink(), api_key_lookup=KEY.get, max_retries=0,
                       settings_provider=lambda: platform, cache=ResponseCache(None))


def ctx_for(**policy):
    return CallContext.for_policy(WorkspacePolicyDoc(**policy), workspace_id="ws1")


# --------------------------------------------------------------------- send_data_samples_to_models
def _catalog_ctx(monkeypatch, *, samples: bool):
    column = SimpleNamespace(name="priority", data_type="text", semantic_type="categorical", business_name=None, description=None,
                             tags=[], profile={"distinct": 4, "null_rate": 0.0,
                                               "top_values": [{"value": "1 - Critical", "count": 9}, {"value": "2 - High", "count": 5}]})
    asset = SimpleNamespace(schema_name="sn", name="incident", business_name="Incidents", row_count=100)
    monkeypatch.setattr(common, "asset_rows", lambda ctx: [(asset, [column])])
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: PlatformSettings())
    return SimpleNamespace(run=SimpleNamespace(objective="why are critical incidents slow"),
                           scope=SimpleNamespace(denied_columns=[]),
                           policy=WorkspacePolicyDoc(send_data_samples_to_models=samples))


def test_send_data_samples_false_keeps_top_values_out_of_prompts(monkeypatch):
    catalog = common.catalog_for_prompt(_catalog_ctx(monkeypatch, samples=False))
    assert "values" not in catalog[0]["columns"][0]
    assert "1 - Critical" not in common.compact_json(catalog)


def test_send_data_samples_true_allows_low_cardinality_vocabulary(monkeypatch):
    catalog = common.catalog_for_prompt(_catalog_ctx(monkeypatch, samples=True))
    assert catalog[0]["columns"][0]["values"] == ["1 - Critical", "2 - High"]


# --------------------------------------------------------------------- allowed_providers
def test_provider_outside_allowed_providers_fails_closed():
    transport = FakeTransport(chat=lambda p: chat_json({"ok": 1}), decide=lambda p: {"answers": {"p": {"noul": 0.5}}})
    r = make(transport)
    chat_only = ctx_for(allowed_providers=["openrouter"])
    with pytest.raises(ModelRouteUnavailable, match="allowed_providers"):
        r.decide("risk_check", {"request": "x"}, {"p": {"type": "noul", "instructions": "?"}}, ctx=chat_only)
    assert not r.available("risk_check", chat_only)
    decisions_only = ctx_for(allowed_providers=["typesafe"])
    with pytest.raises(ModelRouteUnavailable, match="allowed_providers"):
        r.complete_json("planning", "s", "u", ctx=decisions_only)
    assert transport.chat_calls == [] and transport.decide_calls == []
    # The default policy (both providers) routes normally.
    assert r.complete_json("planning", "s", "u", ctx=ctx_for()).data == {"ok": 1}


# --------------------------------------------------------------------- expensive_model_approval_usd
def test_estimate_over_approval_threshold_returns_approval_required():
    sink = Sink()
    transport = FakeTransport(chat=lambda p: chat_json({"ok": 1}))
    r = make(transport, sink)
    with pytest.raises(ApprovalRequired) as exc:
        r.complete_json("planning", "s", "u " * 2000, ctx=ctx_for(expensive_model_approval_usd=0.001))
    assert exc.value.code == "approval_required" and exc.value.details["decision"] == "approval_required"
    assert exc.value.details["estimated_usd"] > 0.001
    assert transport.chat_calls == []  # never called silently
    assert sink.records[-1]["status"] == "approval_required"
    # Under the threshold the call goes ahead.
    assert r.complete_json("planning", "s", "u", ctx=ctx_for(expensive_model_approval_usd=5.0)).data == {"ok": 1}


def test_approval_threshold_prefers_a_fallback_model_within_the_limit():
    seen = []

    def chat(p):
        seen.append(p["model"])
        return chat_json({"ok": 1}, model=p["model"])

    # planning = [claude-sonnet-5 ($3/$15), gpt-5.4 ($2.5/$15)]; low_cost = [flash-lite, deepseek] are cheap.
    r = make(FakeTransport(chat=chat))
    r.complete_json("insight_narrative", "s", "u", ctx=ctx_for(expensive_model_approval_usd=0.01))
    assert seen == ["google/gemini-3.5-flash-lite"]


# --------------------------------------------------------------------- data_residency
def test_residency_with_unknown_region_fails_closed_and_known_region_routes():
    transport = FakeTransport(chat=lambda p: chat_json({"ok": 1}, model=p["model"]))
    with pytest.raises(ModelRouteUnavailable, match="data_residency"):
        make(transport).complete_json("planning", "s", "u", ctx=ctx_for(data_residency="eu"))
    assert transport.chat_calls == []
    config = load_models_config().model_copy(deep=True)
    config.models["openai/gpt-5.4"] = ModelMeta(region="EU", input_usd_per_mtok=2.5, output_usd_per_mtok=15.0)
    resp = make(transport, config=config).complete_json("planning", "s", "u", ctx=ctx_for(data_residency="eu"))
    assert resp.model == "openai/gpt-5.4" and [c["model"] for c in transport.chat_calls] == ["openai/gpt-5.4"]


def test_no_workspace_policy_means_no_workspace_restriction():
    r = make(FakeTransport(chat=lambda p: chat_json({"ok": 1})))
    assert r.complete_json("planning", "s", "u", ctx=CallContext()).data == {"ok": 1}
