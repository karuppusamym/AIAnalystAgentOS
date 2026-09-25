"""Model router (MOD-001..004, GOV-003): purpose -> profile -> allowed models, with fallback,
bounded retry, budget checks, redaction and a persisted record of every call.

Two call shapes:
  complete()  chat models via OpenRouter chat completions (text or JSON output)
  decide()    TypeSafe Jev via the OpenRouter Decisions API (typed choice/noul/score answers)

The router never silently routes around policy: if the workspace allowlist, provider list or
data-residency rule removes every model of a profile, the call fails with ModelRouteUnavailable,
and a call whose pre-call cost estimate exceeds the workspace approval threshold raises
ApprovalRequired. Either way the caller must degrade visibly.

Every call record carries the redacted request and the response (P4-C09) so a run can be
reconstructed and re-executed offline with `analystos.llm.replay.ReplayTransport`.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from analystos.contracts.platform import MODEL_RUNGS
from analystos.core.errors import (
    ApprovalRequired,
    BudgetExceeded,
    LLMDisabled,
    ModelRouteUnavailable,
    ProviderQuotaExhausted,
    UpstreamUnavailable,
)
from analystos.core.ids import stable_hash
from analystos.core.logging import get_logger
from analystos.llm.cache import ResponseCache, estimate_tokens
from analystos.llm.config import ModelsConfig, ProfileConfig, family, load_models_config
from analystos.llm.redaction import redact, redact_obj

log = get_logger(__name__)


JSON_INSTRUCTION = "\n\nRespond with a single valid JSON value only."


@dataclass
class CallContext:
    """Who is calling and under which workspace policy. The policy travels with the call so the
    router can enforce it without reading the database; fields left at None mean "no workspace
    restriction" (platform-level calls such as admin checks)."""

    workspace_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    prompt_version: str | None = None
    allowed_models: list[str] = field(default_factory=list)  # workspace policy narrowing ([] = platform list)
    exclude_families: list[str] = field(default_factory=list)  # e.g. primary family, for verification
    allowed_providers: list[str] | None = None  # workspace policy; None = unrestricted, [] = none allowed
    data_residency: str | None = None  # required provider/model region; unknown region fails closed
    expensive_model_approval_usd: float | None = None  # pre-call estimate above this needs an approval

    @classmethod
    def for_policy(cls, policy: Any, **kwargs: Any) -> CallContext:
        """Context carrying every model-relevant field of a WorkspacePolicyDoc."""
        return cls(**kwargs).with_policy(policy)

    def with_policy(self, policy: Any) -> CallContext:
        if policy is None:
            return self
        self.allowed_models = list(getattr(policy, "allowed_models", None) or self.allowed_models)
        self.allowed_providers = list(policy.allowed_providers)
        self.data_residency = policy.data_residency or None
        self.expensive_model_approval_usd = policy.expensive_model_approval_usd
        return self


@dataclass
class ModelResponse:
    text: str
    data: Any
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    attempts: int = 1
    cached: bool = False


@dataclass
class DecisionResponse:
    answers: dict[str, Any]
    model: str
    cost_usd: float = 0.0
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached: bool = False


class UsageSink(Protocol):
    def record(self, *, ctx: CallContext, purpose: str, profile: str, provider: str, model: str, status: str,
               attempt: int, latency_ms: int, input_tokens: int, output_tokens: int, cost_usd: float,
               request_hash: str | None, error: str | None, tokens_saved: int = 0,
               request: dict | None = None, response: dict | None = None, answered_by: str | None = None,
               cost_source: str | None = None) -> None:
        """`request`/`response` are the redacted replay payloads (see analystos.llm.replay);
        `answered_by` is the ladder rung, `cost_source` provider | price_table@<v> | missing_price | none."""

    def check_budget(self, ctx: CallContext, purpose: str) -> None:
        """Raise BudgetExceeded when the run/workspace budget is spent."""

    def remaining_fraction(self, ctx: CallContext) -> float:
        """Share of the run's budget still available (1.0 when unknown)."""


