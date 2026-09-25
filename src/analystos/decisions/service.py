"""DecisionService.decide(purpose, state, question) -> Decision (ADR-0015, spec v3 §5).

For each purpose (config/models.yaml `decisions`): walk its backends in order, minus any that
calibration downgraded, and let the purpose's authority class decide what the answer may change.

* Model backends (`jev`, `llm_structured`) run under one deadline per decision (default 3 s) and a
  circuit breaker per backend; a timeout, error or open circuit falls through to the next backend and
  the reason is recorded on the decision (a JEV outage lands on `rules`, visibly).
* `rules` and `local_classifier` run inline: deterministic, local, never timed out.
* `escalate_only` / `bounded_stop` purposes start with `rules`; the rule's answer is the baseline
  (or the minimum-work check) and no model answer can go below it.
* Every decision is stored with its options, enforced answer, raw proposal, probabilities, latency,
  cost, every attempt and what the authority class refused.

`state` goes to models (trusted, redacted text only, as for `ModelRouter.decide`); `facts` are the
caller's deterministic context for rules and authority checks and never leave the platform.
"""
from __future__ import annotations

import contextvars
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from analystos.core.ids import new_id, stable_hash
from analystos.core.logging import get_logger
from analystos.decisions import authority
from analystos.decisions.backends import Abstained, build_backends
from analystos.decisions.breaker import breaker
from analystos.decisions.config import DecisionsConfig, PurposeSpec, load
from analystos.decisions.store import DbDecisionStore, DecisionStore, MemoryDecisionStore
from analystos.decisions.types import MODEL_BACKENDS, BackendError, Decision, Proposal, Question
from analystos.llm.router import CallContext, ModelRouter

log = get_logger(__name__)
_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="decision")


def default_store(router: ModelRouter) -> DecisionStore:
    """Decisions are persisted wherever model calls are (a router with the database usage sink)."""
    from analystos.runtime.usage import DbUsageSink

    return DbDecisionStore() if isinstance(getattr(router, "sink", None), DbUsageSink) else MemoryDecisionStore()


