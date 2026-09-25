"""P4-C09: model calls carry redacted request/response payloads, and a recorded run replays offline."""
import pytest
from tests.fakes import FakeTransport, chat_json

from analystos.contracts.platform import PlatformSettings
from analystos.core.errors import ModelRouteUnavailable
from analystos.llm import replay
from analystos.llm.cache import ResponseCache
from analystos.llm.jev import JevDecisions
from analystos.llm.replay import (
    RecordedCall,
    ReplayTransport,
    decode_payload,
    encode_payload,
    replay_router,
    verify_replay,
)
from analystos.llm.router import CallContext, ModelRouter

KEY = {"OPENROUTER_API_KEY": "sk-test-000000000000000000000000"}


class Sink:
    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)

    def check_budget(self, ctx, purpose):
        return None


def recording_router(sink, transport):
    from analystos.contracts.platform import LLMSettings

    # Model first for the purposes recorded here (their default ladders answer from rules).
    platform = PlatformSettings(llm=LLMSettings(purpose_modes={"chart_selection": "always"}))
    return ModelRouter(transport=transport, sink=sink, api_key_lookup=KEY.get, max_retries=0,
                       settings_provider=lambda: platform, cache=ResponseCache(None))


def as_recorded(records):
    return [RecordedCall(id=i, purpose=r["purpose"], status=r["status"], model=r["model"], provider=r["provider"],
                         prompt_version=r["ctx"].prompt_version, request=r.get("request"), response=r.get("response"))
            for i, r in enumerate(records)]


def test_router_records_redacted_request_and_response_payloads():
    sink = Sink()
    router = recording_router(sink, FakeTransport(chat=lambda p: chat_json({"note": "mail ops@example.com"})))
    router.complete_json("planning", "sys", "contact admin@example.com password=hunter2")
    rec = sink.records[-1]
    sent = rec["request"]["messages"]
    assert rec["request"]["kind"] == "chat" and rec["request"]["json_output"] is True
    assert "admin@example.com" not in str(sent) and "hunter2" not in str(sent) and "[REDACTED_EMAIL]" in sent[1]["content"]
    assert "ops@example.com" not in rec["response"]["text"] and rec["response"]["model"] == "anthropic/claude-sonnet-5"


def test_recorded_run_replays_offline_with_identical_outputs_and_jev_probabilities():
    sink = Sink()
    answers = {"pick": {"choice": "bar", "probabilities": {"bar": 0.8, "line": 0.2}, "confidence": 0.7}}
    live = FakeTransport(chat=lambda p: chat_json({"questions": ["q1"]}), decide=lambda p: {"answers": answers, "usage": {}})
    router = recording_router(sink, live)
    ctx = CallContext(run_id="run1", prompt_version="planning.v1@abc")
    first = router.complete_json("planning", "sys", '{"objective":"x"}', ctx=ctx)
    verdict = JevDecisions(router).choose("chart_selection", {"title": "t"}, "which chart?", {"bar": "b", "line": "l"}, ctx=ctx)
    assert verdict.probabilities == {"bar": 0.8, "line": 0.2}

    calls = as_recorded(sink.records)
    decision = next(c for c in calls if c.kind == "decision")
    assert decision.to_dict()["decision"]["pick"] == {"value": "bar", "probabilities": {"bar": 0.8, "line": 0.2}, "confidence": 0.7}

    offline = replay_router(calls)
    assert isinstance(offline.transport, ReplayTransport)
    again = offline.complete_json("planning", "sys", '{"objective":"x"}')
    assert again.data == first.data and again.text == first.text
    replayed = JevDecisions(offline).choose("chart_selection", {"title": "t"}, "which chart?", {"bar": "b", "line": "l"})
    assert replayed.value == "bar" and replayed.probabilities == verdict.probabilities
    assert len(live.chat_calls) == 1 and len(live.decide_calls) == 1  # replay never touched the live transport

    report = verify_replay(calls)
    assert report["checked"] == 2 and report["matched"] == 2 and not report["mismatches"]


def test_unrecorded_request_fails_visibly_in_replay():
    offline = replay_router([])
    with pytest.raises(ModelRouteUnavailable, match="replay"):
        offline.complete_json("planning", "sys", "never recorded")
    assert offline.transport.misses


def test_payload_encoding_is_content_addressed_compressed_and_capped(monkeypatch):
    body = {"messages": [{"role": "user", "content": "x" * 10_000}]}
    h1, blob, size, truncated = encode_payload(body)
    h2, *_ = encode_payload({"messages": [{"content": "x" * 10_000, "role": "user"}]})  # key order irrelevant
    assert h1 == h2 and len(blob) < size and not truncated and decode_payload(blob) == body
    monkeypatch.setattr(replay, "MAX_PAYLOAD_BYTES", 1_000)
    _, blob, size, truncated = encode_payload(body)
    stub = decode_payload(blob)
    assert truncated and stub["_omitted"] and stub["bytes"] == size  # a stub, never a cut body
