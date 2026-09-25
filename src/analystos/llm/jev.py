"""JEV model decisioning: the typed decisions AnalystOS delegates to TypeSafe Jev.

Design rule (ADR-0006): Jev answers *bounded choices* with calibrated probabilities. It never
authorizes anything. It may (a) rank, (b) pick among options the platform already considers
valid, and (c) ESCALATE risk (add an approval), never remove a gate required by policy.
Every helper returns None when Jev is unavailable, and callers fall back to deterministic rules
and record which path decided.
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


class JevDecisions:
    def __init__(self, router: ModelRouter) -> None:
        self.router = router

    def available(self) -> bool:
        return self.router.available("risk_check")

    def _call(self, purpose: str, state: dict, questions: dict, ctx: CallContext | None) -> dict | None:
        if not self.router.available(purpose, ctx):
            return None
        try:
            response = self.router.decide(purpose, state, questions, ctx=ctx)
        except AnalystOSError as exc:
            log.warning("jev %s unavailable: %s", purpose, exc)
            return None
        return {"answers": response.answers, "model": response.model}

    def score_hypotheses(self, objective: str, hypotheses: dict[str, str], ctx: CallContext | None = None) -> dict[str, Verdict] | None:
        """Priority score 0..2 (low/medium/high) for each hypothesis w.r.t. the objective."""
        if not hypotheses:
            return {}
        state = {"objective": objective, **{f"hypothesis_{k}": v for k, v in hypotheses.items()}}
        questions = {
            f"priority_{k}": {"type": "score", "criteria": ["low", "medium", "high"],
                              "instructions": f"How valuable is testing `hypothesis_{k}` for answering `objective`? "
                                              "High = likely to reveal an actionable driver of the objective."}
            for k in hypotheses
        }
        result = self._call("hypothesis_priority", state, questions, ctx)
        if not result:
            return None
        out: dict[str, Verdict] = {}
        for k in hypotheses:
            answer = result["answers"].get(f"priority_{k}") or {}
            if isinstance(answer.get("score"), (int, float)):
                out[k] = Verdict(float(answer["score"]), answer.get("probabilities") or {}, answer.get("confidence"), result["model"])
        return out

    def consequential(self, text: str, ctx: CallContext | None = None) -> Verdict | None:
        """P(request asks to change/delete/move/publish/schedule/send/grant). Used ONLY to escalate."""
        result = self._call("risk_check", {"request": text}, {
            "consequential": {"type": "noul", "instructions": "Does `request` ask to change, delete, move, publish, schedule, "
                              "send, export or grant access to data or systems (not merely read or analyse)?"}}, ctx)
        if not result:
            return None
        value = (result["answers"].get("consequential") or {}).get("noul")
        return Verdict(float(value), {}, None, result["model"]) if isinstance(value, (int, float)) else None

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
        return Verdict(choice, probs, answer.get("confidence"), result["model"])

    def probability(self, purpose: str, state: dict[str, Any], instructions: str, ctx: CallContext | None = None) -> Verdict | None:
        result = self._call(purpose, state, {"p": {"type": "noul", "instructions": instructions}}, ctx)
        if not result:
            return None
        value = (result["answers"].get("p") or {}).get("noul")
        return Verdict(float(value), {}, None, result["model"]) if isinstance(value, (int, float)) else None
