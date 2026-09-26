"""Catalog of deterministic skills (spec §14). Each entry names the implementing function by dotted path
so the tool registry / agents can resolve it lazily (`resolve_skill`)."""
from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

CATEGORIES = ("data_understanding", "profiling", "analysis", "statistical", "ml", "bi", "engineering", "governance")
_P = "analystos.skills"


def _s(id_: str, category: str, description: str, function: str, **extra: Any) -> dict[str, Any]:
    return {"id": id_, "category": category, "description": description, "deterministic": True,
            "function": function if function.startswith("analystos.") else f"{_P}.{function}", "runtime": extra.pop("runtime", "in_process"), **extra}


SKILLS: list[dict[str, Any]] = [
    # data understanding
    _s("relationship_detection", "data_understanding",
       "Discover join keys from declared references and name heuristics, validated by SQL containment and cardinality.",
       "relationships.discover_relationships"),
    _s("column_semantics", "data_understanding",
       "Infer semantic type (id, numeric, categorical, boolean, datetime, text) from type, name and profile stats.",
       "profiling.infer_semantic_type"),
    _s("semantic_inference", "data_understanding",
       "Rule-based business name, table role (fact/dimension/bridge/event/reference/staging/audit), domain, "
       "grain and column roles/units from names, types and references; zero LLM tokens, with confidence and evidence.",
       "catalog.infer_table_semantics", covers=["column_role_inference"]),
    _s("sql_explanation", "data_understanding",
       "Explain a SQL statement from its parse tree (tables, joins, filters, grouping, aggregations) without a model.",
       "sqlexplain.explain_sql"),
    _s("crawl_diff", "data_understanding",
       "Order-independent structural fingerprints and crawl diff: new, changed (added/removed/retyped columns), "
       "unchanged, missing/deprecated (full crawls only) and rename candidates.", "catalog.diff_crawl"),
    # governance
    _s("pii_classification", "governance",
       "PII category and sensitivity from column names, strengthened by sample-value patterns (email, phone, "
       "Luhn-checked cards, SSN, IP); never echoes values.", "catalog.classify_pii"),
    _s("glossary_linking", "governance",
       "Link columns to glossary terms by explicit mapping or stemmed token overlap with names and synonyms.",
       "catalog.link_glossary"),
    # profiling
    _s("dataset_profile", "profiling",
       "Pushdown profile of a table: counts, nulls, distinct, min/max/mean/stddev, percentiles, top values, "
       "monthly counts, histograms, IQR outlier candidates, candidate keys (bounded number of queries).",
       "profiling.profile_asset",
       covers=["numeric_profile", "categorical_profile", "datetime_profile", "outlier_detection",
               "uniqueness_analysis", "missingness_analysis"]),
    _s("data_quality_checks", "profiling",
       "Null rates, duplicate keys, orphan references, temporal order violations, future timestamps, constant "
       "columns and case-variant categories, with counts, percentages and the SQL that measured them.",
       "quality.check_quality"),
    # analysis
    _s("analysis_spec_compiler", "analysis",
       "Compile an AnalysisSpec (closed derivation vocabulary) into pushdown SQL for postgres, tsql and duckdb.",
       "sqlbuild.compile_spec"),
    _s("run_analysis", "analysis",
       "Execute an AnalysisSpec through the gateway and compute its primary statistic "
       "(segmentation, trend, pareto, correlation, driver model).", "analysis.run_analysis",
       covers=["segmentation", "trend_analysis", "pareto_analysis", "root_cause_analysis", "descriptive_statistics"]),
    _s("verify_analysis", "analysis",
       "Independent second-method verification of a finding; reports agreement with the primary verdict.",
       "analysis.verify_analysis"),
    _s("pareto_analysis", "analysis", "Top-k share, share of the top 20% segments, segments for 80%, Gini, GOF test.",
       "stats.pareto_concentration"),
    _s("trend_analysis", "analysis", "OLS linear trend with % change, Mann-Kendall tau and Durbin-Watson check.",
       "stats.linear_trend"),
    _s("change_point_detection", "analysis", "Binary-segmentation mean-shift detection with before/after means.",
       "stats.change_point"),
    # statistical
    _s("chi_square", "statistical",
       "Chi-square test of rates across segments with Cramér's V, Wilson CIs and rate ratios.", "stats.chi_square_rates"),
    _s("nonparametric_test", "statistical", "Mann-Whitney U (rank-biserial) / Kruskal-Wallis (epsilon squared).",
       "stats.compare_groups"),
    _s("t_test", "statistical", "Welch t-test with Hedges' g and CI of the mean difference.", "stats.welch_t_test"),
    _s("anova", "statistical", "One-way ANOVA with eta squared and a Levene variance check.", "stats.one_way_anova"),
    _s("correlation", "statistical", "Spearman and Pearson correlation with Fisher-z confidence intervals.",
       "stats.correlation"),
    _s("logistic_regression", "statistical",
       "Logistic regression with odds ratios, Wald CIs, drop-one LR tests; separation-safe.", "stats.logistic_regression"),
    _s("confidence_interval", "statistical", "Percentile bootstrap CI for any statistic (seeded).", "stats.bootstrap_ci"),
    _s("significance_testing", "statistical", "Benjamini-Hochberg FDR adjustment of p-values.", "stats.benjamini_hochberg"),
    _s("proportion_ci", "statistical", "Wilson score interval for a proportion.", "stats.wilson_ci"),
    _s("permutation_test", "statistical", "Monte Carlo permutation test of independence for a rate table.",
       "stats.permutation_chi_square"),
    # ml
    _s("feature_importance", "ml",
       "Random forest + grouped permutation importance on a held-out split (fixed random_state).",
       "stats.feature_importance"),
    _s("anomaly_detection", "ml", "Robust anomalies via median/MAD modified z-scores and IQR fences.",
       "stats.robust_anomalies"),
    _s("forecasting", "ml",
       "Holt-Winters forecast (additive damped trend, seasonality with >= 2 seasons) with seeded simulated "
       "prediction intervals; naive/drift fallback for short series.", "forecast.forecast_series"),
    _s("forecast_deviation", "ml",
       "Flag the latest point(s) falling outside the forecast interval fitted on the preceding history.",
       "forecast.forecast_deviation"),
    # bi
    _s("chart_selection", "bi", "Chart type from intent, dimension type, cardinality and metric count (§34).",
       "viz.choose_chart"),
    _s("dashboard_layout", "bi", "12-column executive / operational dashboard layout (§33).", "dashboards.build_layout",
       covers=["executive_dashboard", "operational_dashboard"]),
    _s("native_filter_selection", "bi", "Dashboard native filters from chart dimensions and dataset columns.",
       "dashboards.choose_native_filters"),
    # engineering
    _s("python_sandbox", "engineering",
       "Run allow-listed numeric Python in an isolated child (sandbox container or namespaces: no network, "
       "read-only filesystem, resource limits); refused when isolation is unavailable.",
       "analystos.sandbox.runner.run_python", runtime="sandbox"),
]

def get_skill(skill_id: str) -> dict[str, Any]:
    for s in SKILLS:
        if s["id"] == skill_id:
            return s
    raise KeyError(skill_id)


def resolve_skill(skill_id: str) -> Callable[..., Any]:
    """Import and return the implementing function of `skill_id`."""
    mod, _, fn = get_skill(skill_id)["function"].rpartition(".")
    return getattr(importlib.import_module(mod), fn)
