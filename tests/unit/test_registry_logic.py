"""Registries (P4-T05) without services: question normalisation, typed parameter extraction and
rendering, promotion templating, novelty config, and the replay router that turns models off."""
from __future__ import annotations

import pytest

from analystos.core.errors import InvalidInput, LLMDisabled
from analystos.registries import replay
from analystos.registries import verified_queries as vq

PRIORITY = {"name": "priority", "type": "enum", "required": True, "values": ["1 - Critical", "2 - High", "3 - Moderate"]}


def test_tokens_drop_stop_words_placeholders_and_plurals_and_apply_synonyms():
    lex = vq.Lexicon(phrases={"resolver team": "assignment group"})
    assert vq.tokens("How many incidents per resolver team in {month}?", lex) == {"count", "incident", "by", "assignment", "group"}
    assert vq.tokens("Number of Incidents by assignment group") == vq.tokens("how many incident by assignment_group")


def test_extract_takes_typed_values_and_blanks_them():
    params = [PRIORITY, {"name": "since", "type": "date"}, {"name": "limit", "type": "number"},
              {"name": "caller", "type": "string"}]
    values, rest = vq.extract("Top 5 incidents with priority 2 - high since March 2024 for 'Ann Lee'", params)
    assert values == {"priority": "2 - High", "since": "2024-03-01", "limit": 5, "caller": "Ann Lee"}
    assert "high" not in rest.lower() and "2024" not in rest and "Ann" not in rest
    assert vq.extract("created after 2024-02-15", [{"name": "d", "type": "date"}])[0] == {"d": "2024-02-15"}
    assert vq.extract("no values here", params)[0] == {}


def test_render_quotes_values_and_rejects_values_outside_the_vocabulary():
    params = [PRIORITY, {"name": "n", "type": "number"}, {"name": "who", "type": "string"}]
    sql = vq.render("SELECT * FROM t WHERE p = {{priority}} AND x > {{n}} AND c = {{who}}", params,
                    {"priority": "1 - critical", "n": "3", "who": "O'Brien"}, "postgres")
    assert sql == "SELECT * FROM t WHERE p = '1 - Critical' AND x > 3 AND c = 'O''Brien'"
    with pytest.raises(InvalidInput):
        vq.render("SELECT {{priority}}", [PRIORITY], {"priority": "x'; DROP TABLE t; --"}, "postgres")
    with pytest.raises(InvalidInput):
        vq.coerce({"name": "d", "type": "date"}, "tomorrow")
    with pytest.raises(InvalidInput):
        vq.coerce({"name": "n", "type": "number"}, "1; select 1")


def test_parameterise_makes_parameters_only_of_literals_the_question_names(monkeypatch):
    monkeypatch.setattr(vq, "_vocabulary", lambda session, ws, tables, column: (
        f"{tables[0]}.{column}", ["1 - Critical", "2 - High"] if column == "priority" else None))
    sql = ("SELECT COUNT(*) AS n FROM sn.incident WHERE priority = '1 - Critical' AND state = 'Closed' "
           "AND opened_at >= '2024-01-01' AND reopen_count > 2")
    template, params, pattern = vq.parameterise(None, "ws", sql, "postgres",
                                                "How many incidents with priority 1 - Critical opened since 2024-01-01?")
    assert [(p["name"], p["type"]) for p in params] == [("priority", "enum"), ("opened_at", "date")]
    assert "{{priority}}" in template and "{{opened_at}}" in template
    assert "'Closed'" in template and "> 2" in template  # not named by the question: fixed
    assert pattern == "How many incidents with priority {priority} opened since {opened_at}?"
    rendered = vq.render(template, params, {"priority": "2 - High", "opened_at": "2025-06-01"}, "postgres")
    assert "priority = '2 - High'" in rendered and "opened_at >= '2025-06-01'" in rendered


def test_novelty_config_defaults_off_and_validates():
    assert replay.novelty_config(None) == {"enabled": False, "max_hypotheses": 3, "llm_budget_usd": 0.05}
    assert replay.novelty_config({"enabled": True, "llm_budget_usd": 1})["llm_budget_usd"] == 1.0
    for bad in ({"enabled": "yes"}, {"max_hypotheses": 0}, {"llm_budget_usd": -1}, {"surprise": 1}, "on"):
        with pytest.raises(InvalidInput):
            replay.novelty_config(bad)
    with pytest.raises(InvalidInput):
        replay.validate_schedule_config({"registry_scope": "everything"})


class _Inner:
    def __init__(self):
        self.skips, self.calls = [], []

    def mode(self, purpose):
        return "always"

    def available(self, purpose, ctx=None):
        return True

    def record_skip(self, purpose, ctx, *, estimated_tokens, reason):
        self.skips.append((purpose, reason))

    def complete_json(self, purpose, *a, **k):
        self.calls.append(purpose)
        return "ok"

    settings = "inner-settings"


class _Run:
    def __init__(self, origin):
        self.origin = origin


def test_replay_router_turns_every_purpose_off_except_the_opted_in_novelty_round():
    inner = _Inner()
    router = replay.ReplayRouter(inner)
    assert router.mode("planning") == router.mode("rev_second_opinion") == router.mode("hypothesis_generation") == "off"
    assert not router.available("insight_narrative")
    with pytest.raises(LLMDisabled):
        router.complete_json("insight_narrative", "s", "u")
    router.record_skip("planning", None, estimated_tokens=10, reason="mode=off")
    assert inner.skips == [("planning", "registry replay: mode=off")] and router.settings == "inner-settings"

    novelty = replay.ReplayRouter(inner, replay.NOVELTY_PURPOSES)
    assert novelty.mode("hypothesis_generation") == "always" and novelty.mode("summarization") == "off"
    assert novelty.complete_json("hypothesis_generation", "s", "u") == "ok" and inner.calls == ["hypothesis_generation"]


def test_services_are_wrapped_only_for_scheduled_replay_runs():
    from analystos.runtime.context import Services

    services = Services(router=_Inner(), gateway=None)
    assert replay.services_for(_Run({"type": "user"}), services) is services
    assert replay.services_for(_Run({"type": "schedule", "replay": False}), services) is services
    wrapped = replay.services_for(_Run({"type": "schedule", "replay": True}), services)
    assert isinstance(wrapped.router, replay.ReplayRouter) and wrapped.jev.router is wrapped.router
    assert replay.services_for(_Run({"type": "schedule", "replay": True}), wrapped) is wrapped
    opted = replay.services_for(_Run({"type": "schedule", "replay": True, "novelty": {"enabled": True}}), services)
    assert opted.router.mode("hypothesis_generation") == "always"
