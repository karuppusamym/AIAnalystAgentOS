"""Purpose declarations for the DecisionService, read from the `decisions` section of config/models.yaml.

Validated at load: every purpose names an authority class, only known backends, and includes
`rules` (the deterministic path that `off`/`auto` modes and outages fall back to). `escalate_only`
and `bounded_stop` must list `rules` first: the rule sets the baseline or the minimum work before
any model is asked.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import BaseModel, Field, model_validator

from analystos.decisions.types import AUTHORITY_CLASSES, BACKENDS
from analystos.llm.config import load_models_config

RULES_FIRST = ("escalate_only", "bounded_stop")


class PurposeSpec(BaseModel):
    name: str
    kind: str
    authority: str
    backends: list[str]
    timeout_seconds: float = 3.0
    breaker_failures: int = 3
    breaker_reset_seconds: float = 30.0
    default: Any = None
    min_rounds: int = 1
    min_supported: int = 0
    stop_at: float = 0.85

    @model_validator(mode="after")
    def _check(self) -> PurposeSpec:
        if self.kind not in ("choice", "probability", "scores"):
            raise ValueError(f"decision purpose {self.name}: unknown kind {self.kind}")
        if self.authority not in AUTHORITY_CLASSES:
            raise ValueError(f"decision purpose {self.name}: unknown authority class {self.authority}")
        unknown = [b for b in self.backends if b not in BACKENDS]
        if unknown:
            raise ValueError(f"decision purpose {self.name}: unknown backends {unknown}")
        if "rules" not in self.backends:
            raise ValueError(f"decision purpose {self.name}: `rules` must be one of its backends (deterministic fallback)")
        if self.authority in RULES_FIRST and self.backends[0] != "rules":
            raise ValueError(f"decision purpose {self.name}: {self.authority} needs `rules` first")
        return self

    def ordered(self, override: list[str] | None = None) -> list[str]:
        """Backend order after an admin override: only known backends, `rules` always present (last
        resort, or first for rules-first classes)."""
        order = [b for b in dict.fromkeys(override or self.backends) if b in BACKENDS]
        if self.authority in RULES_FIRST:
            order = ["rules", *[b for b in order if b != "rules"]]
        elif "rules" not in order:
            order.append("rules")
        return order


class DecisionsConfig(BaseModel):
    purposes: dict[str, PurposeSpec] = Field(default_factory=dict)

    def get(self, purpose: str) -> PurposeSpec:
        spec = self.purposes.get(purpose)
        if spec is None:
            from analystos.core.errors import InvalidInput

            raise InvalidInput(f"unknown decision purpose '{purpose}'")
        return spec


def parse(section: dict[str, Any]) -> DecisionsConfig:
    defaults = dict(section.get("defaults") or {})
    return DecisionsConfig(purposes={name: PurposeSpec(name=name, **{**defaults, **(body or {})})
                                     for name, body in (section.get("purposes") or {}).items()})


@lru_cache
def load() -> DecisionsConfig:
    return parse(load_models_config().decisions)
