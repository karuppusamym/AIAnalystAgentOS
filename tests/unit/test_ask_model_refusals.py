"""Ask generation refusals name their one cause (no services): the router's reason travels through
`llm_json` (ModelOutcome) and `sql_agent.ask` (ModelUnavailable) to one refusal kind with its own
remedy, instead of "no model route" for every cause."""
from __future__ import annotations

import pytest
from tests.fakes import FakeTransport, chat_json
from tests.unit.test_prompt_payload import KEY, Sink, _ctx

from analystos.agents import common, sql_agent
from analystos.contracts.platform import LLMSettings, PlatformSettings
from analystos.contracts.policy import WorkspacePolicyDoc
from analystos.core.errors import BudgetExceeded, ModelUnavailable, ProviderQuotaExhausted
from analystos.llm import router as router_mod
from analystos.llm.cache import ResponseCache
from analystos.llm.router import ModelRouter
from analystos.services import ask as ask_svc


def _router(transport=None, *, keys=KEY.get, sink=None, **llm):
    platform = PlatformSettings(llm=LLMSettings(cache_enabled=False, **llm))
    router = ModelRouter(transport=transport or FakeTransport(chat=lambda p: chat_json({"sql": "select 1"})), sink=sink or Sink(),
                         api_key_lookup=keys, max_retries=0, settings_provider=lambda: platform, cache=ResponseCache(None))
    return router, platform


def _generate(monkeypatch, router, platform, policy=None):
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: platform)
    return common.llm_json(_ctx(router, policy), "sql_generation", "sql_generation.v1", {"question": "q"},
                           prompt_vars={"dialect": "postgres"})


@pytest.fixture(autouse=True)
def _no_cooldown():
    router_mod._PROVIDER_COOLDOWN.clear()
    yield
    router_mod._PROVIDER_COOLDOWN.clear()


# ------------------------------------------------------------------------------ the reason, per cause
def test_mode_off(monkeypatch):
    router, platform = _router(purpose_modes={"sql_generation": "off"})
    data, why = _generate(monkeypatch, router, platform)
    assert data is None and why == "mode_off" and why.code == "mode_off"


def test_no_api_key_names_the_variable(monkeypatch):
    transport = FakeTransport()
    router, platform = _router(transport, keys=lambda env: None)
    data, why = _generate(monkeypatch, router, platform)
    assert data is None and why == "no_api_key" and why.details["env"] == "OPENROUTER_API_KEY"
    assert transport.chat_calls == []


def test_provider_cooldown_after_402_says_when_it_retries(monkeypatch):
    transport = FakeTransport(chat=lambda p: ProviderQuotaExhausted("model provider refused for credits HTTP 402: no credits"))
    router, platform = _router(transport)
    data, why = _generate(monkeypatch, router, platform)
    assert data is None and why == "provider_cooldown" and 0 < why.details["retry_in_s"] <= router_mod.PROVIDER_COOLDOWN_SECONDS
    calls = len(transport.chat_calls)
    data, again = _generate(monkeypatch, router, platform)  # within the cooldown: no provider call at all
    assert data is None and again == "provider_cooldown" and again.details["retry_in_s"] > 0 and len(transport.chat_calls) == calls


def test_policy_blocked(monkeypatch):
    router, platform = _router()
    data, why = _generate(monkeypatch, router, platform, WorkspacePolicyDoc(allowed_providers=["typesafe"]))
    assert why == "policy_blocked" and "allowed_providers" in why.message


def test_residency_blocked(monkeypatch):
    router, platform = _router()
    data, why = _generate(monkeypatch, router, platform, WorkspacePolicyDoc(data_residency="mars"))
    assert why == "residency_blocked" and why.details["data_residency"] == "mars"


def test_approval_required(monkeypatch):
    router, platform = _router()
    data, why = _generate(monkeypatch, router, platform, WorkspacePolicyDoc(expensive_model_approval_usd=0.0))
    assert why == "approval_required"


def test_budget_exceeded_and_cap_reached(monkeypatch):
    class Over(Sink):
        def __init__(self, exc):
            super().__init__()
            self.exc = exc

        def check_budget(self, ctx, purpose):
            raise self.exc

    router, platform = _router(sink=Over(BudgetExceeded("workspace monthly model budget $50.00 exhausted")))
    assert _generate(monkeypatch, router, platform)[1] == "budget_exceeded"
    cap = BudgetExceeded("per-run cap for sql_generation exhausted: calls 3 of 3",
                         details={"purpose": "sql_generation", "cap": "calls", "used": 3, "limit": 3})
    router, platform = _router(sink=Over(cap))
    why = _generate(monkeypatch, router, platform)[1]
    assert why == "cap_reached" and why.details["limit"] == 3


