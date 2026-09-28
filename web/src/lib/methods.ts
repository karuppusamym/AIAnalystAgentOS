/**
 * Plain words for the statistical method, test and effect-size codes the verifier records
 * (skills/, methods/). The code stays the evidence vocabulary; screens show the label and keep the
 * code in a tooltip (UI review P2-17). Unknown codes are humanised rather than hidden.
 */
const LABELS: Record<string, string> = {
  mann_whitney: "Mann-Whitney test",
  mann_whitney_u: "Mann-Whitney test",
  welch_t: "Welch's t-test",
  stouffer_welch_z: "combined t-tests (Stouffer)",
  one_way_anova: "one-way ANOVA",
  kruskal_wallis_h: "Kruskal-Wallis test",
  chi_square_independence: "chi-square test of independence",
  chi_square_retention_by_cohort: "chi-square test across cohorts",
  cochran_mantel_haenszel: "Cochran-Mantel-Haenszel test",
  concentration_gof_uniform: "concentration test (vs. an even spread)",
  multinomial_bootstrap_top_share: "bootstrap of the top share",
  bootstrap_median_difference: "bootstrap of the median difference",
  ols_linear_trend: "linear trend",
  pct_change_theil_sen: "robust trend (Theil-Sen)",
  binary_segmentation: "change-point detection",
  logistic_regression: "logistic regression",
  logistic_regression_l2: "logistic regression",
  random_forest_permutation_importance: "random-forest driver importance",
  robust_z_iqr: "robust outlier score",
  max_abs_robust_z: "largest robust outlier score",
  rate_effect_z: "rate difference (z)",
  combined_z: "combined z-score",
  rank_biserial: "rank-biserial effect",
  hedges_g: "Hedges' g effect",
  eta_squared: "eta-squared effect",
  epsilon_squared: "epsilon-squared effect",
  cramers_v: "Cramér's V effect",
  odds_ratio: "odds ratio",
  odds_ratio_top_vs_baseline: "odds ratio (top vs. baseline)",
  odds_ratio_best_vs_worst_cohort: "odds ratio (best vs. worst cohort)",
  pooled_odds_ratio_after_vs_before: "pooled odds ratio (after vs. before)",
  relative_median_difference: "relative median difference",
  pct_change_fitted: "fitted % change",
  trend_per_period: "trend per period",
  top_share_vs_fair_share: "top share vs. an even share",
  shift_pct: "% shift",
  gini: "Gini concentration",
  pct_deviation_from_forecast: "% deviation from forecast",
  rate_effect_relative_to_kpi_before: "rate change relative to before",
};

export function methodLabel(code: string | null | undefined): string {
  if (!code) return "—";
  const known = LABELS[code.toLowerCase()];
  if (known) return known;
  return code.replace(/_/g, " ").replace(/\bpct\b/g, "%").replace(/\bvs\b/g, "vs.");
}

export function methodsLabel(codes: string[] | null | undefined): string {
  return (codes ?? []).map(methodLabel).join(", ");
}
