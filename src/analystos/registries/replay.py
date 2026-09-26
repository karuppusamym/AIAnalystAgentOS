"""Scheduled re-analysis as a registry replay (ladder rung L1, review C11).

A scheduled re-analysis run (origin `{type: schedule, replay: true}`) sees the model router through
`ReplayRouter`: every purpose is `off`, so each step takes its rule/template path and the avoided
call is recorded as a saving labelled "registry replay". The only exception is the opt-in novelty
round (`novelty: {enabled, max_hypotheses, llm_budget_usd}` on the schedule), which may ask for new
hypotheses within its own budget; its findings are reported as new questions, not new findings."""
from __future__ import annotations

import dataclasses
from typing import Any

from analystos.core.errors import InvalidInput, LLMDisabled

NOVELTY_PURPOSES = frozenset({"hypothesis_generation"})
NOVELTY_DEFAULTS: dict[str, Any] = {"enabled": False, "max_hypotheses": 3, "llm_budget_usd": 0.05}
REGISTRY_SCOPES = ("previous_run", "workspace")


def novelty_config(raw: Any) -> dict[str, Any]:
    """Validated novelty settings (off by default)."""
    if raw is None:
        return dict(NOVELTY_DEFAULTS)
    if not isinstance(raw, dict):
        raise InvalidInput("config.novelty must be an object like {enabled, max_hypotheses, llm_budget_usd}")
    unknown = set(raw) - set(NOVELTY_DEFAULTS)
    if unknown:
        raise InvalidInput(f"unknown config.novelty keys: {sorted(unknown)}")
    out = {**NOVELTY_DEFAULTS, **raw}
    if not isinstance(out["enabled"], bool):
        raise InvalidInput("config.novelty.enabled must be true or false")
    if not isinstance(out["max_hypotheses"], int) or isinstance(out["max_hypotheses"], bool) or not 1 <= out["max_hypotheses"] <= 20:
        raise InvalidInput("config.novelty.max_hypotheses must be an integer from 1 to 20")
    budget = out["llm_budget_usd"]
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) or not 0 <= budget <= 10:
        raise InvalidInput("config.novelty.llm_budget_usd must be a number from 0 to 10")
    out["llm_budget_usd"] = float(budget)
    return out


def validate_schedule_config(config: dict[str, Any]) -> None:
    if not isinstance(config.get("replay", True), bool):
        raise InvalidInput("config.replay must be true or false")
    if config.get("registry_scope", "previous_run") not in REGISTRY_SCOPES:
        raise InvalidInput(f"config.registry_scope must be one of {list(REGISTRY_SCOPES)}")
    novelty_config(config.get("novelty"))


def frozen_analyses(run: Any) -> list[dict[str, Any]]:
    """The pinned AnalysisSpec set a run replays (ADR-0021): a schedule's baseline set or a work order's."""
    return list((((getattr(run, "capabilities", None) or {}).get("pinned") or {}).get("analyses")) or [])


def replay_settings(run: Any) -> dict[str, Any] | None:
    """The replay settings of a scheduled re-analysis run (or a run of a typed work order), else None.
    With a frozen set the run replays exactly those specs; otherwise the registry scope decides."""
    origin = getattr(run, "origin", None) or {}
    analyses = frozen_analyses(run)
    if origin.get("type") == "work_order" and analyses:
        return {"novelty": dict(NOVELTY_DEFAULTS), "registry_scope": "previous_run", "analyses": analyses, "label": "work_order"}
    if origin.get("type") != "schedule" or not origin.get("replay"):
        return None
    return {"novelty": novelty_config(origin.get("novelty")), "registry_scope": origin.get("registry_scope", "previous_run"),
            "analyses": analyses, "label": "registry"}


class ReplayRouter:
    """The run's model router with every purpose off except the ones the novelty round may use."""

    def __init__(self, inner: Any, allowed: frozenset[str] = frozenset()) -> None:
        self._inner = inner
        self._allowed = allowed

    def mode(self, purpose: str) -> str:
        if purpose in self._allowed:
            return "off" if self._inner.mode(purpose) == "off" else "always"  # opted in explicitly
        return "off"

    def available(self, purpose: str, ctx: Any = None) -> bool:
        return purpose in self._allowed and self._inner.available(purpose, ctx)

    def record_skip(self, purpose: str, ctx: Any, *, estimated_tokens: int, reason: str, rung: str = "rules") -> None:
        self._inner.record_skip(purpose, ctx, estimated_tokens=estimated_tokens, reason=f"registry replay: {reason}"[:200], rung=rung)

    def _guard(self, purpose: str) -> None:
        if purpose not in self._allowed:
            raise LLMDisabled(f"{purpose} is off during a scheduled registry replay")

    def complete(self, purpose: str, *args: Any, **kwargs: Any) -> Any:
        self._guard(purpose)
        return self._inner.complete(purpose, *args, **kwargs)

    def complete_json(self, purpose: str, *args: Any, **kwargs: Any) -> Any:
        self._guard(purpose)
        return self._inner.complete_json(purpose, *args, **kwargs)

    def decide(self, purpose: str, *args: Any, **kwargs: Any) -> Any:
        self._guard(purpose)
        return self._inner.decide(purpose, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def services_for(run: Any, services: Any) -> Any:
    """Wrap the step's services for a replay run; any other run gets them unchanged."""
    settings = replay_settings(run)
    if settings is None or isinstance(services.router, ReplayRouter):
        return services
    allowed = NOVELTY_PURPOSES if settings["novelty"]["enabled"] else frozenset()
    return dataclasses.replace(services, router=ReplayRouter(services.router, allowed))