def test_context_over_budget(monkeypatch):
    router, platform = _router(max_prompt_tokens=500)
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: platform)
    data, why = common.llm_json(_ctx(router), "sql_generation", "sql_generation.v1", {"question": "q " * 5000},
                                prompt_vars={"dialect": "postgres"})
    assert data is None and why == "context_over_budget"


def test_invalid_output(monkeypatch):
    transport = FakeTransport(chat=lambda p: {"model": "m", "choices": [{"message": {"content": "SELECT 1 -- not json"}}], "usage": {}})
    router, platform = _router(transport)
    assert _generate(monkeypatch, router, platform)[1] == "invalid_output"


def test_router_unavailable_mirrors_available():
    router, _ = _router()
    assert router.available("sql_generation") and router.unavailable("sql_generation") is None
    router, _ = _router(keys=lambda env: None)
    assert not router.available("sql_generation") and router.unavailable("sql_generation").code == "no_api_key"


# ------------------------------------------------------------------------------ Ask: kind + remedy
def _ask_refusal(monkeypatch, outcome):
    from tests.unit.test_ask_threads import _ctx as ask_ctx

    from analystos.registries.verified_queries import Lookup

    monkeypatch.setattr(sql_agent, "_authorize_ask", lambda ctx: None)
    monkeypatch.setattr(sql_agent, "_check_budget", lambda ctx: None)
    monkeypatch.setattr(sql_agent, "catalog_for_prompt", lambda ctx, **kw: "catalog")
    monkeypatch.setattr(sql_agent, "compile_for", lambda ctx, purpose, required, **kw: required)
    monkeypatch.setattr(sql_agent, "_registry_lookup", lambda ctx, q, p: Lookup(None))
    monkeypatch.setattr(sql_agent, "llm_json", lambda *a, **k: (None, outcome) if not isinstance(outcome, dict) else (outcome, "m"))
    ctx, _ = ask_ctx()
    with pytest.raises(ModelUnavailable) as err:
        sql_agent.ask(ctx, "Which vendors were late twice in a row?")
    assert ctx.services.gateway.calls == []
    return ask_svc.refusal_for(err.value)


@pytest.mark.parametrize(("code", "details", "kind", "words"), [
    ("mode_off", {}, "mode_off", ["sql_generation purpose to off", "Admin > Models"]),
    ("no_api_key", {"env": "OPENROUTER_API_KEY"}, "no_api_key",
     ["OPENROUTER_API_KEY", "api and worker containers", ".env", "docker compose up", "restart"]),
    ("provider_cooldown", {"retry_in_s": 42}, "provider_cooldown", ["402", "credits were exhausted", "42 s"]),
    ("policy_blocked", {}, "policy_blocked", ["allowed_providers"]),
    ("residency_blocked", {"data_residency": "eu"}, "residency_blocked", ["data_residency (eu)"]),
    ("approval_required", {}, "approval_required", ["approval threshold", "unpriced"]),
    ("budget_exceeded", {}, "model_budget", ["model budget"]),
    ("cap_reached", {"cap": "calls", "used": 3, "limit": 3}, "cap_reached", ["calls: 3 of 3"]),
    ("context_over_budget", {}, "context_over_budget", ["fewer tables"]),
    ("invalid_output", {}, "invalid_output", ["Ask again"]),
    ("upstream_unavailable", {}, "unavailable", ["Try again"]),
    ("no_route", {}, "no_model", ["verified query"]),
])
def test_each_reason_is_its_own_refusal_kind_with_its_remedy(monkeypatch, code, details, kind, words):
    r = _ask_refusal(monkeypatch, common.ModelOutcome(code, f"because {code}", **details))
    assert r["kind"] == kind and r["title"] == ask_svc.REFUSALS[kind]["title"]
    assert code in r["message"] and r["details"]["reason"] == code
    for w in words:
        assert w in r["remedy"], (w, r["remedy"])
    assert "{" not in r["remedy"]


def test_an_answer_without_sql_is_invalid_output(monkeypatch):
    r = _ask_refusal(monkeypatch, {"explanation": "I cannot"})
    assert r["kind"] == "invalid_output"


def test_every_model_refusal_kind_exists_with_title_and_remedy():
    for kind in set(ask_svc.MODEL_REFUSALS.values()) | {"no_model"}:
        assert ask_svc.REFUSALS[kind]["title"] and ask_svc.REFUSALS[kind]["remedy"]
    no_key = ask_svc.refusal("no_api_key", "x")  # remedy placeholders have defaults
    assert "OPENROUTER_API_KEY" in no_key["remedy"] and "{" not in no_key["remedy"]
