import pytest
from tests.fakes import FakeTransport, chat_json

from analystos.core.errors import ModelRouteUnavailable, UpstreamUnavailable
from analystos.llm.jev import JevDecisions
from analystos.llm.router import CallContext, ModelRouter, parse_json_text

KEY = {"OPENROUTER_API_KEY": "sk-test-000000000000000000000000"}


class Sink:
    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)

    def check_budget(self, ctx, purpose):
        return None


def make(transport, sink=None):
    return ModelRouter(transport=transport, sink=sink or Sink(), api_key_lookup=KEY.get, max_retries=0)


def test_json_completion_and_usage_recorded():
    sink = Sink()
    r = make(FakeTransport(chat=lambda p: chat_json({"ok": True})), sink)
    resp = r.complete_json("planning", "sys", "user")
    assert resp.data == {"ok": True} and resp.cost_usd == 0.001
    assert sink.records[-1]["status"] == "ok" and sink.records[-1]["purpose"] == "planning"


def test_fallback_to_next_model_on_transient_error():
    seen = []

    def chat(p):
        seen.append(p["model"])
        return UpstreamUnavailable("503") if len(seen) == 1 else chat_json({"x": 1}, model=p["model"])

    resp = make(FakeTransport(chat=chat)).complete_json("planning", "s", "u")
    assert seen == ["anthropic/claude-sonnet-5", "openai/gpt-5.4"] and resp.model == "openai/gpt-5.4"


def test_workspace_allowlist_narrowing_fails_closed():
    r = make(FakeTransport(chat=lambda p: chat_json({})))
    with pytest.raises(ModelRouteUnavailable):
        r.complete("planning", [{"role": "user", "content": "x"}], ctx=CallContext(allowed_models=["some/other-model"]))


def test_verification_excludes_primary_family():
    r = make(FakeTransport())
    _, _, models = r.candidates("verification", CallContext(exclude_families=["openai"]))
    assert models and all(not m.startswith(("anthropic/", "openai/")) for m in models)


def test_no_key_means_unavailable_not_silent_reroute():
    r = ModelRouter(transport=FakeTransport(), sink=Sink(), api_key_lookup=lambda _: None)
    assert r.available("planning") is False
    with pytest.raises(ModelRouteUnavailable):
        r.complete("planning", [{"role": "user", "content": "x"}])


def test_secrets_are_redacted_before_leaving():
    t = FakeTransport(chat=lambda p: chat_json({}))
    make(t).complete("planning", [{"role": "user", "content": "use key sk-or-v1-abcdefghijklmnopqrstuvwxyz and password=hunter2 "
                                                                "postgresql://u:secretpw@h/db"}])
    sent = t.chat_calls[0]["messages"][0]["content"]
    assert "abcdefghijklmnop" not in sent and "hunter2" not in sent and "secretpw" not in sent


def test_parse_json_text_handles_fences():
    assert parse_json_text("```json\n{\"a\": 1}\n```") == {"a": 1}
    assert parse_json_text("Sure: {\"a\": [1,2]} done") == {"a": [1, 2]}


def test_jev_score_choice_and_escalation():
    def decide(p):
        q = p["questions"]
        answers = {}
        for k, v in q.items():
            if v["type"] == "score":
                answers[k] = {"type": "score", "score": 1.9, "probabilities": {"2": 0.9}, "confidence": 0.9}
            elif v["type"] == "choice":
                answers[k] = {"type": "choice", "choice": "line", "probabilities": {"line": 0.95, "bar": 0.05}}
            else:
                answers[k] = {"type": "noul", "noul": 0.97}
        return {"model": "typesafe/jev-1.13", "answers": answers, "usage": {"cost": 0.00001}}

    t = FakeTransport(decide=decide)
    jev = JevDecisions(make(t))
    scores = jev.score_hypotheses("objective", {"0": "h zero", "1": "h one"})
    assert set(scores) == {"0", "1"} and scores["0"].value == pytest.approx(1.9)
    assert jev.choose("chart_selection", {"x": "y"}, "pick", {"line": "l", "bar": "b"}).value == "line"
    assert jev.consequential("publish it").value == pytest.approx(0.97)
    assert t.decide_calls[0]["model"] == "typesafe/jev-1.13"


def test_jev_invalid_choice_is_ignored():
    t = FakeTransport(decide=lambda p: {"answers": {"pick": {"choice": "rm -rf", "probabilities": {}}}})
    assert JevDecisions(make(t)).choose("chart_selection", {}, "pick", {"line": "l", "bar": "b"}) is None


def test_jev_unavailable_returns_none():
    r = ModelRouter(transport=FakeTransport(), sink=Sink(), api_key_lookup=lambda _: None)
    assert JevDecisions(r).consequential("x") is None
