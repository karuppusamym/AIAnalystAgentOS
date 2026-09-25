"""Platform settings: every runtime knob an administrator can change without a redeploy (§52.8).

Stored as append-only versions (platform_setting); the effective document is the latest version
merged over these defaults. Workspace policy (contracts/policy.py) may only tighten what is set here.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

LLMMode = Literal["off", "auto", "always"]
# Execution ladder (spec v3 §4.1, ADR-0012): what may answer a purpose, cheapest first. cache = exact
# response cache (L0), registry = verified query/hypothesis/metric (L1), rules = deterministic code
# (L2), decision = typed decision model (L3), llm_small / llm_large = low_cost / strong chat profiles
# (L4/L5). `answered_by` on every model_call row is one of these.
Rung = Literal["cache", "registry", "rules", "decision", "llm_small", "llm_large"]
MODEL_RUNGS: tuple[str, ...] = ("decision", "llm_small", "llm_large")

# Purposes where a deterministic path produces an equivalent result (auto = deterministic first).
DETERMINISTIC_CAPABLE = {"planning", "hypothesis_generation", "follow_up_generation", "insight_narrative", "summarization",
                         "semantic_modeling", "dashboard_design", "feedback_interpretation", "metadata_enrichment",
                         "hypothesis_priority", "chart_selection", "feedback_classification", "stop_check", "agent_actions",
                         "ask_route", "clarify_needed", "metric_match", "join_path_choice"}


class LLMSettings(BaseModel):
    """How and when models are called.

    Each purpose runs a ladder (config/models.yaml `ladders`, spec v3 §4.1). `ladders` here replaces a
    purpose's ladder outright; `purpose_modes` (the increment-3 modes, what presets set) derive one
    from the default: off = no model rung, auto = deterministic rungs first and the model only when
    they are insufficient, always = model first. Precedence: ladders > purpose_modes > default.
    """

    purpose_modes: dict[str, LLMMode] = Field(default_factory=dict)  # missing purpose -> its default ladder
    ladders: dict[str, list[Rung]] = Field(default_factory=dict)  # purpose -> ordered rungs (admin override)
    # Per-run caps for one purpose, on top of the workspace run budget: {"calls": n, "tokens": n, "usd": x}.
    purpose_run_caps: dict[str, dict[str, float]] = Field(default_factory=dict)
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


ContextSection = Literal["catalog", "glossary", "business_rules", "metrics", "prior_findings", "negative_knowledge", "episodes"]
CatalogDetail = Literal["names", "columns", "stats", "profile"]


class PurposeProfile(BaseModel):
    """What the context compiler (P4-T03) gives one model purpose, and how much of it.

    `sections` are filled in order after the mandatory part (the caller's required inputs); each
    section is cut in whole items and whatever does not fit is listed as omitted, never truncated.
    `referenced_only` restricts the catalog to the tables the objective/question/SQL names (SQL
    generation and repair). Budgets are characters of compact JSON (≈ 3.6 chars per token)."""

    sections: list[ContextSection] = Field(default_factory=list)
    catalog_detail: CatalogDetail = "columns"
    referenced_only: bool = False
    drop_semantic_types: list[str] = Field(default_factory=list)  # e.g. raw ids, which hypothesis specs cannot use
    max_columns_per_table: int = Field(30, ge=1, le=500)
    max_items_per_section: int = Field(8, ge=0, le=100)
    item_chars: int = Field(240, ge=40, le=4000)  # excerpt length of one knowledge item
    max_chars: int = Field(40_000, ge=1_000, le=1_500_000)


def _default_profiles() -> dict[str, PurposeProfile]:
    knowledge: list[ContextSection] = ["glossary", "business_rules", "metrics"]
    return {
        "planning": PurposeProfile(sections=["catalog", "glossary", "business_rules"], catalog_detail="names",
                                   drop_semantic_types=["id"], max_columns_per_table=20, max_items_per_section=4,
                                   max_chars=16_000),
        "hypothesis_generation": PurposeProfile(sections=["catalog", *knowledge, "prior_findings", "negative_knowledge"],
                                                catalog_detail="stats", drop_semantic_types=["id"],
                                                max_columns_per_table=30, max_chars=48_000),
        "follow_up_generation": PurposeProfile(sections=["catalog", "negative_knowledge"], catalog_detail="stats",
                                               drop_semantic_types=["id"], max_columns_per_table=24,
                                               max_items_per_section=6, max_chars=40_000),
        "sql_generation": PurposeProfile(sections=["catalog", "glossary", "metrics"], catalog_detail="profile",
                                         referenced_only=True, max_columns_per_table=40, max_items_per_section=6,
                                         max_chars=24_000),
        "sql_repair": PurposeProfile(sections=["catalog"], catalog_detail="columns", referenced_only=True,
                                     max_columns_per_table=60, max_chars=16_000),
        "semantic_modeling": PurposeProfile(sections=["metrics", "glossary", "business_rules"], max_items_per_section=5,
                                            max_chars=20_000),
        "feedback_interpretation": PurposeProfile(sections=["catalog", "glossary"], catalog_detail="names",
                                                  max_columns_per_table=60, max_items_per_section=5, max_chars=12_000),
    }


class ContextSettings(BaseModel):
    """Context compiler (P4-T03): per-purpose profiles; purposes without one get only their
    mandatory inputs. `min_relevance` is the share of objective terms a knowledge item must match
    (or a mapped in-scope column) before it may reach a prompt; below it the section says NO_MATCH."""

    compiler_enabled: bool = True
    min_relevance: float = Field(0.15, ge=0.0, le=1.0)
    profiles: dict[str, PurposeProfile] = Field(default_factory=_default_profiles)


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
    # alert_triage materiality rules (ADR-0015): below these a signal keeps its severity and is not
    # escalated or auto-investigated; it is never suppressed (the alert is still raised).
    min_material_effect: float = Field(0.0, ge=0.0, le=10.0)  # |relative change|, 0 = no minimum
    min_material_points: int = Field(3, ge=2, le=520)


class DecisionSettings(BaseModel):
    """DecisionService (ADR-0015) knobs an administrator can change without a redeploy."""

    backends: dict[str, list[str]] = Field(default_factory=dict)  # purpose -> backend order override (rules always kept)
    calibration_window_days: int = Field(30, ge=1, le=365)
    calibration_min_outcomes: int = Field(20, ge=1, le=100_000)  # fewer labelled outcomes = no verdict, no change
    max_brier: float = Field(0.2, ge=0.0, le=1.0)  # spec v3 §5: route/bounded_stop need Brier <= 0.2
    max_ece: float = Field(0.15, ge=0.0, le=1.0)
    purpose_max_brier: dict[str, float] = Field(default_factory=dict)  # per-purpose override
    auto_downgrade: bool = True  # a backend below threshold drops out of that purpose's chain (recorded, reversible)
    pinned: list[str] = Field(default_factory=list)  # "purpose:backend" pairs never downgraded automatically


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
    context: ContextSettings = Field(default_factory=ContextSettings)
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings)
    crawl: CrawlSettings = Field(default_factory=CrawlSettings)
    monitors: MonitorSettings = Field(default_factory=MonitorSettings)
    sources: SourceSettings = Field(default_factory=SourceSettings)
    features: FeatureFlags = Field(default_factory=FeatureFlags)
    decisions: DecisionSettings = Field(default_factory=DecisionSettings)


PRESETS: dict[str, dict[str, LLMMode]] = {
    # The default ladders (P4-T02): deterministic first wherever a rule path exists.
    "balanced": {},
    # Fewest tokens without losing analysis quality: rules first everywhere a rule path exists.
    "token_saver": {p: "auto" for p in DETERMINISTIC_CAPABLE} | {"verification": "always"},
    # Model first for every purpose (the pre-increment-4 default).
    "max_quality": {p: "always" for p in DETERMINISTIC_CAPABLE | {"verification", "sql_generation", "sql_repair",
                                                                 "rev_second_opinion", "risk_check", "alert_triage",
                                                                 "statistical_interpretation"}},
    # No model calls at all: the platform runs entirely on deterministic paths.
    "offline": {p: "off" for p in DETERMINISTIC_CAPABLE | {"verification", "sql_generation", "sql_repair",
                                                          "rev_second_opinion", "risk_check", "alert_triage",
                                                          "statistical_interpretation", "decision_structured"}},
}
