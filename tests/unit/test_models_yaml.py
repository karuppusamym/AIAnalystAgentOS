"""The operator's config/models.yaml: whichever models it routes to, each is allowed and priced.

Router behaviour is tested against a pinned table (`pinned_models_config`); this file checks only that
the live table is coherent, so a model change can never silently record cost as `missing_price`.
"""
from __future__ import annotations

from pathlib import Path

from analystos.llm.config import load_models_config

LIVE = Path(__file__).resolve().parents[2] / "config" / "models.yaml"
# The JEV decision model has no published list price; its calls are recorded as missing_price (visible in
# /api/admin/token-savings) until one is added. Any other routed model must carry a price.
UNPRICED_BY_DESIGN = {"typesafe/jev-1.13"}


def _cfg():
    return load_models_config.__wrapped__(LIVE)


def test_every_routed_model_is_allowlisted_and_priced():
    cfg = _cfg()
    routed = {m for p in cfg.profiles.values() for m in [*p.models, *(p.escalation_models or [])]}
    assert routed <= set(cfg.allowlist), routed - set(cfg.allowlist)
    unpriced = {m for m in routed - UNPRICED_BY_DESIGN if m not in cfg.models or cfg.models[m].input_usd_per_mtok is None
                or cfg.models[m].output_usd_per_mtok is None}
    assert not unpriced, f"routed models without a price: {sorted(unpriced)}"
    assert cfg.prices_version


def test_anthropic_models_ask_for_prompt_cache_breakpoints():
    cfg = _cfg()
    for name, meta in cfg.models.items():
        if name.startswith("anthropic/"):
            assert meta.prompt_cache, name
