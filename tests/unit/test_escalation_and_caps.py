"""Cheap first, escalate (the small tier answers; the large tier only after a deterministic validation
failure, recorded) and hard spend caps (atomic reservations settled to the actual cost, platform day +
workspace month, fail closed without Redis). No services: fake transport, in-memory Redis stand-in."""
import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from tests.fakes import FakeRedis, FakeTransport, chat_json

from analystos.agents import common
from analystos.contracts.platform import PRESET_ESCALATION, PRESETS, LLMSettings, PlatformSettings
from analystos.contracts.policy import WorkspacePolicyDoc
from analystos.core.errors import BudgetExceeded, EscalationUnavailable, SpendCapReached, SpendCountersUnavailable
from analystos.llm.cache import ResponseCache
from analystos.llm.config import load_models_config
from analystos.llm.router import CallContext, ModelRouter
from analystos.runtime.budget_counters import BudgetCounters, CapExceeded, CapSpec, CountersUnavailable

KEY = {"OPENROUTER_API_KEY": "sk-test-000000000000000000000000"}
SMALL, LARGE = "openai/gpt-5.4-mini", "anthropic/claude-sonnet-5"


class Sink:
    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)

    def check_budget(self, ctx, purpose):
        return None


class MemoryCache(ResponseCache):
    def __init__(self):
        super().__init__(None)
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl):
        self.store[key] = value


def make(transport, sink=None, cache=None, **llm):
    platform = PlatformSettings(llm=LLMSettings(**llm))
    return ModelRouter(transport=transport, sink=sink or Sink(), api_key_lookup=KEY.get, max_retries=0,
                       settings_provider=lambda: platform, cache=cache or ResponseCache(None))


def has_sql(r):
    return None if isinstance(r.data, dict) and r.data.get("sql") else "no SQL statement in the answer"


