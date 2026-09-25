"""Decision vocabulary (ADR-0015): what a caller asks, what a backend proposes, what the service decides.

A backend *proposes*; the service applies the purpose's authority class and *decides*. The
`Decision` returned to callers is always the enforced value, never a raw backend answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Kind = Literal["choice", "probability", "scores"]
AuthorityClass = Literal["rank", "choose_presentation", "escalate_only", "route", "bounded_stop"]
AUTHORITY_CLASSES: tuple[str, ...] = ("rank", "choose_presentation", "escalate_only", "route", "bounded_stop")
BACKENDS: tuple[str, ...] = ("jev", "rules", "local_classifier", "llm_structured")
MODEL_BACKENDS = frozenset({"jev", "llm_structured"})  # remote: timeout + circuit breaker + record_skip

# Classes the spec marks "calibration needed" (Brier on a labelled set); every class is still measured.
CALIBRATION_REQUIRED = frozenset({"route", "bounded_stop"})


@dataclass
class Question:
    """What the caller asks. `options` are always platform-valid (for scores: the items to rank).

    `hint` is a deterministic answer the caller already computed (the chart rules' pick, the
    hypotheses' priority labels); the `rules` backend uses it where the purpose's rule needs it.
    For `escalate_only`: `levels` is the ordered scale, `baseline` the level the platform already
    assigned, and a model probability at or beyond `escalate_at` proposes `escalate_to`
    (`escalate_when="low"`: strictly below, e.g. P(evidence supports claim) < 0.3 -> doubt)."""

    kind: Kind
    instructions: str
    options: dict[str, str] = field(default_factory=dict)
    hint: Any = None
    key: str | None = None  # JEV question key (kept stable so recorded calls replay)
    criteria: list[str] = field(default_factory=lambda: ["low", "medium", "high"])
    levels: list[str] = field(default_factory=list)
    baseline: str | None = None
    escalate_to: str | None = None
    escalate_at: float = 0.5
    escalate_when: Literal["high", "low"] = "high"
    default: Any = None

    @classmethod
    def choice(cls, instructions: str, options: dict[str, str], *, hint: Any = None, default: Any = None) -> Question:
        return cls(kind="choice", instructions=instructions, options=dict(options), hint=hint, default=default)

    @classmethod
    def scores(cls, instructions: str, items: dict[str, str], *, hint: dict[str, float] | None = None,
               criteria: list[str] | None = None) -> Question:
        return cls(kind="scores", instructions=instructions, options=dict(items), hint=hint,
                   criteria=list(criteria or ["low", "medium", "high"]))

    @classmethod
    def probability(cls, instructions: str, *, key: str | None = None, hint: Any = None) -> Question:
        return cls(kind="probability", instructions=instructions, key=key, hint=hint)

    @classmethod
    def escalation(cls, instructions: str, *, levels: list[str], baseline: str, escalate_to: str, escalate_at: float,
                   escalate_when: Literal["high", "low"] = "high", key: str | None = None, hint: Any = None) -> Question:
        return cls(kind="probability", instructions=instructions, key=key, hint=hint, levels=list(levels), baseline=baseline,
                   escalate_to=escalate_to, escalate_at=escalate_at, escalate_when=escalate_when)


@dataclass
class Proposal:
    """One backend's answer. `final` (rules only) ends the chain: no model is consulted after it."""

    value: Any
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None
    model: str | None = None
    cost_usd: float = 0.0
    final: bool = False
    details: dict[str, Any] = field(default_factory=dict)
    note: str | None = None


@dataclass
class Decision:
    id: str
    purpose: str
    authority: str
    backend: str  # who decided: jev | rules | local_classifier | llm_structured | default
    value: Any  # the ENFORCED answer
    p: float | None = None  # model/rule probability for probability purposes
    proposal: Any = None  # the raw answer of the deciding backend (before enforcement)
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None
    model: str | None = None
    latency_ms: int = 0
    cost_usd: float = 0.0
    fallback_reason: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    enforced: list[str] = field(default_factory=list)  # what the authority class changed or refused
    inputs_hash: str = ""
    subject: str | None = None

    @property
    def by_model(self) -> bool:
        return self.backend in MODEL_BACKENDS

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "purpose": self.purpose, "backend": self.backend, "value": self.value, "p": self.p,
                "model": self.model, "fallback_reason": self.fallback_reason}


class BackendError(Exception):
    """A backend failed (outage, timeout, malformed answer): counts against its circuit breaker."""
