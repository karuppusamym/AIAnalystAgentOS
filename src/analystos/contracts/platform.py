"""Platform settings: every runtime knob an administrator can change without a redeploy (§52.8).

Stored as append-only versions (platform_setting); the effective document is the latest version
merged over these defaults. Workspace policy (contracts/policy.py) may only tighten what is set here.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

LLMMode = Literal["off", "auto", "always"]

# Purposes where a deterministic path produces an equivalent result (auto = deterministic first).
DETERMINISTIC_CAPABLE = {"planning", "hypothesis_generation", "follow_up_generation", "insight_narrative", "summarization",
                         "semantic_modeling", "dashboard_design", "feedback_interpretation", "metadata_enrichment",
                         "hypothesis_priority", "chart_selection", "feedback_classification", "stop_check"}


class LLMSettings(BaseModel):
    """How and when models are called.

    mode per purpose: off = never call (deterministic path only), auto = deterministic first and call
    the model only when the deterministic result is insufficient, always = call when available.
    """

    purpose_modes: dict[str, LLMMode] = Field(default_factory=dict)  # missing purpose -> "always"
    routing_overrides: dict[str, str] = Field(default_factory=dict)  # purpose -> profile
    profile_models: dict[str, list[str]] = Field(default_factory=dict)  # profile -> models (fallback order)
    disabled_models: list[str] = Field(default_factory=list)  # removed from the allowlist at runtime
    cache_enabled: bool = True
    cache_ttl_hours: int = Field(168, ge=0, le=24 * 90)
    cacheable_purposes: list[str] = Field(default_factory=lambda: [
        "planning", "hypothesis_generation", "semantic_modeling", "sql_generation", "feedback_interpretation",
        "metadata_enrichment", "insight_narrative", "summarization", "hypothesis_priority", "chart_selection",
        "rev_second_opinion", "risk_check", "feedback_classification", "alert_triage"])
    max_prompt_tokens: int = Field(16000, ge=500, le=400_000)  # estimated input tokens per call; larger prompts are refused (deterministic fallback)
    downgrade_below_budget_fraction: float = Field(0.25, ge=0.0, le=1.0)  # when a run has less budget left, chat purposes use the low_cost profile
    compact_prompts: bool = True
    catalog_max_columns_per_table: int = Field(30, ge=5, le=500)
    catalog_max_tables: int = Field(12, ge=1, le=200)


class AnalysisSettings(BaseModel):
    max_round1_hypotheses: int = Field(8, ge=1, le=30)
    max_followups_per_round: int = Field(3, ge=0, le=10)
    # Statistical safeguards can only be tightened: REV uses max(critic.MIN_N, this value).
    min_sample_size: int = Field(100, ge=30, le=1_000_000)
    sample_rows: int = Field(50000, ge=1000, le=5_000_000)
    heuristic_hypotheses_sufficient: int = Field(6, ge=1, le=30)  # auto mode: skip the model if rules produce this many
    max_charts: int = Field(16, ge=1, le=60)


class CrawlSettings(BaseModel):
    default_mode: Literal["full", "incremental"] = "incremental"
    max_tables: int = Field(2000, ge=1, le=100_000)
    profile_changed_only: bool = True
    profile_sample_rows: int = Field(100000, ge=0, le=10_000_000)
    llm_enrichment: bool = False  # deterministic semantics always run; the model only fills placeholders when enabled
    enrichment_batch_tables: int = Field(25, ge=1, le=100)
    enrichment_max_columns: int = Field(12, ge=1, le=60)
    pii_value_sampling: bool = True  # classify PII from a small sample of values (values never leave the platform)


class MonitorSettings(BaseModel):
    default_lookback: int = Field(8, ge=3, le=520)
    default_z_threshold: float = Field(3.0, ge=1.0, le=10.0)
    triage_escalate_probability: float = Field(0.8, ge=0.5, le=1.0)
    auto_investigation_enabled: bool = True


class SourceSettings(BaseModel):
    enabled_kinds: list[str] = Field(default_factory=list)  # empty = every kind in the catalog
    allow_pushdown: bool = True
    staged_max_rows: int = Field(1_000_000, ge=1000, le=100_000_000)


class FeatureFlags(BaseModel):
    jev_decisions: bool = True
    superset_publishing: bool = True
    python_sandbox: bool = True
    reports: bool = True


class PlatformSettings(BaseModel):
    llm: LLMSettings = Field(default_factory=LLMSettings)
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings)
    crawl: CrawlSettings = Field(default_factory=CrawlSettings)
    monitors: MonitorSettings = Field(default_factory=MonitorSettings)
    sources: SourceSettings = Field(default_factory=SourceSettings)
    features: FeatureFlags = Field(default_factory=FeatureFlags)


PRESETS: dict[str, dict[str, LLMMode]] = {
    # Model quality where it matters most; deterministic where output is equivalent.
    "balanced": {"feedback_classification": "always", "insight_narrative": "always", "planning": "always"},
    # Fewest tokens without losing analysis quality: rules first everywhere a rule path exists.
    "token_saver": {p: "auto" for p in DETERMINISTIC_CAPABLE} | {"verification": "always"},
    "max_quality": {},
    # No model calls at all: the platform runs entirely on deterministic paths.
    "offline": {p: "off" for p in DETERMINISTIC_CAPABLE | {"verification", "sql_generation", "sql_repair",
                                                          "rev_second_opinion", "risk_check", "alert_triage",
                                                          "statistical_interpretation"}},
}
