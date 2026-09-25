"""P4-T08: DecisionService backends, authority classes (one test per class), timeout, circuit breaker,
JEV outage -> rules (recorded), ADR-0015 purpose changes and the new purposes' rules."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from tests.fakes import FakeTransport, chat_json

from analystos.contracts.platform import DETERMINISTIC_CAPABLE, PlatformSettings
from analystos.core.errors import ModelRouteUnavailable
from analystos.decisions import Question, authority
from analystos.decisions import breaker as breaker_mod
from analystos.decisions.backends import LocalClassifierBackend, build_backends
from analystos.decisions.config import load, parse
from analystos.decisions.service import DecisionService
from analystos.decisions.store import MemoryDecisionStore
from analystos.decisions.types import Proposal
from analystos.llm.cache import ResponseCache
from analystos.llm.config import load_models_config
from analystos.llm.jev import JevDecisions
from analystos.llm.router import ModelRouter

KEY = {"OPENROUTER_API_KEY": "sk-test-000000000000000000000000"}


class Sink:
    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)

    def check_budget(self, ctx, purpose):
        return None


@pytest.fixture(autouse=True)
def _fresh_breakers():
    breaker_mod.reset_all()
    yield
    breaker_mod.reset_all()


def router(transport=None, settings=None, sink=None):
    platform = settings or PlatformSettings()
    return ModelRouter(transport=transport or FakeTransport(), sink=sink or Sink(), api_key_lookup=KEY.get, max_retries=0,
                       settings_provider=lambda: platform, cache=ResponseCache(None))


def service(transport=None, *, settings=None, store=None, config=None, sink=None):
    r = router(transport, settings, sink)
    return DecisionService(r, store=store if store is not None else MemoryDecisionStore(), config=config)


def jev_answers(answers, model="typesafe/jev-1.13"):
    return FakeTransport(decide=lambda p: {"model": model, "answers": answers, "usage": {"cost": 0.00002}})


def model_first(*purposes):
    """The pre-P4-T02 default: the model answers first, so its proposal is what authority enforces."""
    s = PlatformSettings()
    return s.model_copy(update={"llm": s.llm.model_copy(update={"purpose_modes": {p: "always" for p in purposes}})})


def with_backends(**orders):
    s = model_first(*orders)
    return s.model_copy(update={"decisions": s.decisions.model_copy(update={"backends": orders})})


# ------------------------------------------------------------------------------------ config
def test_every_purpose_has_a_rule_path_and_rules_first_where_the_class_needs_it():
    cfg = load()
    routing = load_models_config().routing
    for name, spec in cfg.purposes.items():
        assert "rules" in spec.backends, name
        assert routing.get(name) == "decision", f"{name} must be routed for JEV"
        if spec.authority in ("escalate_only", "bounded_stop"):
            assert spec.backends[0] == "rules", name
        assert spec.timeout_seconds == 3
    for new in ("ask_route", "clarify_needed", "metric_match", "join_path_choice"):
        assert new in cfg.purposes and new in DETERMINISTIC_CAPABLE
    assert (cfg.get("ask_route").authority, cfg.get("clarify_needed").authority, cfg.get("metric_match").authority,
            cfg.get("join_path_choice").authority) == ("route", "escalate_only", "rank", "rank")
    # ADR-0015 purpose changes
    assert cfg.get("chart_selection").backends[0] == "rules" and cfg.get("stop_check").backends[0] == "rules"
    assert cfg.get("alert_triage").backends[0] == "rules" and cfg.get("alert_triage").authority == "escalate_only"
    assert routing.get("decision_structured") == "low_cost"


def test_config_rejects_a_model_first_escalation_or_a_purpose_without_rules():
    with pytest.raises(ValueError, match="rules"):
        parse({"purposes": {"x": {"kind": "probability", "authority": "escalate_only", "backends": ["jev", "rules"]}}})
    with pytest.raises(ValueError, match="rules"):
        parse({"purposes": {"x": {"kind": "choice", "authority": "route", "backends": ["jev"]}}})
    spec = load().get("stop_check")
    assert spec.ordered(["jev"]) == ["rules", "jev"]  # an admin override cannot put a model before the rule
    assert load().get("feedback_classification").ordered(["local_classifier"]) == ["local_classifier", "rules"]


# ------------------------------------------------------------------------------------- rank
def test_rank_backend_cannot_drop_an_option_and_unknown_options_are_ignored():
    answers = {"priority_0": {"score": 2, "probabilities": {"high": 0.9}, "confidence": 0.8},
               "priority_9": {"score": 2}}  # 1 dropped, 9 unknown
    t = jev_answers(answers)
    svc = service(t, settings=model_first("hypothesis_priority"))
    d = svc.decide("hypothesis_priority", {"objective": "o"},
                   Question.scores("rank", {"0": "h0", "1": "h1"}, hint={"0": 0.0, "1": 1.0}))
    assert d.backend == "jev" and d.value == {"0": 2.0, "1": 1.0}
    assert d.details["0"]["by"] == "jev" and d.details["1"]["by"] == "rules"
    assert any("dropped" in n for n in d.enforced)
    # the JEV payload is exactly what JevDecisions.score_hypotheses sends (replay/cache compatible)
    direct = jev_answers(answers)
    JevDecisions(router(direct, model_first("hypothesis_priority"))).score_hypotheses("o", {"0": "h0", "1": "h1"})
    assert t.decide_calls[0]["state"] == direct.decide_calls[0]["state"]
    assert t.decide_calls[0]["questions"] == direct.decide_calls[0]["questions"]


def test_rank_with_no_valid_score_falls_back_to_rules():
    d = service(jev_answers({"priority_0": {"score": "high"}}), settings=model_first("hypothesis_priority")).decide(
        "hypothesis_priority", {"objective": "o"}, Question.scores("rank", {"0": "h0"}, hint={"0": 2.0}))
    assert d.backend == "rules" and d.value == {"0": 2.0} and "jev" in d.fallback_reason


# ------------------------------------------------------------------------ choose_presentation
def test_chart_selection_is_decided_by_rules_and_jev_is_not_called():
    t = jev_answers({"pick": {"choice": "pie", "probabilities": {"pie": 0.99}}})
    sink = Sink()
    d = service(t, sink=sink).decide("chart_selection", {"chart_title": "x"},
                                     Question.choice("which?", {"bar": "b", "pie": "p"}, hint="bar"))
    assert d.value == "bar" and d.backend == "rules" and t.decide_calls == []
    assert any(r["status"] == "skipped" and r["purpose"] == "chart_selection" for r in sink.records)  # shown as savings


def test_choose_presentation_answer_outside_the_valid_options_is_ignored():
    t = jev_answers({"pick": {"choice": "rm -rf", "probabilities": {"rm -rf": 1.0}}})
    d = service(t).decide("chart_selection", {"chart_title": "x"}, Question.choice("which?", {"bar": "b", "pie": "p"}))
    assert len(t.decide_calls) == 1  # a rule tie (no hint) reaches JEV ...
    assert d.value in ("bar", "pie") and d.backend == "default"  # ... whose invalid answer is refused
    assert d.attempts[-1]["outcome"] == "refused"
    ok = service(jev_answers({"pick": {"choice": "pie", "probabilities": {"pie": 0.7, "bar": 0.3}}})).decide(
        "chart_selection", {"chart_title": "x"}, Question.choice("which?", {"bar": "b", "pie": "p"}))
    assert ok.value == "pie" and ok.backend == "jev" and ok.probabilities == {"pie": 0.7, "bar": 0.3}


# ------------------------------------------------------------------------------------ route
def test_route_answer_outside_the_valid_paths_is_ignored():
    kinds = {"redirect": "r", "question": "q"}
    d = service(jev_answers({"pick": {"choice": "grant_admin"}}), settings=model_first("feedback_classification")).decide(
        "feedback_classification", {"feedback": "x"}, Question.choice("kind?", kinds))
    assert d.value == "redirect" and d.backend == "rules" and "refused" in d.fallback_reason
    ok = service(jev_answers({"pick": {"choice": "question", "probabilities": {"question": 0.8}}}),
                 settings=model_first("feedback_classification")).decide(
        "feedback_classification", {"feedback": "x"}, Question.choice("kind?", kinds))
    assert ok.value == "question" and ok.backend == "jev" and ok.fallback_reason is None


def test_ask_route_rules_are_tool_first_and_only_ties_reach_a_model():
    q = lambda: Question.choice("route?", {"verified_query": "v", "tool": "t", "generate": "g", "decline": "d"})  # noqa: E731
    svc = service(jev_answers({"pick": {"choice": "tool", "probabilities": {"tool": 0.9}}}))
    assert svc.decide("ask_route", {"question": "q"}, q(), facts={"verified_match": 0.95}).value == "verified_query"
    assert svc.decide("ask_route", {"question": "q"}, q(), facts={"missing_inputs": ["period"]}).value == "decline"
    assert svc.decide("ask_route", {"question": "q"}, q(), facts={}).value == "generate"
    tie = svc.decide("ask_route", {"question": "q"}, q(), facts={"verified_match": 0.85, "tool_match": 0.82})
    assert tie.backend == "jev" and tie.value == "tool"


# ---------------------------------------------------------------------------- escalate_only
def _triage_q(baseline):
    return Question.escalation("material?", levels=["info", "warning", "critical"], baseline=baseline, escalate_to="critical",
                               escalate_at=0.8)


def test_escalate_only_not_material_cannot_lower_severity():
    t = jev_answers({"p": {"noul": 0.01}})
    svc = service(t)
    warn = svc.decide("alert_triage", {"signal": "s"}, _triage_q("warning"), facts={"points": 12})
    assert warn.value == "warning" and warn.p == 0.01 and warn.backend == "jev"  # "not material" did not lower it
    crit = svc.decide("alert_triage", {"signal": "s"}, _triage_q("critical"), facts={"points": 12})
    assert crit.value == "critical" and len(t.decide_calls) == 1  # already the top level: no model call at all
    up = service(jev_answers({"p": {"noul": 0.95}})).decide("alert_triage", {"signal": "s"}, _triage_q("warning"),
                                                            facts={"points": 12})
    assert up.value == "critical" and up.details["baseline"]["material"] is True


def test_escalate_only_refuses_a_lowering_proposal_whatever_the_backend():
    q = Question.escalation("?", levels=["info", "warning", "critical"], baseline="critical", escalate_to="info", escalate_at=0.5)
    level, notes = authority.escalate(q, "critical", 0.99)
    assert level == "critical" and "refused to lower" in notes[0]

    class Lowering:  # a rules replacement that tries to lower the baseline
        def propose(self, *a):
            return Proposal(value="info")

    backends = {**build_backends(router()), "rules": Lowering()}
    d = DecisionService(router(), store=MemoryDecisionStore(), backends=backends).decide(
        "alert_triage", {"signal": "s"}, _triage_q("warning"))
    assert d.value == "warning" and any("refused to lower" in n for n in d.enforced)


def test_alert_materiality_rules_keep_noise_away_from_the_model():
    t = jev_answers({"p": {"noul": 0.99}})
    d = service(t).decide("alert_triage", {"signal": "s"}, _triage_q("warning"),
                          facts={"points": 12, "effect": 0.01, "min_effect": 0.05})
    assert d.value == "warning" and t.decide_calls == [] and d.details["material"] is False
    assert "below" in d.details["reasons"][0]


def test_risk_check_rule_escalates_and_the_model_cannot_undo_it():
    t = jev_answers({"consequential": {"noul": 0.0}})
    q = lambda: Question.escalation("?", key="consequential", levels=["read", "consequential"], baseline="read",  # noqa: E731
                                    escalate_to="consequential", escalate_at=0.5)
    d = service(t).decide("risk_check", {"request": "please publish the dashboard"}, q())
    assert d.value == "consequential" and d.p == 1.0 and t.decide_calls == []
    read = service(t).decide("risk_check", {"request": "focus on network incidents"}, q())
    assert read.value == "read" and t.decide_calls[0]["questions"] == {
        "consequential": {"type": "noul", "instructions": "?"}}


def test_clarify_needed_asks_when_an_input_is_missing_and_a_model_can_only_add_a_question():
    q = lambda: Question.escalation("ambiguous?", levels=["answer", "clarify"], baseline="answer",  # noqa: E731
                                    escalate_to="clarify", escalate_at=0.7)
    svc = service(jev_answers({"p": {"noul": 0.1}}))
    assert svc.decide("clarify_needed", {"question": "q"}, q(), facts={"missing_inputs": ["metric"]}).value == "clarify"
    assert svc.decide("clarify_needed", {"question": "q"}, q()).value == "answer"
    assert service(jev_answers({"p": {"noul": 0.9}})).decide("clarify_needed", {"question": "q"}, q()).value == "clarify"


# ----------------------------------------------------------------------------- bounded_stop
def _stop(svc, **facts):
    return svc.decide("stop_check", {"objective": "o", "findings": "f"}, Question.probability("answered?"), facts=facts)


def test_bounded_stop_never_stops_before_the_minimum_work():
    t = jev_answers({"p": {"noul": 0.99}})
    svc = service(t)
    assert _stop(svc, round=0, supported=5).value is False and t.decide_calls == []  # before min rounds: rule, no call
    few = _stop(svc, round=1, supported=2)
    assert few.value is False and few.backend == "jev" and "refused stop" in few.enforced[0]
    assert _stop(svc, round=1, supported=3).value is True


def test_stop_check_rule_first_stops_when_the_last_round_found_nothing_new():
    t = jev_answers({"p": {"noul": 0.0}})
    d = _stop(service(t), round=2, supported=1, new_supported_last_round=0)
    assert d.value is True and d.backend == "rules" and t.decide_calls == []


# ------------------------------------------------------------------ timeout, breaker, outage
def test_timeout_falls_back_to_rules_and_is_recorded():
    cfg = parse({"defaults": {"timeout_seconds": 0.1},
                 "purposes": {"feedback_classification": {"kind": "choice", "authority": "route", "backends": ["jev", "rules"],
                                                          "default": "redirect"}}})

    def slow(p):
        time.sleep(0.5)
        return {"answers": {"pick": {"choice": "question"}}}

    store = MemoryDecisionStore()
    started = time.perf_counter()
    d = service(FakeTransport(decide=slow), store=store, config=cfg, settings=model_first("feedback_classification")).decide(
        "feedback_classification", {"feedback": "x"}, Question.choice("kind?", {"redirect": "r", "question": "q"}))
    assert time.perf_counter() - started < 0.4
    assert d.backend == "rules" and "timeout" in d.fallback_reason and store.decisions[0].fallback_reason == d.fallback_reason


def test_jev_outage_opens_the_breaker_then_a_half_open_probe_recovers():
    cfg = parse({"defaults": {"breaker_failures": 2, "breaker_reset_seconds": 0.05},
                 "purposes": {"feedback_classification": {"kind": "choice", "authority": "route", "backends": ["jev", "rules"],
                                                          "default": "redirect"}}})
    state = {"down": True}

    def decide(p):
        if state["down"]:
            return ModelRouteUnavailable("decisions API HTTP 404")
        return {"answers": {"pick": {"choice": "question"}}}

    t = FakeTransport(decide=decide)
    store = MemoryDecisionStore()
    svc = service(t, store=store, config=cfg, settings=model_first("feedback_classification"))
    q = lambda: Question.choice("kind?", {"redirect": "r", "question": "q"})  # noqa: E731
    first = [svc.decide("feedback_classification", {"feedback": "x"}, q()) for _ in range(3)]
    assert [d.backend for d in first] == ["rules"] * 3  # JEV outage -> rules
    assert len(t.decide_calls) == 2  # third decision: circuit open, JEV not called
    assert first[2].attempts[0]["outcome"] == "breaker_open" and "failed" in first[0].fallback_reason
    assert all(d.fallback_reason for d in store.decisions)  # every fallback is recorded
    state["down"] = False
    time.sleep(0.06)
    probe = svc.decide("feedback_classification", {"feedback": "x"}, q())
    assert probe.backend == "jev" and probe.value == "question"
    assert breaker_mod.breaker("jev").state == "closed"


def test_calibration_downgrade_skips_the_backend_visibly():
    t = jev_answers({"pick": {"choice": "question"}})
    d = service(t, store=MemoryDecisionStore({("feedback_classification", "jev")})).decide(
        "feedback_classification", {"feedback": "x"}, Question.choice("kind?", {"redirect": "r", "question": "q"}))
    assert d.backend == "rules" and t.decide_calls == []
    assert d.attempts[0] == {"backend": "jev", "outcome": "downgraded", "reason": "calibration below threshold"}
    assert "downgraded" in d.fallback_reason


# ------------------------------------------------------------------- other backends
def test_local_classifier_is_deterministic_and_usable_as_a_backend():
    local = LocalClassifierBackend()
    kinds = {"redirect": "", "add_context": "", "reject_finding": "", "deeper_analysis": "", "question": ""}
    pick = lambda text: local.propose("feedback_classification", {"feedback": text}, {},  # noqa: E731
                                      Question.choice("kind?", kinds, default="redirect"), None)
    assert pick("Exclude the network team, focus only on hardware").value == "redirect"
    assert pick("This finding is wrong and misleading").value == "reject_finding"
    assert pick("Please drill down deeper into region").value == "deeper_analysis"
    risk = local.propose("risk_check", {"request": "publish and email it"}, {}, Question.probability("?"), None)
    assert risk.value > 0.9 and risk.model == "local:risk_check@1"
    svc = service(FakeTransport(), settings=with_backends(feedback_classification=["local_classifier"]))
    d = svc.decide("feedback_classification", {"feedback": "that is wrong"}, Question.choice("kind?", kinds))
    assert d.backend == "local_classifier" and d.value == "reject_finding" and abs(sum(d.probabilities.values()) - 1) < 1e-4


def test_llm_structured_answers_a_strict_schema_and_a_bad_answer_falls_back():
    good = FakeTransport(chat=lambda p: chat_json({"choice": "question", "probabilities": {"question": 0.8}, "confidence": 0.8},
                                                  model="google/gemini-3.5-flash-lite"))
    settings = with_backends(feedback_classification=["llm_structured"])
    q = lambda: Question.choice("kind?", {"redirect": "r", "question": "q"})  # noqa: E731
    d = service(good, settings=settings).decide("feedback_classification", {"feedback": "x"}, q())
    assert d.backend == "llm_structured" and d.value == "question" and d.model == "google/gemini-3.5-flash-lite"
    assert "JSON schema" in good.chat_calls[0]["messages"][0]["content"]
    bad = FakeTransport(chat=lambda p: chat_json({"choice": "question", "grant": True}))
    fallback = service(bad, settings=settings).decide("feedback_classification", {"feedback": "x"}, q())
    assert fallback.backend == "rules" and "schema" in fallback.fallback_reason


def test_mode_auto_means_the_rule_decides_and_the_skip_is_counted():
    s = PlatformSettings()
    s = s.model_copy(update={"llm": s.llm.model_copy(update={"purpose_modes": {"feedback_classification": "auto"}})})
    sink, t = Sink(), jev_answers({"pick": {"choice": "question"}})
    d = service(t, settings=s, sink=sink).decide("feedback_classification", {"feedback": "x"},
                                                  Question.choice("kind?", {"redirect": "r", "question": "q"}))
    assert d.backend == "rules" and t.decide_calls == [] and sink.records[-1]["status"] == "skipped"


# ------------------------------------------------------------------- new purposes: rules
def test_metric_match_and_join_path_rules():
    svc = service(FakeTransport())  # no JEV answers -> rules
    m = svc.decide("metric_match", {"question": "what is the breach rate by team"},
                   Question.scores("match?", {"sla_breach_rate": "share of incidents breaching SLA", "record_count": "rows"}))
    assert m.value["sla_breach_rate"] > m.value["record_count"] and set(m.value) == {"sla_breach_rate", "record_count"}
    paths = {"a": {"hops": 1, "confidence": 0.9}, "b": {"hops": 2, "confidence": 0.99}}
    j = svc.decide("join_path_choice", {"question": "q"}, Question.scores("path?", {"a": "a", "b": "b"}), facts={"paths": paths})
    assert j.backend == "rules" and j.value["a"] > j.value["b"]


# ------------------------------------------------------------------- monitors call site
def test_triage_call_site_cannot_suppress_investigation(monkeypatch):
    from analystos import decisions
    from analystos.runtime import context
    from analystos.services import monitors as mon
    from analystos.services import platform_settings

    svc = service(jev_answers({"p": {"noul": 0.0}}))
    monkeypatch.setattr(decisions, "decision_service", lambda _router=None: svc)
    monkeypatch.setattr(context, "default_router", lambda: None)
    monkeypatch.setattr(context, "workspace_call_ctx", lambda *a, **k: None)
    monkeypatch.setattr(platform_settings, "get", lambda: PlatformSettings())
    monitor = SimpleNamespace(id="mon_1", name="m", workspace_id="ws_1", kind="metric_drift", config={"metric": "x"})
    severity, triage = mon._triage(monitor, SimpleNamespace(id="ws_1", objective="o"),
                                   {"severity": "warning", "message": "up", "points": 12, "pct_change": 0.4, "period": "2026-08"})
    assert severity == "warning" and triage["material"] is True and triage["p_material"] == 0.0 and triage["by"] == "jev"
    assert svc.store.decisions[-1].subject.startswith("alert:")
