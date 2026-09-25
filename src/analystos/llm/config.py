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
            "providers": {k: {"kind": v.kind, "base_url": v.base_url} for k, v in self.providers.items()},
        }


def family(model: str) -> str:
    return model.split("/", 1)[0]


@lru_cache
def load_models_config(path: Path | None = None) -> ModelsConfig:
    data = yaml.safe_load((path or get_settings().models_config).read_text())
    return ModelsConfig.model_validate(data)