class DecisionService:
    def __init__(self, router: ModelRouter, *, store: DecisionStore | None = None, backends: dict[str, Any] | None = None,
                 config: DecisionsConfig | None = None, settings_provider: Any = None) -> None:
        self.router = router
        self.store = store if store is not None else default_store(router)
        self.backends = backends if backends is not None else build_backends(router)
        self.config = config or load()
        self.settings_provider = settings_provider or router.settings_provider

    # ------------------------------------------------------------------ chain
    def chain(self, spec: PurposeSpec) -> tuple[list[str], list[str]]:
        """(backends to try in order, backends removed by a calibration downgrade)."""
        settings = self.settings_provider().decisions
        order = spec.ordered(settings.backends.get(spec.name))
        down = self.store.downgraded()
        removed = [b for b in order if b != "rules" and (spec.name, b) in down]
        return [b for b in order if b not in removed], removed

    # ------------------------------------------------------------------ decide
    def decide(self, purpose: str, state: dict[str, Any], question: Question, *, facts: dict[str, Any] | None = None,
               ctx: CallContext | None = None, subject: str | None = None) -> Decision:
        spec = self.config.get(purpose)
        facts = {"min_rounds": spec.min_rounds, **(facts or {})}
        if question.default is None:
            question.default = spec.default
        started = time.perf_counter()
        deadline = started + spec.timeout_seconds
        order, removed = self.chain(spec)
        configured_first = spec.ordered(self.settings_provider().decisions.backends.get(spec.name))[0]
        attempts: list[dict[str, Any]] = [{"backend": b, "outcome": "downgraded", "reason": "calibration below threshold"}
                                          for b in removed]
        baseline: Proposal | None = None  # escalate_only: the rule's level
        decided: tuple[str, Proposal, Any, list[str]] | None = None
        rule_scores: dict[str, float] | None = None

        for name in order:
            backend = self.backends.get(name)
            if backend is None:
                attempts.append({"backend": name, "outcome": "abstained", "reason": "backend not installed"})
                continue
            t0 = time.perf_counter()
            try:
                proposal = self._run(name, backend, spec, state, facts, question, ctx, deadline)
            except Abstained as exc:
                attempts.append(_attempt(name, "abstained", str(exc), t0))
                continue
            except _Skipped as exc:
                attempts.append(_attempt(name, exc.outcome, exc.reason, t0))
                continue
            except BackendError as exc:
                attempts.append(_attempt(name, "failed", str(exc), t0))
                continue
            try:
                if spec.kind == "scores" and rule_scores is None:
                    rule_scores = proposal.value if name == "rules" and isinstance(proposal.value, dict) else \
                        self._rule_scores(spec, state, facts, question, ctx)
                value, notes = self._enforce(spec, question, facts, name, proposal, baseline, rule_scores or {})
            except authority.Refused as exc:
                attempts.append(_attempt(name, "refused", str(exc), t0, proposal))
                continue
            attempts.append(_attempt(name, "answered", proposal.note, t0, proposal))
            if spec.authority == "escalate_only" and name == "rules":
                baseline = Proposal(value=value, probabilities=proposal.probabilities, final=proposal.final,
                                    details=proposal.details, note=proposal.note)
                decided = (name, proposal, value, notes)
                if proposal.final:
                    break
                continue
            decided = (name, proposal, value, notes)
            break

        decision = self._finish(spec, question, state, facts, configured_first, attempts, decided, baseline, rule_scores, started, subject)
        self._record_avoided_calls(spec, order, decided, attempts, ctx)
        self.store.record(decision, ctx=ctx, options=question.options or {"levels": question.levels})
        return decision

    # ------------------------------------------------------------------ internals
    def _run(self, name: str, backend: Any, spec: PurposeSpec, state: dict, facts: dict, q: Question,
             ctx: CallContext | None, deadline: float) -> Proposal:
        if name not in MODEL_BACKENDS:
            return backend.propose(spec.name, state, facts, q, ctx)
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise _Skipped("deadline", f"no time left of the {spec.timeout_seconds:g}s decision budget")
        cb = breaker(name, failures=spec.breaker_failures, reset_seconds=spec.breaker_reset_seconds)
        if not cb.allow():
            raise _Skipped("breaker_open", f"circuit open after {cb.consecutive} failures: {cb.last_error or ''}"[:300])
        run = contextvars.copy_context().run
        future = _EXECUTOR.submit(run, backend.propose, spec.name, state, facts, q, ctx)
        try:
            proposal = future.result(timeout=remaining)
        except FutureTimeout:
            future.cancel()
            cb.failure("timeout")
            raise BackendError(f"timeout after {spec.timeout_seconds:g}s") from None
        except BackendError as exc:
            cb.failure(str(exc))
            raise
        except Abstained:
            cb.release()
            raise
        except Exception as exc:  # a backend bug is an outage of that backend, not of the decision
            cb.failure(f"{type(exc).__name__}: {exc}")
            raise BackendError(f"{type(exc).__name__}: {exc}"[:300]) from exc
        cb.success()
        return proposal

    def _rule_scores(self, spec: PurposeSpec, state: dict, facts: dict, q: Question, ctx: CallContext | None) -> dict[str, float]:
        try:
            value = self.backends["rules"].propose(spec.name, state, facts, q, ctx).value
            return value if isinstance(value, dict) else {}
        except (Abstained, KeyError):
            return {}

    def _enforce(self, spec: PurposeSpec, q: Question, facts: dict, name: str, proposal: Proposal,
                 baseline: Proposal | None, rule_scores: dict[str, float]) -> tuple[Any, list[str]]:
        if spec.authority == "rank":
            return authority.rank(q, proposal, rule_scores)
        if spec.authority in ("choose_presentation", "route"):
            return authority.choose(q, proposal, spec.authority), []
        if spec.authority == "escalate_only":
            floor = q.baseline if q.baseline in q.levels else (q.levels[0] if q.levels else None)
            if name == "rules":
                level = proposal.value if proposal.value in q.levels else floor
                if authority.level_index(q, level) < authority.level_index(q, floor):
                    return floor, [f"refused to lower {floor} to {level}"]
                return level, []
            p = authority._number(proposal.value)
            if p is None:
                raise authority.Refused("escalate_only: answer is not a probability")
            return authority.escalate(q, baseline.value if baseline is not None else floor, p)
        # bounded_stop
        if name in MODEL_BACKENDS or name == "local_classifier":
            p = authority._number(proposal.value)
            if p is None:
                raise authority.Refused("bounded_stop: answer is not a probability")
            stop = p >= spec.stop_at
        else:
            stop = bool(proposal.value)
        return authority.bounded_stop(spec, facts, stop, by_model=name != "rules")

    def _finish(self, spec: PurposeSpec, q: Question, state: dict, facts: dict, first: str, attempts: list[dict],
                decided: tuple[str, Proposal, Any, list[str]] | None, baseline: Proposal | None,
                rule_scores: dict[str, float] | None, started: float, subject: str | None) -> Decision:
        if decided is None:
            backend, proposal, value, notes = "default", None, self._default(spec, q, rule_scores), []
        else:
            backend, proposal, value, notes = decided
        failed = [a for a in attempts if a["outcome"] != "answered"]
        fallback = None
        if failed and (decided is None or backend != first or spec.authority == "escalate_only"):
            fallback = "; ".join(f"{a['backend']}: {a['outcome']}" + (f" ({a['reason']})" if a.get("reason") else "")
                                 for a in failed)[:1000]
        probabilities = dict(proposal.probabilities) if proposal else {}
        p = None
        if spec.kind == "probability" and proposal is not None:
            p = authority._number(proposal.value) if backend != "rules" else probabilities.get("yes")
        details: dict[str, Any] = dict(proposal.details) if proposal else {}
        if baseline is not None:
            details["baseline"] = {"level": baseline.value, **baseline.details}
        if spec.kind == "scores" and isinstance(value, dict):
            by_backend = backend if backend != "default" else "rules"
            per_item = (proposal.details if proposal else {}) or {}
            raw = proposal.value if proposal and isinstance(proposal.value, dict) else {}
            details = {k: {"by": by_backend if k in raw else "rules", **(per_item.get(k) or {})} for k in value}
        return Decision(
            id=new_id("dec"), purpose=spec.name, authority=spec.authority, backend=backend, value=value, p=p,
            proposal=proposal.value if proposal else None, probabilities=probabilities,
            confidence=proposal.confidence if proposal else None, model=proposal.model if proposal else None,
            latency_ms=round((time.perf_counter() - started) * 1000), cost_usd=sum(a.get("cost_usd", 0.0) for a in attempts),
            fallback_reason=fallback, attempts=attempts, details=details, enforced=notes,
            inputs_hash=stable_hash({"purpose": spec.name, "state": state, "facts": facts,
                                     "question": {"kind": q.kind, "instructions": q.instructions, "options": q.options,
                                                  "hint": q.hint, "levels": q.levels, "baseline": q.baseline}}),
            subject=subject)

    @staticmethod
    def _default(spec: PurposeSpec, q: Question, rule_scores: dict[str, float] | None) -> Any:
        if spec.kind == "scores":
            top = float(max(len(q.criteria) - 1, 1))
            return {k: (rule_scores or {}).get(k, top / 2) for k in q.options}
        if spec.kind == "choice":
            return q.default if q.default in q.options else next(iter(q.options), None)
        if spec.authority == "escalate_only":
            return q.baseline
        return False  # bounded_stop: nobody decided, so exploration continues

    def _record_avoided_calls(self, spec: PurposeSpec, order: list[str], decided: tuple | None, attempts: list[dict],
                              ctx: CallContext | None) -> None:
        """A rule that decided before JEV avoided a call: show it as savings when JEV could have answered."""
        if decided is None or decided[0] != "rules" or "jev" not in order or order.index("jev") < order.index("rules") \
                or any(a["backend"] == "jev" for a in attempts):
            return
        jev = self.backends.get("jev")
        try:
            if jev is not None and hasattr(jev, "available") and jev.available(spec.name, ctx):
                self.router.record_skip(spec.name, ctx, estimated_tokens=400, reason="rule decided first")
        except Exception as exc:  # noqa: BLE001 - accounting only
            log.debug("skip accounting failed: %s", exc)


class _Skipped(Exception):
    def __init__(self, outcome: str, reason: str) -> None:
        super().__init__(reason)
        self.outcome, self.reason = outcome, reason


def _attempt(name: str, outcome: str, reason: str | None, t0: float, proposal: Proposal | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"backend": name, "outcome": outcome, "ms": round((time.perf_counter() - t0) * 1000)}
    if reason:
        out["reason"] = reason[:300]
    if proposal is not None:
        out["proposal"] = proposal.value if not isinstance(proposal.value, dict) else dict(proposal.value)
        if proposal.model:
            out["model"] = proposal.model
        if proposal.cost_usd:
            out["cost_usd"] = proposal.cost_usd
    return out


def decision_service(router: ModelRouter | None = None) -> DecisionService:
    """The service over the process's default router (database usage sink, so decisions persist)."""
    if router is None:
        from analystos.runtime.context import default_router

        router = default_router()
    return DecisionService(router)
