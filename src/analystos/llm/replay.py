"""Replayable model calls (P4-C09).

Every model_call row references its redacted request and response in `model_payload`
(content-addressed, zlib-compressed, capped). From those rows this module can

* reconstruct what a run sent to models and what came back (`load_run_calls`, `run_report`),
  including JEV decision answers with their probabilities, and
* re-execute recorded calls with no network: `ReplayTransport` answers a `ModelRouter` from the
  recorded responses, keyed by the exact (redacted) messages or decision state it receives.

What is stored is what left the platform: the router redacts before sending, so the stored
request is the sent request. Responses are redacted too, so a replayed text may differ from the
original where it contained a credential-like string; that is the intended trade.
"""
from __future__ import annotations

import hashlib
import json
import zlib
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from analystos.core.errors import ModelRouteUnavailable
from analystos.core.ids import stable_hash
from analystos.llm.router import JSON_INSTRUCTION, CallContext, ModelRouter, NullSink

MAX_PAYLOAD_BYTES = 1_000_000  # uncompressed canonical JSON; prompts are already bounded by max_prompt_tokens


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False).encode()


def encode_payload(value: Any) -> tuple[str, bytes, int, bool]:
    """(sha256, compressed body, uncompressed size, truncated). An over-cap body is replaced by a
    stub that says so; a payload is never cut mid-JSON."""
    raw = canonical_bytes(value)
    size, truncated = len(raw), False
    if size > MAX_PAYLOAD_BYTES:
        raw = canonical_bytes({"_omitted": "payload exceeded the replay cap", "bytes": size,
                               "sha256": stable_hash(value), "cap": MAX_PAYLOAD_BYTES})
        truncated = True
    return hashlib.sha256(raw).hexdigest(), zlib.compress(raw, 6), size, truncated


def decode_payload(body: bytes | None) -> Any:
    if body is None:
        return None
    return json.loads(zlib.decompress(body).decode())


def chat_key(messages: list[dict[str, Any]]) -> str:
    return stable_hash({"messages": messages})


def decision_key(state: dict[str, Any], questions: dict[str, Any]) -> str:
    return stable_hash({"state": state, "questions": questions})


@dataclass
class RecordedCall:
    id: int
    purpose: str
    status: str
    model: str
    provider: str
    prompt_version: str | None
    agent_id: str | None = None
    task_id: str | None = None
    attempt: int = 0
    request: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    created_at: datetime | None = None

    @property
    def kind(self) -> str:
        return (self.request or {}).get("kind") or ("decision" if self.provider == "typesafe" else "chat")

    @property
    def replayable(self) -> bool:
        return self.request is not None and self.response is not None and "_omitted" not in self.request

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["created_at"] = self.created_at.isoformat() if self.created_at else None
        if self.kind == "decision" and self.response:
            out["decision"] = decision_summary(self.response.get("answers") or {})
        return out


def decision_summary(answers: dict[str, Any]) -> dict[str, Any]:
    """The typed JEV answers per question: value, probabilities, confidence."""
    out = {}
    for q, a in answers.items():
        if not isinstance(a, dict):
            continue
        value = next((a[k] for k in ("choice", "score", "noul") if k in a), None)
        out[q] = {"value": value, "probabilities": a.get("probabilities") or {}, "confidence": a.get("confidence")}
    return out


@dataclass
class ReplayTransport:
    """A `Transport` that answers from recorded responses instead of the network.

    Identical requests recorded several times are answered in recorded order, then the last
    answer repeats. A request with no recording raises ModelRouteUnavailable (a non-retryable
    4xx-like failure), so the router degrades exactly as it would for a rejected model."""

    calls: list[RecordedCall]
    misses: list[str] = field(default_factory=list)
    served: int = 0

    def __post_init__(self) -> None:
        self._chat: dict[str, list[dict]] = defaultdict(list)
        self._decide: dict[str, list[dict]] = defaultdict(list)
        self._next: dict[str, int] = defaultdict(int)
        for c in self.calls:
            if not c.replayable:
                continue
            if c.kind == "decision":
                self._decide[decision_key(c.request["state"], c.request["questions"])].append(c.response)
            else:
                self._chat[chat_key(c.request["messages"])].append(c.response)

    def _take(self, table: dict[str, list[dict]], key: str, what: str) -> dict:
        answers = table.get(key)
        if not answers:
            self.misses.append(key)
            raise ModelRouteUnavailable(f"replay: no recorded {what} response for this request ({key[:12]})")
        i = self._next[key]
        self._next[key] = i + 1
        self.served += 1
        return answers[min(i, len(answers) - 1)]

    def chat(self, *, base_url: str, api_key: str, payload: dict, timeout: float) -> dict:
        r = self._take(self._chat, chat_key(payload["messages"]), "chat")
        return {"model": r.get("model") or payload.get("model"), "choices": [{"message": {"content": r.get("text") or ""}}],
                "usage": r.get("usage") or {}}

    def decide(self, *, base_url: str, api_key: str, payload: dict, timeout: float) -> dict:
        r = self._take(self._decide, decision_key(payload["state"], payload["questions"]), "decision")
        return {"model": r.get("model") or payload.get("model"), "answers": r.get("answers") or {}, "usage": r.get("usage") or {}}


