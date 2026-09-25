from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from analystos.core.config import get_settings


class ProviderConfig(BaseModel):
    kind: str
    base_url: str
    api_key_env: str
    region: str | None = None  # where the provider processes requests; None = unknown (fails a residency policy)


class ModelMeta(BaseModel):
    """Optional per-model metadata. Prices are list-price estimates used only for the pre-call
    approval check (policy.expensive_model_approval_usd); billed cost is always the provider's."""

    region: str | None = None  # overrides the provider region; None = inherit (unknown if both are None)
    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None
    # Provider prompt caching needs explicit breakpoints (Anthropic `cache_control`, also through
    # OpenRouter). Families with automatic prefix caching (OpenAI, DeepSeek, Gemini) leave it false:
    # the cache-stable prompt order is enough for them.
    prompt_cache: bool = False


class ProfileConfig(BaseModel):
    models: list[str]
    provider: str = "openrouter"
    temperature: float = 0.2
    max_tokens: int = 2000
    timeout_seconds: float = 60
    exclude_families: list[str] = Field(default_factory=list)


class ModelsConfig(BaseModel):
    providers: dict[str, ProviderConfig]
    allowlist: list[str]
    profiles: dict[str, ProfileConfig]
    routing: dict[str, str]
    models: dict[str, ModelMeta] = Field(default_factory=dict)

    def region_of(self, model: str, provider: str) -> str | None:
        meta = self.models.get(model)
        if meta and meta.region:
            return meta.region
        cfg = self.providers.get(provider)
        return cfg.region if cfg else None

    def prompt_cache(self, model: str) -> bool:
        meta = self.models.get(model)
        return bool(meta and meta.prompt_cache)

    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float | None:
        """Upper-bound USD estimate for one call, or None when the model has no price metadata."""
        meta = self.models.get(model)
        if meta is None or meta.input_usd_per_mtok is None or meta.output_usd_per_mtok is None:
            return None
        return (input_tokens * meta.input_usd_per_mtok + output_tokens * meta.output_usd_per_mtok) / 1_000_000

    def profile_for(self, purpose: str) -> tuple[str, ProfileConfig]:
        name = self.routing.get(purpose)
        if not name or name not in self.profiles:
            raise KeyError(f"No model profile routed for purpose '{purpose}'")
        return name, self.profiles[name]

    def public_view(self) -> dict[str, Any]:
        return {
            "allowlist": self.allowlist,
            "profiles": {k: v.model_dump() for k, v in self.profiles.items()},
            "routing": self.routing,
            "providers": {k: {"kind": v.kind, "base_url": v.base_url, "region": v.region} for k, v in self.providers.items()},
            "models": {k: v.model_dump() for k, v in self.models.items()},
        }


def family(model: str) -> str:
    return model.split("/", 1)[0]


@lru_cache
def load_models_config(path: Path | None = None) -> ModelsConfig:
    data = yaml.safe_load((path or get_settings().models_config).read_text())
    return ModelsConfig.model_validate(data)
