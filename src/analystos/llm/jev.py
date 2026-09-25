"""JEV model decisioning: the typed decisions AnalystOS delegates to TypeSafe Jev.

Design rule (ADR-0006): Jev answers *bounded choices* with calibrated probabilities. It never
authorizes anything. It may (a) rank, (b) pick among options the platform already considers
valid, and (c) ESCALATE risk (add an approval), never remove a gate required by policy.
Every helper returns None when Jev is unavailable, and callers fall back to deterministic rules
and record which path decided. Call sites go through `analystos.decisions.DecisionService`, which
uses this class as its `jev` backend with `strict=True` (errors raise, so its circuit breaker sees
outages) and enforces each purpose's authority class (ADR-0015).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from analystos.core.errors import AnalystOSError
from analystos.core.logging import get_logger
from analystos.llm.router import CallContext, ModelRouter

log = get_logger(__name__)


@dataclass
class Verdict:
    value: Any
    probabilities: dict[str, float]
    confidence: float | None
    model: str
    by: str = "jev"
    cost_usd: float = 0.0


CONSEQUENTIAL_INSTRUCTIONS = ("Does `request` ask to change, delete, move, publish, schedule, send, export or grant access to "
                              "data or systems (not merely read or analyse)?")


class JevDecisions:
    def __init__(self, router: ModelRouter, *, strict: bool = False) -> None:
        self.router = router
        self.strict = strict  # raise provider errors instead of returning None (DecisionService breaker)

    def available(self) -> bool:
        return self.router.available("risk_check")

    def _call(self, purpose: str, state: dict, questions: dict, ctx: CallContext | None) -> dict | None:
        mode = self.router.mode(purpose)
        if mode in ("off", "auto"):
            # auto for a decision purpose = the deterministic rule decides (admin control plane)
            self.router.record_skip(purpose, ctx, estimated_tokens=400, reason=f"mode={mode}: rule decision")
            return None
        if not self.router.available(purpose, ctx):
            return None
        try:
            response = self.router.decide(purpose, state, questions, ctx=ctx)
        except AnalystOSError as exc:
            log.warning("jev %s unavailable: %s", purpose, exc)
            if self.strict:
                raise
            return None
        return {"answers": response.answers, "model": response.model, "cost": response.cost_usd}

    def score_hypotheses(self, objective: str, hypotheses: dict[str, str], ctx: CallContext | None = None) -> dict[str, Verdict] | None:
        """Priority score 0..2 (low/medium/high) for each hypothesis w.r.t. the objective."""
        return self.score_items(
            "hypothesis_priority", {"objective": objective}, hypotheses, item_prefix="hypothesis", question_prefix="priority",
            instructions="How valuable is testing `hypothesis_{k}` for answering `objective`? "
                         "High = likely to reveal an actionable driver of the objective.", ctx=ctx)

    def score_items(self, purpose: str, state: dict[str, Any], items: dict[str, str], *, instructions: str,
                    criteria: list[str] | None = None, item_prefix: str = "option", question_prefix: str = "score",
                    ctx: CallContext | None = None) -> dict[str, Verdict] | None:
        """Score each item on `criteria` (index = score). `instructions` may reference `{k}`."""
        if not items:
            return {}
        full_state = {**state, **{f"{item_prefix}_{k}": v for k, v in items.items()}}
        questions = {f"{question_prefix}_{k}": {"type": "score", "criteria": list(criteria or ["low", "medium", "high"]),
                                                "instructions": instructions.replace("{k}", k)} for k in items}
        result = self._call(purpose, full_state, questions, ctx)
        if not result:
            return None
        out: dict[str, Verdict] = {}
        for k in items:
            answer = result["answers"].get(f"{question_prefix}_{k}") or {}
            if isinstance(answer.get("score"), (int, float)):
                out[k] = Verdict(float(answer["score"]), answer.get("probabilities") or {}, answer.get("confidence"), result["model"],
                                 cost_usd=result.get("cost", 0.0))
        return out

    def consequential(self, text: str, ctx: CallContext | None = None) -> Verdict | None:
        """P(request asks to change/delete/move/publish/schedule/send/grant). Used ONLY to escalate."""
        return self.probability("risk_check", {"request": text}, CONSEQUENTIAL_INSTRUCTIONS, ctx, key="consequential")

    def choose(self, purpose: str, state: dict[str, Any], instructions: str, options: dict[str, str],
               ctx: CallContext | None = None) -> Verdict | None:
        """Pick one of `options` (key -> description). Options are always platform-valid choices."""
        if len(options) < 2:
            return None
        result = self._call(purpose, state, {"pick": {"type": "choice", "instructions": instructions,
                                                      "criteria": {k[:80]: v[:400] for k, v in options.items()}}}, ctx)
        if not result:
            return None
        answer = result["answers"].get("pick") or {}
        choice = answer.get("choice")
        if choice not in options:
            return None
        probs = {str(k): float(v) for k, v in (answer.get("probabilities") or {}).items() if k in options}
        return Verdict(choice, probs, answer.get("confidence"), result["model"], cost_usd=result.get("cost", 0.0))

    def probability(self, purpose: str, state: dict[str, Any], instructions: str, ctx: CallContext | None = None,
                    *, key: str = "p") -> Verdict | None:
        result = self._call(purpose, state, {key: {"type": "noul", "instructions": instructions}}, ctx)
        if not result:
            return None
        value = (result["answers"].get(key) or {}).get("noul")
        return Verdict(float(value), {}, None, result["model"], cost_usd=result.get("cost", 0.0)) \
            if isinstance(value, (int, float)) else None