class NullSink:
    def record(self, **_: Any) -> None:
        return None

    def check_budget(self, ctx: CallContext, purpose: str) -> None:
        return None

    def remaining_fraction(self, ctx: CallContext) -> float:
        return 1.0


class Transport(Protocol):
    def chat(self, *, base_url: str, api_key: str, payload: dict, timeout: float) -> dict: ...
    def decide(self, *, base_url: str, api_key: str, payload: dict, timeout: float) -> dict: ...


class HttpTransport:
    def __init__(self) -> None:
        self._client = httpx.Client(headers={"HTTP-Referer": "https://github.com/karuppusamym/AIAnalystAgentOS",
                                             "X-Title": "Context2AI AnalystOS"})

    def _post(self, url: str, api_key: str, payload: dict, timeout: float) -> dict:
        try:
            response = self._client.post(url, json=payload, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise UpstreamUnavailable(f"model provider timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"model provider unreachable: {exc}") from exc
        if response.status_code in (408, 409, 425, 429) or response.status_code >= 500:
            raise UpstreamUnavailable(f"model provider HTTP {response.status_code}: {response.text[:300]}")
        if response.status_code == 402:
            raise ProviderQuotaExhausted(f"model provider refused for credits HTTP 402: {response.text[:300]}")
        if response.status_code >= 400:
            raise ModelRouteUnavailable(f"model provider rejected request HTTP {response.status_code}: {response.text[:300]}")
        return response.json()

    def chat(self, *, base_url: str, api_key: str, payload: dict, timeout: float) -> dict:
        return self._post(f"{base_url.rstrip('/')}/chat/completions", api_key, payload, timeout)

    def decide(self, *, base_url: str, api_key: str, payload: dict, timeout: float) -> dict:
        return self._post(base_url.rstrip("/"), api_key, payload, timeout)


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


PROVIDER_COOLDOWN_SECONDS = 60
_PROVIDER_COOLDOWN: dict[str, float] = {}  # provider -> monotonic time the cooldown ends (process-wide)


def _cooling_down(provider: str) -> bool:
    until = _PROVIDER_COOLDOWN.get(provider)
    if until is None:
        return False
    if time.monotonic() >= until:
        _PROVIDER_COOLDOWN.pop(provider, None)
        return False
    return True


def parse_json_text(text: str) -> Any:
    text = text.strip()
    match = _JSON_BLOCK.search(text)
    if match:
        text = match.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
        if start < 0:
            raise
        end = max(text.rfind("}"), text.rfind("]"))
        return json.loads(text[start:end + 1])


def ladder_for_mode(default: list[str], mode: str) -> list[str]:
    """Apply an increment-3 mode to a ladder: off drops the model rungs (rules answer), auto puts the
    deterministic rungs first, always puts the model rungs first (rules stay the fallback)."""
    models = [r for r in default if r in MODEL_RUNGS]
    other = [r for r in default if r not in MODEL_RUNGS]
    if "rules" not in other:
        other.append("rules")  # every caller has a deterministic fallback, even if it is "degrade visibly"
    if mode == "off":
        return other
    cache = [r for r in other if r == "cache"]
    rest = [r for r in other if r != "cache"]
    return cache + (rest + models if mode == "auto" else models + rest)


def mode_of(ladder: list[str]) -> str:
    """The increment-3 mode a ladder amounts to (what `model_gate` and JEV callers branch on)."""
    model_at = next((i for i, r in enumerate(ladder) if r in MODEL_RUNGS), None)
    if model_at is None:
        return "off"
    det_at = next((i for i, r in enumerate(ladder) if r in ("registry", "rules")), None)
    return "auto" if det_at is not None and det_at < model_at else "always"


def _price_error(cost_source: str, model: str, config: ModelsConfig) -> str | None:
    if cost_source != "missing_price":
        return None
    return f"missing price: {model} has no entry in price table {config.prices_version} and the provider reported no cost"


class ModelRouter:
    def __init__(self, config: ModelsConfig | None = None, *, sink: UsageSink | None = None,
                 transport: Transport | None = None, api_key_lookup: Callable[[str], str | None] | None = None,
                 max_retries: int = 2, settings_provider: Callable[[], Any] | None = None,
                 cache: ResponseCache | None = None) -> None:
        self.config = config or load_models_config()
        self.sink = sink or NullSink()
        self.transport = transport or HttpTransport()
        self.api_key_lookup = api_key_lookup or (lambda env: (os.getenv(env) or "").strip() or None)
        self.max_retries = max_retries
        self.settings_provider = settings_provider or _default_settings
        self.cache = cache if cache is not None else ResponseCache(None)

    # ------------------------------------------------------------------ admin settings
    @property
    def settings(self):
        return self.settings_provider().llm

    def ladder(self, purpose: str) -> list[str]:
        """The purpose's execution ladder (spec v3 §4.1): admin `ladders` override, else the admin
        mode applied to the models.yaml default, else the default. JEV purposes lose their decision
        rung when the jev feature flag is off."""
        platform = self.settings_provider()
        llm = platform.llm
        default = self.config.default_ladder(purpose)
        if purpose in llm.ladders:
            rungs = list(llm.ladders[purpose])
        elif purpose in llm.purpose_modes:
            rungs = ladder_for_mode(default, llm.purpose_modes[purpose])
        else:
            rungs = default
        if purpose in self.config.routing and self.config.routing[purpose] == "decision" and not platform.features.jev_decisions:
            rungs = ladder_for_mode(rungs, "off")
        return rungs

    def mode(self, purpose: str) -> str:
        """off | auto | always, derived from the ladder (ADR-0012: the increment-3 modes are ladder presets)."""
        return mode_of(self.ladder(purpose))

    def record_skip(self, purpose: str, ctx: CallContext | None, *, estimated_tokens: int, reason: str,
                    rung: str = "rules") -> None:
        """Account for a model call avoided by a deterministic rung (shown as tokens saved)."""
        ctx = ctx or CallContext()
        self.sink.record(ctx=ctx, purpose=purpose, profile="-", provider="deterministic", model="-", status="skipped",
                         attempt=0, latency_ms=0, input_tokens=0, output_tokens=0, cost_usd=0.0, request_hash=None,
                         error=reason[:200], tokens_saved=estimated_tokens, answered_by=rung, cost_source="none")

    def _cost(self, model: str, usage: dict[str, Any], input_tokens: int, output_tokens: int) -> tuple[float, str]:
        """Provider-reported cost first, else the versioned price table; a model with neither is
        recorded as `missing_price` (surfaced in token savings), never silently as a known $0."""
        reported = usage.get("cost")
        if reported is not None:
            return float(reported), "provider"
        priced = self.config.estimate_cost(model, input_tokens, output_tokens)
        if priced is not None:
            return priced, f"price_table@{self.config.prices_version}"
        log.error("no price for model %s in price table %s and the provider reported no cost; cost recorded as missing_price",
                  model, self.config.prices_version)
        return 0.0, "missing_price"

    # ------------------------------------------------------------------ routing
    def candidates(self, purpose: str, ctx: CallContext) -> tuple[str, ProfileConfig, list[str]]:
        llm = self.settings
        override = llm.routing_overrides.get(purpose)
        if override and override in self.config.profiles:
            profile_name, profile = override, self.config.profiles[override]
        else:
            profile_name, profile = self.config.profile_for(purpose)
        return self._resolve(profile_name, profile, ctx)

    def _resolve(self, profile_name: str, profile: ProfileConfig, ctx: CallContext) -> tuple[str, ProfileConfig, list[str]]:
        """Admin profile models, then platform allowlist - disabled models, workspace allowlist, family exclusions."""
        llm = self.settings
        if llm.profile_models.get(profile_name):
            profile = profile.model_copy(update={"models": list(llm.profile_models[profile_name])})
        allowed = set(self.config.allowlist) - set(llm.disabled_models)
        if ctx.allowed_models:
            allowed &= set(ctx.allowed_models)
        excluded = set(profile.exclude_families) | set(ctx.exclude_families)
        models = [m for m in profile.models if m in allowed and family(m) not in excluded]
        if self._policy_block(profile, ctx):
            models = []
        elif ctx.data_residency:
            want = ctx.data_residency.strip().lower()
            models = [m for m in models if (self.config.region_of(m, profile.provider) or "").strip().lower() == want]
        return profile_name, profile, models

    @staticmethod
    def _policy_block(profile: ProfileConfig, ctx: CallContext) -> str | None:
        if ctx.allowed_providers is not None and profile.provider not in ctx.allowed_providers:
            return f"provider '{profile.provider}' is not in the workspace allowed_providers {ctx.allowed_providers}"
        return None

    def _no_route(self, purpose: str, profile_name: str, profile: ProfileConfig, ctx: CallContext) -> ModelRouteUnavailable:
        reason = self._policy_block(profile, ctx)
        if reason is None and ctx.data_residency:
            reason = (f"no model of profile {profile_name} has a known region matching the workspace data_residency "
                      f"'{ctx.data_residency}' (unknown regions fail closed)")
        return ModelRouteUnavailable(f"no allowed model for purpose '{purpose}' (profile {profile_name})"
                                     + (f": {reason}" if reason else ""))

    def _within_cost(self, purpose: str, profile_name: str, profile: ProfileConfig, models: list[str], ctx: CallContext,
                     *, input_tokens: int, output_tokens: int, request_hash: str, request: dict) -> list[str]:
        """Drop models whose pre-call estimate exceeds the workspace approval threshold. When none
        remain the call needs an approval: raise ApprovalRequired instead of calling silently.
        Models without price metadata cannot be estimated and are not held back."""
        limit = ctx.expensive_model_approval_usd
        if limit is None:
            return models
        estimates = {m: self.config.estimate_cost(m, input_tokens, output_tokens) for m in models}
        within = [m for m in models if estimates[m] is None or estimates[m] <= limit]
        if within:
            return within
        cheapest = min(models, key=lambda m: estimates[m] or 0.0)
        message = (f"'{purpose}' is estimated at ${estimates[cheapest]:.4f} on {cheapest}, above the workspace "
                   f"approval threshold ${limit:.4f} (expensive_model_approval_usd)")
        self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=cheapest,
                         status="approval_required", attempt=0, latency_ms=0, input_tokens=0, output_tokens=0, cost_usd=0.0,
                         request_hash=request_hash, error=message[:500], request=request, answered_by="rules",
                         cost_source="none")
        raise ApprovalRequired(message, details={"purpose": purpose, "model": cheapest, "estimated_usd": estimates[cheapest],
                                                 "limit_usd": limit, "decision": "approval_required"})

    def available(self, purpose: str, ctx: CallContext | None = None) -> bool:
        ctx = ctx or CallContext()
        if self.mode(purpose) == "off":
            return False
        try:
            _, profile, models = self.candidates(purpose, ctx)
        except KeyError:
            return False
        provider = self.config.providers.get(profile.provider)
        return bool(models and provider and self.api_key_lookup(provider.api_key_env)) and not _cooling_down(profile.provider)

    def _provider(self, profile: ProfileConfig) -> tuple[str, str]:
        provider = self.config.providers.get(profile.provider)
        if provider is None:
            raise ModelRouteUnavailable(f"provider '{profile.provider}' is not configured")
        key = self.api_key_lookup(provider.api_key_env)
        if not key:
            raise ModelRouteUnavailable(f"no API key for provider '{profile.provider}' (set {provider.api_key_env})")
        return provider.base_url, key

    # ------------------------------------------------------------------ chat
    def complete(self, purpose: str, messages: list[dict[str, str]], *, ctx: CallContext | None = None,
                 json_output: bool = False, max_tokens: int | None = None) -> ModelResponse:
        ctx = ctx or CallContext()
        if self.mode(purpose) == "off":
            raise LLMDisabled(f"model use for '{purpose}' is turned off by the administrator")
        profile_name, profile, models = self.candidates(purpose, ctx)
        llm = self.settings
        if profile.provider != "typesafe" and profile_name != "low_cost" and not profile.exclude_families \
                and "low_cost" in self.config.profiles \
                and getattr(self.sink, "remaining_fraction", lambda _c: 1.0)(ctx) < llm.downgrade_below_budget_fraction:
            # Budget nearly spent: finish the run on the cheapest profile instead of failing it. Same
            # allowlists and exclusions as any call; never for independent-family verification.
            low_name, low, downgraded = self._resolve("low_cost", self.config.profiles["low_cost"], ctx)
            if downgraded:
                profile_name, profile, models = low_name, low, downgraded
        if not models:
            raise self._no_route(purpose, profile_name, profile, ctx)
        rung = self.config.model_rung(profile_name)
        base_url, key = self._provider(profile)
        self.sink.check_budget(ctx, purpose)
        safe_messages = [{"role": m["role"], "content": redact(m["content"])} for m in messages]
        if json_output:
            safe_messages[0] = {"role": safe_messages[0]["role"], "content": safe_messages[0]["content"] + JSON_INSTRUCTION}
        request_hash = stable_hash({"purpose": purpose, "messages": safe_messages})
        request = {"kind": "chat", "purpose": purpose, "messages": safe_messages, "json_output": json_output,
                   "max_tokens": max_tokens, "temperature": profile.temperature}
        estimate = estimate_tokens("".join(m["content"] for m in safe_messages))
        if estimate > llm.max_prompt_tokens:
            self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model="-",
                             status="refused", attempt=0, latency_ms=0, input_tokens=0, output_tokens=0, cost_usd=0.0,
                             request_hash=request_hash, error=f"prompt ~{estimate} tokens > limit {llm.max_prompt_tokens}",
                             tokens_saved=estimate, request=request, answered_by="rules", cost_source="none")
            raise LLMDisabled(f"prompt for '{purpose}' is ~{estimate} tokens, above the admin limit {llm.max_prompt_tokens}")
        models = self._within_cost(purpose, profile_name, profile, models, ctx, input_tokens=estimate,
                                   output_tokens=max_tokens or profile.max_tokens, request_hash=request_hash, request=request)
        cache_key = None
        if llm.cache_enabled and purpose in llm.cacheable_purposes:
            cache_key = ResponseCache.key(purpose, models, {"m": safe_messages, "json": json_output, "max": max_tokens,
                                                            "t": profile.temperature}, ctx.workspace_id)
            hit = self.cache.get(cache_key)
            if hit:
                self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=hit["model"],
                                 status="cache_hit", attempt=0, latency_ms=0, input_tokens=0, output_tokens=0, cost_usd=0.0,
                                 request_hash=request_hash, error=None,
                                 tokens_saved=int(hit.get("input_tokens", 0)) + int(hit.get("output_tokens", 0)),
                                 request=request, response={"model": hit["model"], "text": redact(hit["text"]), "cached": True},
                                 answered_by="cache", cost_source="none")
                return ModelResponse(text=hit["text"], data=hit.get("data"), model=hit["model"], provider=profile.provider,
                                     cached=True)
        if _cooling_down(profile.provider):
            raise ProviderQuotaExhausted(f"provider '{profile.provider}' refused for credits recently; "
                                         f"deterministic path until the cooldown ends")
        last_error: Exception | None = None
        attempt = 0
        for model in models:
            for retry in range(self.max_retries + 1):
                attempt += 1
                payload: dict[str, Any] = {"model": model, "messages": safe_messages, "temperature": profile.temperature,
                                           "max_tokens": max_tokens or profile.max_tokens, "usage": {"include": True}}
                if json_output:
                    payload["response_format"] = {"type": "json_object"}
                started = time.perf_counter()
                try:
                    body = self.transport.chat(base_url=base_url, api_key=key, payload=payload, timeout=profile.timeout_seconds)
                    text = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                    usage = body.get("usage") or {}
                    latency = round((time.perf_counter() - started) * 1000)
                    data = None
                    if json_output:
                        data = parse_json_text(text)  # JSONDecodeError -> next attempt
                    answered_model = str(body.get("model") or model)
                    in_tok, out_tok = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
                    cost, cost_source = self._cost(answered_model if answered_model in self.config.models else model,
                                                   usage, in_tok, out_tok)
                    response = ModelResponse(text=text, data=data, model=answered_model, provider=profile.provider,
                                             input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost, latency_ms=latency,
                                             attempts=attempt)
                    self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=response.model,
                                     status="ok", attempt=attempt, latency_ms=latency, input_tokens=response.input_tokens,
                                     output_tokens=response.output_tokens, cost_usd=response.cost_usd,
                                     request_hash=request_hash, error=_price_error(cost_source, response.model, self.config),
                                     request=request, response={"model": response.model, "text": redact(text), "usage": usage},
                                     answered_by=rung, cost_source=cost_source)
                    if cache_key:
                        self.cache.set(cache_key, {"text": text, "data": data, "model": response.model,
                                                   "input_tokens": response.input_tokens, "output_tokens": response.output_tokens},
                                       llm.cache_ttl_hours * 3600)
                    return response
                except (UpstreamUnavailable, json.JSONDecodeError, ValueError) as exc:
                    last_error = exc
                    self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=model,
                                     status="error", attempt=attempt, latency_ms=round((time.perf_counter() - started) * 1000),
                                     input_tokens=0, output_tokens=0, cost_usd=0.0, request_hash=request_hash, error=str(exc)[:500],
                                     request=request, answered_by=rung, cost_source="none")
                    log.warning("model call failed purpose=%s model=%s attempt=%s: %s", purpose, model, attempt, exc)
                    if retry < self.max_retries:
                        time.sleep(min(0.5 * 2**retry, 4))
                except ProviderQuotaExhausted as exc:  # 402: no model behind this provider will work either
                    self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=model,
                                     status="error", attempt=attempt, latency_ms=round((time.perf_counter() - started) * 1000),
                                     input_tokens=0, output_tokens=0, cost_usd=0.0, request_hash=request_hash, error=str(exc)[:500],
                                     request=request, answered_by=rung, cost_source="none")
                    _PROVIDER_COOLDOWN[profile.provider] = time.monotonic() + PROVIDER_COOLDOWN_SECONDS
                    log.warning("provider %s refused for credits; cooling down %ss", profile.provider, PROVIDER_COOLDOWN_SECONDS)
                    raise
                except ModelRouteUnavailable as exc:  # 4xx: this model will not work; try next model
                    last_error = exc
                    self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=model,
                                     status="error", attempt=attempt, latency_ms=round((time.perf_counter() - started) * 1000),
                                     input_tokens=0, output_tokens=0, cost_usd=0.0, request_hash=request_hash, error=str(exc)[:500],
                                     request=request, answered_by=rung, cost_source="none")
                    break
        raise ModelRouteUnavailable(f"all models failed for purpose '{purpose}': {last_error}")

    def complete_json(self, purpose: str, system: str, user: str, *, ctx: CallContext | None = None,
                      max_tokens: int | None = None) -> ModelResponse:
        return self.complete(purpose, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                             ctx=ctx, json_output=True, max_tokens=max_tokens)

    # ------------------------------------------------------------------ decisions (JEV)
    def decide(self, purpose: str, state: dict[str, Any], questions: dict[str, Any], *,
               ctx: CallContext | None = None) -> DecisionResponse:
        """Typed decision via TypeSafe Jev. Only trusted, redacted text goes into `state`:
        objective, registry/catalog descriptions, computed statistics — never raw result rows."""
        ctx = ctx or CallContext()
        if self.mode(purpose) == "off":
            raise LLMDisabled(f"decision model for '{purpose}' is turned off by the administrator")
        profile_name, profile, models = self.candidates(purpose, ctx)
        if not models:
            raise self._no_route(purpose, profile_name, profile, ctx)
        base_url, key = self._provider(profile)
        self.sink.check_budget(ctx, purpose)
        safe_state = redact_obj({k: (v[:4000] if isinstance(v, str) else v) for k, v in state.items()})
        request_hash = stable_hash({"purpose": purpose, "state": safe_state, "questions": questions})
        request = {"kind": "decision", "purpose": purpose, "state": safe_state, "questions": questions}
        models = self._within_cost(purpose, profile_name, profile, models, ctx,
                                   input_tokens=estimate_tokens(json.dumps(request, default=str)),
                                   output_tokens=profile.max_tokens, request_hash=request_hash, request=request)
        llm = self.settings
        cache_key = None
        if llm.cache_enabled and purpose in llm.cacheable_purposes:
            cache_key = ResponseCache.key(purpose, models, {"s": safe_state, "q": questions}, ctx.workspace_id)
            hit = self.cache.get(cache_key)
            if hit:
                self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=hit["model"],
                                 status="cache_hit", attempt=0, latency_ms=0, input_tokens=0, output_tokens=0, cost_usd=0.0,
                                 request_hash=request_hash, error=None,
                                 tokens_saved=int(hit.get("input_tokens", 0)) + int(hit.get("output_tokens", 0)),
                                 request=request, response={"model": hit["model"], "answers": redact_obj(hit["answers"]),
                                                            "cached": True}, answered_by="cache", cost_source="none")
                return DecisionResponse(answers=hit["answers"], model=hit["model"], cached=True)
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 2):
            started = time.perf_counter()
            try:
                body = self.transport.decide(base_url=base_url, api_key=key,
                                             payload={"model": models[0], "state": safe_state, "questions": questions},
                                             timeout=profile.timeout_seconds)
                answers = body.get("answers")
                if not isinstance(answers, dict):
                    raise UpstreamUnavailable("decision response had no answers")
                usage = body.get("usage") or {}
                latency = round((time.perf_counter() - started) * 1000)
                in_tok, out_tok = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
                answered_model = str(body.get("model") or models[0])
                cost, cost_source = self._cost(answered_model if answered_model in self.config.models else models[0],
                                               usage, in_tok, out_tok)
                result = DecisionResponse(answers=answers, model=answered_model, cost_usd=cost, latency_ms=latency,
                                          input_tokens=in_tok, output_tokens=out_tok)
                self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=result.model,
                                 status="ok", attempt=attempt, latency_ms=latency, input_tokens=result.input_tokens,
                                 output_tokens=result.output_tokens, cost_usd=result.cost_usd, request_hash=request_hash,
                                 error=_price_error(cost_source, result.model, self.config),
                                 request=request, response={"model": result.model, "answers": redact_obj(answers), "usage": usage},
                                 answered_by="decision", cost_source=cost_source)
                if cache_key:
                    self.cache.set(cache_key, {"answers": answers, "model": result.model, "input_tokens": result.input_tokens,
                                               "output_tokens": result.output_tokens}, llm.cache_ttl_hours * 3600)
                return result
            except (UpstreamUnavailable, ModelRouteUnavailable) as exc:
                last_error = exc
                self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=models[0],
                                 status="error", attempt=attempt, latency_ms=round((time.perf_counter() - started) * 1000),
                                 input_tokens=0, output_tokens=0, cost_usd=0.0, request_hash=request_hash, error=str(exc)[:500],
                                 request=request, answered_by="decision", cost_source="none")
                if isinstance(exc, ModelRouteUnavailable):
                    break
                time.sleep(min(0.5 * 2 ** (attempt - 1), 4))
        raise ModelRouteUnavailable(f"decision model failed for purpose '{purpose}': {last_error}")


def _default_settings():
    try:
        from analystos.services.platform_settings import get

        return get()
    except Exception:  # pragma: no cover - settings must never break model routing
        from analystos.contracts.platform import PlatformSettings

        return PlatformSettings()


__all__ = ["MODEL_RUNGS", "JSON_INSTRUCTION", "ladder_for_mode", "mode_of", "ApprovalRequired", "BudgetExceeded", "CallContext", "DecisionResponse", "ModelResponse", "ModelRouter", "parse_json_text"]
