"""Model provider health for administrators (`GET /api/admin/models/health`).

Answers "why are no model calls happening?" without a live call: is the provider's key present in
THIS process's environment (never the key itself), when did a call last succeed, is the provider
cooling down (e.g. after HTTP 402, shared across processes through Redis), and how much of today's
platform spend cap is used. `probe=True` sends one tiny billable request (purpose `health_probe`,
through the router: allowlists, spend reservation and the call record all apply) to verify credits."""
from __future__ import annotations

import os
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analystos.core.errors import AnalystOSError
from analystos.db.models import ModelCall
from analystos.llm import router as router_module
from analystos.llm.config import load_models_config
from analystos.llm.router import CallContext, ModelRouter
from analystos.runtime.budget_counters import BudgetCounters, day_start, next_day_start
from analystos.runtime.usage import day_cost_sql

PROBE_PURPOSE = "health_probe"
LOOKBACK = timedelta(days=30)


def _missing_key_message(env: str) -> str:
    return f"No API key in this process — set {env} for the api and worker containers and restart them."


def _probe(router: ModelRouter, provider: str, counters: BudgetCounters) -> dict[str, Any]:
    cfg = router.config
    profile = cfg.profiles.get(cfg.routing.get(PROBE_PURPOSE, ""))
    if profile is None or profile.provider != provider:
        return {"probed": False, "detail": f"no probe route for provider {provider} (purpose {PROBE_PURPOSE} uses "
                                           f"{profile.provider if profile else 'nothing'})"}
    # An explicit probe is how an administrator checks that topped-up credits work: it goes out even
    # while the provider cools down, and a success ends the cooldown.
    router_module._PROVIDER_COOLDOWN.pop(provider, None)
    started = time.perf_counter()
    try:
        r = router.complete(PROBE_PURPOSE, [{"role": "user", "content": "Reply with the single word OK."}],
                            ctx=CallContext(agent_id="admin.health_probe"), max_tokens=5)
    except AnalystOSError as exc:
        return {"probed": True, "ok": False, "code": exc.code, "error": exc.message[:300],
                "latency_ms": round((time.perf_counter() - started) * 1000),
                **({"remedy": exc.details["remedy"]} if isinstance(exc.details, dict) and exc.details.get("remedy") else {})}
    counters.clear_cooldown(provider)
    return {"probed": True, "ok": True, "model": r.model, "latency_ms": r.latency_ms, "cost_usd": r.cost_usd,
            "input_tokens": r.input_tokens, "output_tokens": r.output_tokens}


def health(session: Session, *, probe: bool = False, router: ModelRouter | None = None,
           counters: BudgetCounters | None = None) -> dict[str, Any]:
    from analystos.runtime import budget_counters
    from analystos.runtime.context import default_router
    from analystos.services.platform_settings import get as platform

    cfg = load_models_config()
    counters = counters or budget_counters.default_budget_counters()
    now = counters.clock()
    llm = platform().llm
    spent = counters.value(counters.platform_day_key())
    source = "counter"
    if spent is None:
        spent, source = day_cost_sql(session, now), "database"
    since = now - LOOKBACK
    last_ok = dict(session.execute(select(ModelCall.provider, func.max(ModelCall.created_at))
                                   .where(ModelCall.created_at >= since, ModelCall.status == "ok")
                                   .group_by(ModelCall.provider)).all())
    today = dict(session.execute(select(ModelCall.provider, func.coalesce(func.sum(ModelCall.cost_usd), 0.0))
                                 .where(ModelCall.created_at >= day_start(now)).group_by(ModelCall.provider)).all())
    calls_today = dict(session.execute(select(ModelCall.provider, func.count())
                                       .where(ModelCall.created_at >= day_start(now), ModelCall.status.in_(("ok", "error")))
                                       .group_by(ModelCall.provider)).all())
    providers = []
    for name, p in cfg.providers.items():
        key_present = p.api_key_env is None or bool((os.getenv(p.api_key_env) or "").strip())
        last_error = session.execute(select(ModelCall.created_at, ModelCall.model, ModelCall.error)
                                     .where(ModelCall.created_at >= since, ModelCall.provider == name,
                                            ModelCall.status == "error")
                                     .order_by(ModelCall.created_at.desc()).limit(1)).first()
        local_until = router_module._PROVIDER_COOLDOWN.get(name)
        local_left = max(0.0, local_until - time.monotonic()) if local_until else 0.0
        shared = counters.cooldown(name)
        cooldown = None
        if local_left > 0 or shared:
            cooldown = {"remaining_seconds": round(max(local_left, shared[0] if shared else 0.0)),
                        "reason": shared[1] if shared else "provider refused for credits (HTTP 402) in this process"}
        ok_at = last_ok.get(name)
        entry: dict[str, Any] = {
            "provider": name, "type": p.type, "kind": p.kind, "base_url": p.url(), "api_key_env": p.api_key_env,
            "key_required": p.api_key_env is not None, "key_present": key_present,
            "last_success_at": ok_at.isoformat() if ok_at else None,
            "last_error": {"at": last_error[0].isoformat(), "model": last_error[1], "error": (last_error[2] or "")[:300]}
            if last_error else None,
            "cooldown": cooldown, "spend_today_usd": round(float(today.get(name) or 0.0), 6),
            "calls_today": int(calls_today.get(name) or 0),
            "message": _missing_key_message(p.api_key_env) if not key_present and p.api_key_env else None,
        }
        providers.append(entry)
    if probe:
        router = router or default_router()
        for entry in providers:
            if not entry["key_present"]:
                entry["probe"] = {"probed": False, "ok": False, "detail": entry["message"]}
            else:
                entry["probe"] = _probe(router, entry["provider"], counters)
    cap = llm.daily_spend_cap_usd
    return {
        "checked_at": now.isoformat(),
        "counters_available": counters.available,
        # Hard caps can be proven (Redis, or Postgres in the lite profile); false = billable calls are refused.
        "spend_counters": {"store": counters.spend_store, "available": counters.spend_available},
        "spend_today": {"usd": round(float(spent), 6), "cap_usd": cap, "source": source,
                        "fraction": round(float(spent) / cap, 4) if cap else None,
                        "alert_fraction": llm.spend_alert_fraction, "resets_at": next_day_start(now).isoformat()},
        "providers": providers,
    }


__all__ = ["PROBE_PURPOSE", "health"]