def _replay_settings():
    from analystos.contracts.platform import LLMSettings, PlatformSettings

    # Every purpose on, no cache, no size refusal, no budget downgrade: the recording decides.
    return PlatformSettings(llm=LLMSettings(cache_enabled=False, max_prompt_tokens=400_000,
                                            downgrade_below_budget_fraction=0.0))


def replay_router(calls: list[RecordedCall], *, sink: Any = None) -> ModelRouter:
    """A router that re-executes recorded calls offline (no API key, no network)."""
    from analystos.llm.cache import ResponseCache

    settings = _replay_settings()
    return ModelRouter(transport=ReplayTransport(calls), sink=sink or NullSink(), api_key_lookup=lambda _env: "replay",
                       max_retries=0, settings_provider=lambda: settings, cache=ResponseCache(None))


def verify_replay(calls: list[RecordedCall]) -> dict[str, Any]:
    """Re-issue every replayable recorded call through a ModelRouter on a ReplayTransport and
    check the router returns the recorded output. Proves the stored inputs are complete."""
    router = replay_router(calls)
    checked, mismatches = 0, []
    for c in calls:
        if not c.replayable or c.status not in ("ok", "cache_hit"):
            continue
        req = c.request
        try:
            if c.kind == "decision":
                got = router.decide(c.purpose, req["state"], req["questions"], ctx=CallContext()).answers
                same = got == (c.response.get("answers") or {})
            else:
                messages = [dict(m) for m in req["messages"]]
                if req.get("json_output") and messages and messages[0]["content"].endswith(JSON_INSTRUCTION):
                    messages[0]["content"] = messages[0]["content"][: -len(JSON_INSTRUCTION)]
                got = router.complete(c.purpose, messages, ctx=CallContext(), json_output=bool(req.get("json_output")),
                                      max_tokens=req.get("max_tokens")).text
                same = got == (c.response.get("text") or "")
        except Exception as exc:  # a replay failure is a finding, not a crash
            mismatches.append({"id": c.id, "purpose": c.purpose, "error": str(exc)[:300]})
            continue
        checked += 1
        if not same:
            mismatches.append({"id": c.id, "purpose": c.purpose, "error": "output differs from the recording"})
    transport: ReplayTransport = router.transport  # type: ignore[assignment]
    return {"checked": checked, "matched": checked - sum(1 for m in mismatches if "differs" in m["error"]),
            "mismatches": mismatches, "network_calls": 0, "served_from_recording": transport.served}


def load_run_calls(run_id: str) -> list[RecordedCall]:
    """Every model_call of a run with its request/response bodies, in call order."""
    from sqlalchemy import select

    from analystos.db.base import session_scope
    from analystos.db.models import ModelCall, ModelPayload

    with session_scope() as s:
        rows = list(s.scalars(select(ModelCall).where(ModelCall.run_id == run_id).order_by(ModelCall.id)))
        refs = {r for row in rows for r in (row.request_ref, row.response_ref) if r}
        bodies = {p.hash: decode_payload(p.body) for p in s.scalars(select(ModelPayload).where(ModelPayload.hash.in_(refs)))} \
            if refs else {}
        return [RecordedCall(id=row.id, purpose=row.purpose, status=row.status, model=row.model, provider=row.provider,
                             prompt_version=row.prompt_version, agent_id=row.agent_id, task_id=row.task_id, attempt=row.attempt,
                             request=bodies.get(row.request_ref) if row.request_ref else None,
                             response=bodies.get(row.response_ref) if row.response_ref else None, created_at=row.created_at)
                for row in rows]


def run_report(run_id: str, *, check: bool = False) -> dict[str, Any]:
    calls = load_run_calls(run_id)
    report: dict[str, Any] = {
        "run_id": run_id,
        "summary": {"calls": len(calls), "with_request": sum(1 for c in calls if c.request is not None),
                    "with_response": sum(1 for c in calls if c.response is not None),
                    "decisions": sum(1 for c in calls if c.kind == "decision" and c.response is not None),
                    "statuses": dict(sorted(Counter(c.status for c in calls).items()))},
        "calls": [c.to_dict() for c in calls],
    }
    if check:
        report["replay"] = verify_replay(calls)
    return report
