# Analytical benchmark (P4-V01) — 2026-09-25

Generated 2026-09-25 22:04 UTC by `scripts/benchmark_analytical.py` at `581fd8c` (Python 3.11.15). Models: **off** (no provider key: every model purpose takes its rule path).

Datasets: `analystos.evaluation.datasets` — one table per domain generated from an explicit causal graph (ITSM incidents with 4 planted effects, sales orders with 3, accounts-payable invoices with 3; two null columns each, drawn independently of everything). Global-null replicates regenerate the same tables with every planted effect set to zero. A verified finding is *true* when its outcome and segment share an ancestor in the graph; FDR is the mean false-discovery proportion per replicate, compared with the nominal α = 0.05.

Thresholds: precision_min 0.9, recall_min 0.9, fdr_max 0.05, null_fdr_max 0.05.

## Tier: platform

| Measure | Value |
|---|---|
| replicates (with effects + global null) | 9 |
| hypotheses tested | 92 |
| verified findings | 24 (true 24, false 0) |
| **precision** of verified findings | **1.000** |
| **recall** of planted effects | **1.000** (20/20) |
| **FDR** (replicates with effects) vs α 0.05 | **0.000** |
| **FDR under the global null** (= FWER there) | **0.000** |
| null hypotheses tested | 66 |
| null hypotheses with raw p < α (before effect-size gates, BH, second method) | n/a |

| Domain | replicates | verified | precision | recall | FDR | null FDR | null tests | raw p<α on nulls | seconds |
|---|---|---|---|---|---|---|---|---|---|
| itsm | 3 | 10 | 1.000 | 1.000 (8/8) | 0.000 | 0.000 | 21 | n/a | 45.4 |
| sales | 3 | 8 | 1.000 | 1.000 (6/6) | 0.000 | 0.000 | 21 | n/a | 25.4 |
| finance | 3 | 6 | 1.000 | 1.000 (6/6) | 0.000 | 0.000 | 24 | n/a | 18.7 |

Missed planted effects: none.

False verified findings: none.

## Scope and limits

* Planted effects are sized clearly above the verdict layer's materiality thresholds (skills/stats.py), so recall
  here is recall for material effects; power close to the thresholds is covered by tests/benchmarks/test_analytical_benchmarks.py,
  not by this report.
* The component tier reads raw p-values, so it also reports test calibration (share of null hypotheses with raw p < α,
  expected ≈ α). The platform tier scores what a real run stored (verified insights and tested hypotheses).
* A run with a live or local model (`--models live`) is a separate dated report; a model changes which hypotheses are
  proposed and how findings are worded, never the statistics, BH or the second-method gate.

## Verdict

PASS — every threshold met.
