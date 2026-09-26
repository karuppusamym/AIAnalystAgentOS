# Method-specific evaluation (P4-03) — 2026-09-26

Generated 2026-09-26 04:04 UTC by `scripts/benchmark_methods.py` at `eb10924` (Python 3.11.15), 200 seeded datasets per point, 169.3 s. No services and no model calls: the verdict layer (`skills/stats.py`) the methods use for their primary test.

Thresholds: null_supported_max 0.08, power_at_2x_min 0.8, holdout_fpr_max 0.08, same_data_fpr_min 0.1; nominal α = 0.05.

## Null behaviour and power near the materiality threshold

Supported rate (the method's verdict: significant *and* the effect clears the materiality threshold) as the true effect is a multiple k of the threshold. k = 0 is the null.

| Method | threshold | design | 0x | 0.5x | 1x | 1.5x | 2x | 3x | raw p<α at k=0 |
|---|---|---|---|---|---|---|---|---|---|
| rate_by_segment | rate ratio >= 1.2 (and Cramér's V >= 0.05) | 4 groups x 2000, baseline rate 0.20, one group at 0.20 x (1 + 0.20 k) | 0.000 | 0.010 | 0.375 | 0.895 | 0.995 | 1.000 | 0.040 |
| numeric_by_segment | |rank-biserial| >= 0.1 | 2 groups x 1500, normal(0,1) vs normal(shift,1), shift for rank-biserial 0.1 k | 0.000 | 0.015 | 0.505 | 0.995 | 1.000 | 1.000 | 0.065 |
| correlation | |Spearman rho| >= 0.1 | 1500 pairs, bivariate normal with Spearman rho 0.1 k | 0.000 | 0.045 | 0.505 | 0.960 | 1.000 | 1.000 | 0.050 |
| trend | |fitted change| >= 10% first to last period | 26 periods, level 100 + noise sd 5, fitted change 10% k | 0.000 | 0.065 | 0.495 | 0.935 | 1.000 | 1.000 | 0.020 |

Reading: at k = 1 an effect exactly at the threshold is reported about half the time by construction (the estimate must clear it); power reaches the 2x target with these sample sizes. The null column is the per-method false-positive rate of a single test before Benjamini-Hochberg and the second method, which lower it further.

## Selection effects

Design: 8 groups x 1000 rows, rate 0.20 (null) / one group at 0.30 (planted); 400 datasets.

| Measure | Value |
|---|---|
| quoted top-vs-bottom rate ratio under the null (true 1.0): mean / 95th pct | 1.199 / 1.307 |
| planted: true rate ratio vs mean quoted when supported | 1.5 vs 1.639 (supported 0.998) |
| post-hoc top group tested on the **same** data: false-positive rate | **0.225** |
| post-hoc top group (chosen on one half) tested on the **held-out** half | **0.052** |

Reading: choosing the top and bottom groups after looking inflates the quoted ratio even when nothing is there, and testing a group chosen on the same data rejects far more often than α. This is why P4-03 labels every finding of an adaptive round a *discovery* and only a held-out partition or a pre-registered re-test on a new data version may label it *confirmed* (`evidence/confirmation.py`). The quoted ratio is shown with its interval and the selection procedure in the evidence bundle's method dimension.

## V01 component suite by method

Seeds 1-10 with planted effects, global-null seeds 101-105; ITSM, sales, finance.

| Method | tested | verified | precision | null tests | raw p<α on nulls | FDR under the global null |
|---|---|---|---|---|---|---|
| driver_model | 30 | 20 | 1.000 | 10 | 0.000 | 0.000 |
| numeric_by_segment | 450 | 60 | 1.000 | 390 | 0.061 | 0.000 |
| pareto | 15 | 10 | 1.000 | 5 | 0.000 | 0.000 |
| rate_by_segment | 285 | 30 | 1.000 | 255 | 0.043 | 0.000 |
| trend | 45 | 0 | n/a | 45 | 0.000 | 0.000 |

## Platform acceptance (integration, 2026-09-26)

`tests/integration/test_typed_evidence_p403.py` against the compose Postgres (staged Postgres source, local
orchestrator, no model provider), seeded retail orders with planted effects:

* run 1: 4 findings, 4 verified, all `exploratory` (label discovery); each carries a complete evidence bundle, a
  content-versioned manifest entry (24,000 rows) and a narrative whose every number binds to a typed fact;
* re-staging identical data kept the content version: nothing stale;
* deleting 2,666 origin rows and re-staging (24,000 -> 21,334 rows) marked all 4 run-1 findings stale
  (4 `insight.stale` events);
* run 2 (previous_run_id = run 1) re-tested the 4 carried claims on the new snapshot: 4 verified, 4 `confirmed`
  by `fresh_snapshot_replication`.

Migration 0030 up / down / up on a scratch database (`test_migration_0030_maps_legacy_badges_and_reverts`):
verified -> legacy (equivalent exploratory), failed -> inconclusive, rejected -> invalid, draft -> legacy;
`verified` / `confidence` values unchanged.

## Not covered here

* pareto, driver_model, cohort_retention and contribution_decomposition have no closed-form threshold sweep here; their planted/null cases are in `tests/methods` and `tests/unit/test_skills_analysis.py`, and their platform-level null FDR is the V01 per-method table.
* Power is for the primary verdict; a verified finding additionally needs BH across the run's family and the second method, so platform power is lower (V01 recall is for material planted effects).
* A live-model tier does not change these numbers: models never compute statistics.

## Verdict

PASS — every threshold met.
