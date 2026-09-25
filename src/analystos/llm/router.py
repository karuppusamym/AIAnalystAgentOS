"""Model router (MOD-001..004, GOV-003): purpose -> profile -> allowed models, with fallback,
bounded retry, budget checks, redaction and a persisted record of every call.

Two call shapes:
  complete()  chat models via OpenRouter chat completions (text or JSON output)
  decide()    TypeSafe Jev via the OpenRouter Decisions API (typed choice/noul/score answers)

The router never silently routes around policy: if the workspace allowlist removes every model
of a profile, the call fails with ModelRouteUnavailable and the caller must degrade visibly.
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

from analystos.core.errors import BudgetExceeded, ModelRouteUnavailable, UpstreamUnavailable
from analystos.core.ids import stable_hash
from analystos.core.logging import get_logger
from analystos.llm.config import ModelsConfig, ProfileConfig, family, load_models_config
from analystos.llm.redaction import redact, redact_obj

log = get_logger(__name__)


@dataclass
class CallContext:
    workspace_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    prompt_version: str | None = None
    allowed_models: list[str] = field(default_factory=list)  # workspace policy narrowing ([] = platform list)
    exclude_families: list[str] = field(default_factory=list)  # e.g. primary family, for verification


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


@dataclass
class DecisionResponse:
    answers: dict[str, Any]
    model: str
    cost_usd: float = 0.0
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class UsageSink(Protocol):
    def record(self, *, ctx: CallContext, purpose: str, profile: str, provider: str, model: str, status: str,
               attempt: int, latency_ms: int, input_tokens: int, output_tokens: int, cost_usd: float,
               request_hash: str | None, error: str | None) -> None: ...

    def check_budget(self, ctx: CallContext, purpose: str) -> None:
        """Raise BudgetExceeded when the run/workspace budget is spent."""


class NullSink:
    def record(self, **_: Any) -> None:
        return None

    def check_budget(self, ctx: CallContext, purpose: str) -> None:
        return None


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
        if response.status_code >= 400:
            raise ModelRouteUnavailable(f"model provider rejected request HTTP {response.status_code}: {response.text[:300]}")
        return response.json()

    def chat(self, *, base_url: str, api_key: str, payload: dict, timeout: float) -> dict:
        return self._post(f"{base_url.rstrip('/')}/chat/completions", api_key, payload, timeout)

    def decide(self, *, base_url: str, api_key: str, payload: dict, timeout: float) -> dict:
        return self._post(base_url.rstrip("/"), api_key, payload, timeout)


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


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


class ModelRouter:
    def __init__(self, config: ModelsConfig | None = None, *, sink: UsageSink | None = None,
                 transport: Transport | None = None, api_key_lookup: Callable[[str], str | None] | None = None,
                 max_retries: int = 2) -> None:
        self.config = config or load_models_config()
        self.sink = sink or NullSink()
        self.transport = transport or HttpTransport()
        self.api_key_lookup = api_key_lookup or (lambda env: (os.getenv(env) or "").strip() or None)
        self.max_retries = max_retries

    # ------------------------------------------------------------------ routing
    def candidates(self, purpose: str, ctx: CallContext) -> tuple[str, ProfileConfig, list[str]]:
        profile_name, profile = self.config.profile_for(purpose)
        allowed = set(self.config.allowlist)
        if ctx.allowed_models:
            allowed &= set(ctx.allowed_models)
        excluded = set(profile.exclude_families) | set(ctx.exclude_families)
        models = [m for m in profile.models if m in allowed and family(m) not in excluded]
        return profile_name, profile, models

    def available(self, purpose: str, ctx: CallContext | None = None) -> bool:
        ctx = ctx or CallContext()
        try:
            _, profile, models = self.candidates(purpose, ctx)
        except KeyError:
            return False
        provider = self.config.providers.get(profile.provider)
        return bool(models and provider and self.api_key_lookup(provider.api_key_env))

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
        profile_name, profile, models = self.candidates(purpose, ctx)
        if not models:
            raise ModelRouteUnavailable(f"no allowed model for purpose '{purpose}' (profile {profile_name})")
        base_url, key = self._provider(profile)
        self.sink.check_budget(ctx, purpose)
        safe_messages = [{"role": m["role"], "content": redact(m["content"])} for m in messages]
        if json_output:
            safe_messages[0] = {"role": safe_messages[0]["role"],
                                "content": safe_messages[0]["content"] + "\n\nRespond with a single valid JSON value only."}
        request_hash = stable_hash({"purpose": purpose, "messages": safe_messages})
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
                    response = ModelResponse(text=text, data=data, model=str(body.get("model") or model), provider=profile.provider,
                                             input_tokens=int(usage.get("prompt_tokens") or 0),
                                             output_tokens=int(usage.get("completion_tokens") or 0),
                                             cost_usd=float(usage.get("cost") or 0.0), latency_ms=latency, attempts=attempt)
                    self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=response.model,
                                     status="ok", attempt=attempt, latency_ms=latency, input_tokens=response.input_tokens,
                                     output_tokens=response.output_tokens, cost_usd=response.cost_usd,
                                     request_hash=request_hash, error=None)
                    return response
                except (UpstreamUnavailable, json.JSONDecodeError, ValueError) as exc:
                    last_error = exc
                    self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=model,
                                     status="error", attempt=attempt, latency_ms=round((time.perf_counter() - started) * 1000),
                                     input_tokens=0, output_tokens=0, cost_usd=0.0, request_hash=request_hash, error=str(exc)[:500])
                    log.warning("model call failed purpose=%s model=%s attempt=%s: %s", purpose, model, attempt, exc)
                    if retry < self.max_retries:
                        time.sleep(min(0.5 * 2**retry, 4))
                except ModelRouteUnavailable as exc:  # 4xx: this model will not work; try next model
                    last_error = exc
                    self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=model,
                                     status="error", attempt=attempt, latency_ms=round((time.perf_counter() - started) * 1000),
                                     input_tokens=0, output_tokens=0, cost_usd=0.0, request_hash=request_hash, error=str(exc)[:500])
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
        profile_name, profile, models = self.candidates(purpose, ctx)
        if not models:
            raise ModelRouteUnavailable(f"no allowed decision model for purpose '{purpose}'")
        base_url, key = self._provider(profile)
        self.sink.check_budget(ctx, purpose)
        safe_state = redact_obj({k: (v[:4000] if isinstance(v, str) else v) for k, v in state.items()})
        request_hash = stable_hash({"purpose": purpose, "state": safe_state, "questions": questions})
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
                result = DecisionResponse(answers=answers, model=str(body.get("model") or models[0]),
                                          cost_usd=float(usage.get("cost") or 0.0), latency_ms=latency,
                                          input_tokens=int(usage.get("input_tokens") or 0),
                                          output_tokens=int(usage.get("output_tokens") or 0))
                self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=result.model,
                                 status="ok", attempt=attempt, latency_ms=latency, input_tokens=result.input_tokens,
                                 output_tokens=result.output_tokens, cost_usd=result.cost_usd, request_hash=request_hash, error=None)
                return result
            except (UpstreamUnavailable, ModelRouteUnavailable) as exc:
                last_error = exc
                self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=models[0],
                                 status="error", attempt=attempt, latency_ms=round((time.perf_counter() - started) * 1000),
                                 input_tokens=0, output_tokens=0, cost_usd=0.0, request_hash=request_hash, error=str(exc)[:500])
                if isinstance(exc, ModelRouteUnavailable):
                    break
                time.sleep(min(0.5 * 2 ** (attempt - 1), 4))
        raise ModelRouteUnavailable(f"decision model failed for purpose '{purpose}': {last_error}")


__all__ = ["BudgetExceeded", "CallContext", "DecisionResponse", "ModelResponse", "ModelRouter", "parse_json_text"]