def by_model(answers):
    """A transport answering per requested model (text, or a callable of the payload)."""
    def chat(p):
        a = answers[p["model"]]
        a = a(p) if callable(a) else a
        return {"model": p["model"], "choices": [{"message": {"content": a}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}
    return FakeTransport(chat=chat)


# ------------------------------------------------------------------------------ routing defaults
def test_chat_profiles_are_cheap_first_with_the_large_tier_for_escalation():
    cfg = load_models_config()
    for name in ("reasoning_strong", "coding", "analytical_reasoning"):
        profile = cfg.profiles[name]
        assert LARGE not in profile.models and profile.escalation_models[0] == LARGE, name
        small = cfg.estimate_cost(profile.models[0], 4000, 1000)
        assert small < cfg.estimate_cost(LARGE, 4000, 1000) / 3, name  # the first answer costs a fraction
    r = make(FakeTransport())
    _, profile, first = r.candidates("sql_generation", CallContext())
    assert first[0] == SMALL and r.escalation_policy("sql_generation") == "on_validation_failure"
    assert r.escalation_tier("sql_generation", CallContext(), "coding", profile, first)[0] == LARGE
    assert r.escalation_policy("metadata_enrichment") == "never"  # batch crawl prompts never escalate


def test_max_quality_keeps_sonnet_first():
    r = make(FakeTransport(), escalation=PRESET_ESCALATION["max_quality"], purpose_modes=PRESETS["max_quality"])
    for purpose in ("planning", "hypothesis_generation", "sql_generation", "semantic_modeling", "feedback_interpretation"):
        assert r.candidates(purpose, CallContext())[2][0] == LARGE, purpose
    assert r.candidates("insight_narrative", CallContext())[2][0] == "google/gemini-3.5-flash-lite"  # was small before too


# ------------------------------------------------------------------------------ escalation
def test_a_valid_small_answer_is_used_and_never_escalated():
    sink, t = Sink(), by_model({SMALL: json.dumps({"sql": "select 1"})})
    resp = make(t, sink).complete("sql_generation", [{"role": "user", "content": "u"}], json_output=True, validate=has_sql)
    assert resp.model == SMALL and resp.escalated_from is None and resp.validation_error is None
    assert [c["model"] for c in t.chat_calls] == [SMALL]
    assert [r["status"] for r in sink.records] == ["ok"] and "escalated_from" not in sink.records[0]


def test_unparseable_json_escalates_once_and_is_recorded():
    sink = Sink()
    t = by_model({SMALL: "Sure! SELECT", LARGE: json.dumps({"sql": "select 1"})})
    resp = make(t, sink).complete("sql_generation", [{"role": "user", "content": "u"}], json_output=True, validate=has_sql)
    assert [c["model"] for c in t.chat_calls] == [SMALL, LARGE]  # no retry of the small model, no second small model
    assert resp.model == LARGE and resp.escalated_from == SMALL and resp.escalation_reason.startswith("invalid_json")
    failed, answered = sink.records
    assert failed["status"] == "error" and failed["error"].startswith("invalid_json") and failed["cost_usd"] > 0  # billed
    assert answered["status"] == "ok" and answered["escalated_from"] == SMALL
    assert answered["escalation_reason"].startswith("invalid_json") and answered["answered_by"] == "llm_large"


def test_a_failed_check_escalates_and_a_passed_check_does_not():
    sink = Sink()
    t = by_model({SMALL: json.dumps({"explanation": "no sql"}), LARGE: json.dumps({"sql": "select 1"})})
    resp = make(t, sink).complete("sql_generation", [{"role": "user", "content": "u"}], json_output=True, validate=has_sql)
    assert resp.model == LARGE and resp.escalation_reason == "validation: no SQL statement in the answer"
    assert [r["status"] for r in sink.records] == ["error", "ok"] and sink.records[0]["error"].startswith("validation:")


def test_policy_never_returns_the_small_answer_marked_invalid():
    sink = Sink()
    t = by_model({SMALL: json.dumps({"explanation": "no sql"}), LARGE: json.dumps({"sql": "select 1"})})
    resp = make(t, sink, escalation={"sql_generation": "never"}).complete(
        "sql_generation", [{"role": "user", "content": "u"}], json_output=True, validate=has_sql)
    assert resp.model == SMALL and resp.validation_error == "no SQL statement in the answer"
    assert [c["model"] for c in t.chat_calls] == [SMALL]  # the caller's deterministic path decides


def test_always_large_asks_the_large_tier_first():
    t = by_model({LARGE: json.dumps({"sql": "select 1"})})
    resp = make(t, escalation={"sql_generation": "always_large"}).complete(
        "sql_generation", [{"role": "user", "content": "u"}], json_output=True, validate=has_sql)
    assert resp.model == LARGE and resp.escalated_from is None and [c["model"] for c in t.chat_calls] == [LARGE]


def test_explicit_escalation_after_a_downstream_failure():
    sink = Sink()
    t = by_model({LARGE: json.dumps({"sql": "select 2"})})
    make(t, sink).complete("sql_generation", [{"role": "user", "content": "u"}], json_output=True, validate=has_sql,
                          escalate="sql_rejected after 2 repairs: column x does not exist", escalated_from=SMALL)
    assert [c["model"] for c in t.chat_calls] == [LARGE]
    assert sink.records[-1]["escalated_from"] == SMALL and sink.records[-1]["escalation_reason"].startswith("sql_rejected")
    with pytest.raises(EscalationUnavailable):
        make(t, escalation={"sql_generation": "never"}).complete("sql_generation", [{"role": "user", "content": "u"}],
                                                                 json_output=True, escalate="sql_rejected")


def test_budget_downgrade_does_not_escalate():
    class Low(Sink):
        def remaining_fraction(self, ctx):
            return 0.1

    t = by_model({"google/gemini-3.5-flash-lite": "not json", "deepseek/deepseek-v4-flash": "not json"})
    with pytest.raises(Exception):  # noqa: B017 - every small model failed; no silent jump to Sonnet
        make(t, Low()).complete("planning", [{"role": "user", "content": "u"}], json_output=True)
    assert LARGE not in [c["model"] for c in t.chat_calls]


def test_only_validated_answers_are_cached():
    cache = MemoryCache()
    t = by_model({SMALL: json.dumps({"explanation": "no sql"}), LARGE: json.dumps({"sql": "select 1"})})
    r = make(t, cache=cache)
    first = r.complete("sql_generation", [{"role": "user", "content": "u"}], json_output=True, validate=has_sql)
    again = r.complete("sql_generation", [{"role": "user", "content": "u"}], json_output=True, validate=has_sql)
    assert first.model == LARGE and again.cached and again.data == {"sql": "select 1"}
    assert len(t.chat_calls) == 2  # the escalated answer is served from the L0 cache next time


def test_llm_json_passes_the_callers_check_and_reports_the_escalation(monkeypatch):
    said = []
    sink = Sink()
    t = by_model({"google/gemini-3.5-flash": json.dumps({"hypotheses": []}),
                  LARGE: json.dumps({"hypotheses": [{"statement": "x"}]})})
    platform = PlatformSettings(llm=LLMSettings(cache_enabled=False))
    router = ModelRouter(transport=t, sink=sink, api_key_lookup=KEY.get, max_retries=0, settings_provider=lambda: platform,
                         cache=ResponseCache(None))
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: platform)
    ctx = SimpleNamespace(router=router, policy=WorkspacePolicyDoc(), run=SimpleNamespace(objective="x"),
                          call_ctx=lambda exclude_families=None: CallContext(workspace_id="ws1", run_id="run1"),
                          say=lambda text, **k: said.append(text))
    data, model = common.llm_json(ctx, "hypothesis_generation", "hypothesis_generation.v1", {"objective": "x"},
                                  validate=lambda d: None if (d or {}).get("hypotheses") else "empty hypothesis list")
    assert model == LARGE and data["hypotheses"] and any("escalated to" in s for s in said)


# ------------------------------------------------------------------------------ reservations (counters)
def _caps(c: BudgetCounters, day_cap=2.0, month_cap=50.0, day_seed=0.0, month_seed=0.0):
    return [CapSpec("platform_daily", c.platform_day_key(), day_cap, 3600, lambda: day_seed),
            CapSpec("workspace_monthly", c.workspace_month_key("ws1"), month_cap, 3600, lambda: month_seed)]


def test_reserve_settle_and_release():
    c = BudgetCounters(None, "t:", client=FakeRedis())
    res = c.reserve(_caps(c, day_seed=0.5), 0.2)
    assert res.after == {"platform_daily": pytest.approx(0.7), "workspace_monthly": pytest.approx(0.2)}
    c.settle(res, 0.05)  # actual cost below the estimate
    assert c.value(c.platform_day_key()) == pytest.approx(0.55) and c.value(c.workspace_month_key("ws1")) == pytest.approx(0.05)
    c.settle(res, 1.0)  # idempotent: a reservation settles once
    assert c.value(c.platform_day_key()) == pytest.approx(0.55)
    failed = c.reserve(_caps(c), 0.3)
    c.settle(failed, 0.0)  # a failed call releases its reservation
    assert c.value(c.platform_day_key()) == pytest.approx(0.55)


def test_a_reservation_over_any_cap_reserves_nothing():
    c = BudgetCounters(None, "t:", client=FakeRedis())
    c.reserve(_caps(c, day_seed=1.9), 0.05)
    with pytest.raises(CapExceeded) as exc:
        c.reserve(_caps(c), 0.2)
    assert exc.value.cap.name == "platform_daily" and exc.value.spent == pytest.approx(1.95)
    assert c.value(c.workspace_month_key("ws1")) == pytest.approx(0.05)  # all or nothing


def test_competing_reservations_never_overshoot_the_cap():
    c = BudgetCounters(None, "t:", client=FakeRedis())
    caps = _caps(c, day_cap=1.0)
    granted, refused = [], []

    def call():
        try:
            granted.append(c.reserve(caps, 0.03))
        except CapExceeded:
            refused.append(1)

    threads = [threading.Thread(target=call) for _ in range(100)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(granted) == 33 and len(refused) == 67  # floor(1.0 / 0.03)
    assert c.value(c.platform_day_key()) <= 1.0 + 1e-9


def test_day_counter_rolls_over_at_utc_midnight():
    now = [datetime(2026, 9, 26, 23, 59, 30, tzinfo=UTC)]
    c = BudgetCounters(None, "t:", client=FakeRedis(), clock=lambda: now[0])
    first = c.reserve([CapSpec("platform_daily", c.platform_day_key(), 1.0, 3600, lambda: 0.95)], 0.04)
    with pytest.raises(CapExceeded):
        c.reserve([CapSpec("platform_daily", c.platform_day_key(), 1.0, 3600, lambda: 0.95)], 0.04)
    now[0] += timedelta(minutes=1)  # 00:00:30 UTC next day: a new counter, seeded from that day's rows
    assert c.platform_day_key().endswith(":20260927:usd")
    res = c.reserve([CapSpec("platform_daily", c.platform_day_key(), 1.0, 3600, lambda: 0.0)], 0.04)
    assert res.after["platform_daily"] == pytest.approx(0.04)
    c.settle(first, 0.01)  # settling yesterday's call adjusts yesterday's counter only
    assert c.value(c.platform_day_key()) == pytest.approx(0.04)


def test_reservation_fails_closed_without_redis():
    c = BudgetCounters("redis://127.0.0.1:1/0", "t:")
    with pytest.raises(CountersUnavailable):
        c.reserve(_caps(c), 0.01)


# ------------------------------------------------------------------------------ caps in the router
class CapSink(Sink):
    def __init__(self, counters, caps):
        super().__init__()
        self.counters, self.caps = counters, caps

    def reserve(self, *, ctx, purpose, model, estimate_usd):
        try:
            return self.counters.reserve(self.caps(), estimate_usd)
        except CapExceeded as exc:
            raise SpendCapReached(f"{exc.cap.name} cap reached", details={"cap": exc.cap.name, "remedy": "wait"}) from None

    def record(self, **kw):
        super().record(**kw)
        if kw.get("reservation") is not None:
            self.counters.settle(kw["reservation"], kw["cost_usd"])


def test_a_call_over_the_cap_is_refused_visibly_and_not_sent():
    c = BudgetCounters(None, "t:", client=FakeRedis())
    sink = CapSink(c, lambda: [CapSpec("platform_daily", c.platform_day_key(), 0.02, 3600, lambda: 0.0)])
    t = FakeTransport(chat=lambda p: chat_json({"ok": 1}, model=p["model"], cost=0.002))
    r = make(t, sink)
    r.complete_json("planning", "s", "u")  # reserves the estimate, settles to the reported $0.002
    assert c.value(c.platform_day_key()) == pytest.approx(0.002)
    with pytest.raises(SpendCapReached) as exc:
        r.complete_json("planning", "s", "u " * 3000)  # estimate (4000 output tokens + the prompt) > the $0.018 left
    assert isinstance(exc.value, BudgetExceeded) and exc.value.details["remedy"]
    assert len(t.chat_calls) == 1 and sink.records[-1]["status"] == "refused" and sink.records[-1]["answered_by"] == "rules"


def test_cap_refusal_falls_back_to_the_rules(monkeypatch):
    """The purpose's deterministic path runs: llm_json returns no data and says why (with the remedy)."""
    said = []
    c = BudgetCounters(None, "t:", client=FakeRedis())
    sink = CapSink(c, lambda: [CapSpec("platform_daily", c.platform_day_key(), 0.0, 3600, lambda: 0.0)])
    t = FakeTransport(chat=lambda p: chat_json({"hypotheses": [{"statement": "x"}]}))
    platform = PlatformSettings(llm=LLMSettings(cache_enabled=False))
    router = ModelRouter(transport=t, sink=sink, api_key_lookup=KEY.get, max_retries=0, settings_provider=lambda: platform,
                         cache=ResponseCache(None))
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: platform)
    ctx = SimpleNamespace(router=router, policy=WorkspacePolicyDoc(), run=SimpleNamespace(objective="x"),
                          call_ctx=lambda exclude_families=None: CallContext(workspace_id="ws1", run_id="run1"),
                          say=lambda text, **k: said.append((text, k.get("data"))))
    data, reason = common.llm_json(ctx, "hypothesis_generation", "hypothesis_generation.v1", {"objective": "x"})
    assert data is None and reason == "cap_reached" and t.chat_calls == []  # code from the Ask refusal taxonomy
    assert "deterministic fallback" in said[-1][0] and said[-1][1]["remedy"] == "wait"


def test_db_sink_reserves_under_the_daily_and_monthly_caps_and_alerts_admins(sqlite_db, budget_counters, monkeypatch):
    from sqlalchemy import select

    from analystos.db.base import session_scope
    from analystos.db.models import Notification, RunEvent, User, Workspace
    from analystos.runtime.usage import DbUsageSink

    platform = PlatformSettings(llm=LLMSettings(daily_spend_cap_usd=1.0))
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: platform)
    with session_scope() as s:
        s.add(User(id="u_admin", email="a@x", name="A", is_admin=True, password_hash="x"))
        s.add(Workspace(id="ws1", name="W", created_by="u_admin"))
    sink = DbUsageSink(budget_counters)
    ctx = CallContext(workspace_id="ws1")
    res = sink.reserve(ctx=ctx, purpose="planning", model=SMALL, estimate_usd=0.85)  # crosses 80% of the $1 day cap
    sink.record(ctx=ctx, purpose="planning", profile="reasoning_strong", provider="openrouter", model=SMALL, status="ok",
                attempt=1, latency_ms=5, input_tokens=100, output_tokens=10, cost_usd=0.5, request_hash=None, error=None,
                reservation=res, cost_source="provider", escalated_from=None)
    assert budget_counters.value(budget_counters.platform_day_key()) == pytest.approx(0.5)
    assert budget_counters.value(budget_counters.workspace_month_key("ws1")) == pytest.approx(0.5)  # settled once, not twice
    with pytest.raises(SpendCapReached) as exc:
        sink.reserve(ctx=ctx, purpose="planning", model=SMALL, estimate_usd=0.6)
    assert exc.value.details["cap"] == "platform_daily" and "00:00 UTC" in exc.value.details["remedy"]
    with session_scope() as s:
        kinds = [e.type for e in s.scalars(select(RunEvent))]
        notes = [n.title for n in s.scalars(select(Notification)) if n.user_id == "u_admin"]
    assert kinds.count("budget.warning") == 1 and kinds.count("budget.cap_reached") == 1
    assert any("of the platform daily cap" in n for n in notes) and any("refused" in n for n in notes)


def test_db_sink_fails_closed_when_the_counters_are_down(sqlite_db, monkeypatch):
    from analystos.runtime.usage import DbUsageSink

    class Down(FakeRedis):
        def register_script(self, source):
            def boom(keys, args):
                raise ConnectionError("redis down")
            return boom

    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: PlatformSettings())
    sink = DbUsageSink(BudgetCounters(None, "t:", client=Down()))
    with pytest.raises(SpendCountersUnavailable):
        sink.reserve(ctx=CallContext(), purpose="planning", model=SMALL, estimate_usd=0.01)


def test_unpriced_models_reserve_at_the_most_expensive_known_rates():
    cfg = load_models_config()
    assert cfg.reservation_estimate("typesafe/jev-1.13", 1000, 100) == pytest.approx(
        max(cfg.estimate_cost(m, 1000, 100) for m in cfg.models))
    assert cfg.reservation_estimate(SMALL, 1000, 100) == cfg.estimate_cost(SMALL, 1000, 100)
