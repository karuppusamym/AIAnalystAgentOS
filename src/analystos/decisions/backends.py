"""DecisionService backends. Each `propose()` returns a Proposal, raises `Abstained` (nothing to
say: mode off/auto, no key, a rule tie) or raises `BackendError` (a failure that counts against the
backend's circuit breaker). None of them decides: the service enforces the authority class.

jev               TypeSafe Jev through ModelRouter.decide (payloads identical to llm/jev.py, so
                  recorded calls replay and caches hit)
rules             analystos.decisions.rules, inline, never timed out
local_classifier  keyword-weighted logistic/softmax model from config/decisions/local_classifier.yaml
                  (no network, no dependency: the air-gapped option)
llm_structured    a small chat model (purpose `decision_structured`) answering a strict JSON schema
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from analystos.core.config import REPO_ROOT
from analystos.core.errors import AnalystOSError
from analystos.decisions.rules import RULES
from analystos.decisions.types import BackendError, Proposal, Question
from analystos.llm.jev import JevDecisions
from analystos.llm.router import CallContext, ModelRouter


class Abstained(Exception):
    """The backend has nothing to say for this question (not a failure)."""


SKIP_TOKENS = 400  # estimated tokens of a decision call avoided by a rule (shown as savings)


def _mode_gate(router: ModelRouter, purpose: str, ctx: CallContext | None) -> None:
    mode = router.mode(purpose)
    if mode in ("off", "auto"):
        # auto for a decision purpose = the deterministic rule decides (admin control plane)
        router.record_skip(purpose, ctx, estimated_tokens=SKIP_TOKENS, reason=f"mode={mode}: rule decision")
        raise Abstained(f"mode={mode}")


class RulesBackend:
    name = "rules"

    def propose(self, purpose: str, state: dict, facts: dict, q: Question, ctx: CallContext | None) -> Proposal:
        fn = RULES.get(purpose)
        if fn is None:
            raise Abstained("no rule for this purpose")
        out = fn(state, facts, q)
        if out is None:
            raise Abstained("rule tie")
        return out


class JevBackend:
    name = "jev"

    def __init__(self, router: ModelRouter) -> None:
        self.router = router
        self.jev = JevDecisions(router, strict=True)

    def available(self, purpose: str, ctx: CallContext | None) -> bool:
        return self.router.mode(purpose) not in ("off", "auto") and self.router.available(purpose, ctx)

    def propose(self, purpose: str, state: dict, facts: dict, q: Question, ctx: CallContext | None) -> Proposal:
        _mode_gate(self.router, purpose, ctx)
        if not self.router.available(purpose, ctx):
            raise Abstained("unavailable (no route, key or provider cooling down)")
        try:
            if q.kind == "scores":
                if not q.options:
                    raise Abstained("nothing to rank")
                verdicts = (self.jev.score_hypotheses(str(state.get("objective", "")), q.options, ctx=ctx)
                            if purpose == "hypothesis_priority" else
                            self.jev.score_items(purpose, state, q.options, instructions=q.instructions, criteria=q.criteria, ctx=ctx))
                if not verdicts:
                    raise BackendError("jev returned no scores")
                first = next(iter(verdicts.values()))
                return Proposal(value={k: v.value for k, v in verdicts.items()}, model=first.model, cost_usd=first.cost_usd,
                                details={k: {"probabilities": v.probabilities, "confidence": v.confidence} for k, v in verdicts.items()})
            if q.kind == "choice":
                if len(q.options) < 2:
                    raise Abstained("fewer than two options")
                result = self.jev._call(purpose, state, {"pick": {"type": "choice", "instructions": q.instructions,
                                                                  "criteria": {k[:80]: v[:400] for k, v in q.options.items()}}}, ctx)
                if not result:
                    raise Abstained("unavailable")
                answer = result["answers"].get("pick") or {}
                probs = {str(k): float(v) for k, v in (answer.get("probabilities") or {}).items() if isinstance(v, (int, float))}
                return Proposal(value=answer.get("choice"), probabilities=probs, confidence=answer.get("confidence"),
                                model=result["model"], cost_usd=result.get("cost", 0.0))
            verdict = self.jev.probability(purpose, state, q.instructions, ctx, key=q.key or "p")
            if verdict is None:
                raise BackendError("jev answer had no probability")
            p = min(max(float(verdict.value), 0.0), 1.0)
            return Proposal(value=p, probabilities={"yes": p, "no": round(1 - p, 6)}, model=verdict.model, cost_usd=verdict.cost_usd)
        except AnalystOSError as exc:
            raise BackendError(f"{exc.code}: {exc.message}"[:300]) from exc


# ----------------------------------------------------------------------------- local classifier
LOCAL_MODEL_PATH = REPO_ROOT / "config" / "decisions" / "local_classifier.yaml"
_WORD = re.compile(r"[a-z0-9]+")


@lru_cache
def load_local_model(path: Path = LOCAL_MODEL_PATH) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}


def _features(state: dict) -> list[str]:
    text = " ".join(str(v) for v in state.values() if isinstance(v, (str, int, float)))
    words = _WORD.findall(text.lower())
    return words + [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]


def _linear(weights: dict[str, Any], feats: list[str]) -> float:
    words = weights.get("words") or {}
    return float(weights.get("bias", 0.0)) + sum(float(words.get(f, 0.0)) for f in feats)


class LocalClassifierBackend:
    """Keyword features, fixed weights: deterministic and explainable. Train offline from
    decision_outcome labels and ship the weights file; no network and no ML dependency at runtime."""

    name = "local_classifier"

    def __init__(self, model: dict[str, Any] | None = None) -> None:
        self.model = model if model is not None else load_local_model()

    def propose(self, purpose: str, state: dict, facts: dict, q: Question, ctx: CallContext | None) -> Proposal:
        spec = self.model.get(purpose)
        if not spec:
            raise Abstained("no local model for this purpose")
        feats = _features(state)
        version = str(spec.get("version", "1"))
        if q.kind == "probability":
            p = 1 / (1 + math.exp(-_linear(spec, feats)))
            return Proposal(value=round(p, 6), probabilities={"yes": round(p, 6), "no": round(1 - p, 6)},
                            model=f"local:{purpose}@{version}")
        if q.kind == "choice":
            classes = {k: v for k, v in (spec.get("classes") or {}).items() if k in q.options}
            if not classes:
                raise Abstained("local model knows none of the options")
            logits = {k: _linear(v, feats) for k, v in classes.items()}
            top = max(logits.values())
            exp = {k: math.exp(v - top) for k, v in logits.items()}
            total = sum(exp.values())
            probs = {k: round(v / total, 6) for k, v in exp.items()}
            best = max(probs, key=lambda k: (probs[k], k == q.default))
            return Proposal(value=best, probabilities=probs, confidence=probs[best], model=f"local:{purpose}@{version}")
        raise Abstained("local model does not rank")


# ------------------------------------------------------------------------------ llm_structured
class _Choice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    choice: str
    probabilities: dict[str, float] = Field(default_factory=dict)
    confidence: float | None = Field(None, ge=0, le=1)


class _Probability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p: float = Field(ge=0, le=1)


class _Scores(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scores: dict[str, float]


_SCHEMAS: dict[str, type[BaseModel]] = {"choice": _Choice, "probability": _Probability, "scores": _Scores}
STRUCTURED_PURPOSE = "decision_structured"


class LlmStructuredBackend:
    name = "llm_structured"

    def __init__(self, router: ModelRouter) -> None:
        self.router = router

    def propose(self, purpose: str, state: dict, facts: dict, q: Question, ctx: CallContext | None) -> Proposal:
        _mode_gate(self.router, purpose, ctx)
        if self.router.mode(STRUCTURED_PURPOSE) == "off" or not self.router.available(STRUCTURED_PURPOSE, ctx):
            raise Abstained("unavailable")
        schema = _SCHEMAS[q.kind]
        kind_rule: dict[str, Any] = {
            "choice": {"choice": "exactly one key of `options`"},
            "probability": {"p": "probability in [0, 1] that the answer to `question` is yes"},
            "scores": {"scores": f"a number 0..{len(q.criteria) - 1} ({'/'.join(q.criteria)}) for every key of `options`"},
        }[q.kind]
        system = ("You answer one bounded decision for an analytics platform. Reply with a single JSON object that "
                  f"matches this JSON schema exactly, with no other keys: {json.dumps(schema.model_json_schema())}. "
                  f"Meaning: {json.dumps(kind_rule)}. Only the listed options are valid answers.")
        user = json.dumps({"purpose": purpose, "state": state, "question": q.instructions, "options": q.options}, default=str)
        try:
            resp = self.router.complete_json(STRUCTURED_PURPOSE, system, user, ctx=ctx, max_tokens=300)
            parsed = schema.model_validate(resp.data)
        except ValidationError as exc:
            raise BackendError(f"llm_structured answer failed the schema: {exc.errors()[0].get('msg', '')}"[:300]) from exc
        except AnalystOSError as exc:
            raise BackendError(f"{exc.code}: {exc.message}"[:300]) from exc
        except (ValueError, TypeError) as exc:
            raise BackendError(f"llm_structured answer unreadable: {exc}"[:300]) from exc
        if isinstance(parsed, _Choice):
            return Proposal(value=parsed.choice, probabilities={k: v for k, v in parsed.probabilities.items() if k in q.options},
                            confidence=parsed.confidence, model=resp.model, cost_usd=resp.cost_usd)
        if isinstance(parsed, _Probability):
            return Proposal(value=parsed.p, probabilities={"yes": parsed.p, "no": round(1 - parsed.p, 6)}, model=resp.model,
                            cost_usd=resp.cost_usd)
        return Proposal(value=dict(parsed.scores), model=resp.model, cost_usd=resp.cost_usd)


def build_backends(router: ModelRouter) -> dict[str, Any]:
    return {"rules": RulesBackend(), "jev": JevBackend(router), "local_classifier": LocalClassifierBackend(),
            "llm_structured": LlmStructuredBackend(router)}
