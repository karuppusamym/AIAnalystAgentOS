# Analytical benchmark (P4-V01) — 2026-09-25

Generated 2026-09-25 22:05 UTC by `scripts/benchmark_analytical.py` at `581fd8c` (Python 3.11.15). Models: **off** (no provider key: every model purpose takes its rule path).

Datasets: `evaluation.datasets` — one table per domain generated from an explicit causal graph (ITSM incidents with 4 planted effects, sales orders with 3, accounts-payable invoices with 3; two null columns each, drawn independently of everything). Global-null replicates regenerate the same tables with every planted effect set to zero. A verified finding is *true* when its outcome and segment share an ancestor in the graph; FDR is the mean false-discovery proportion per replicate, compared with the nominal α = 0.05.

Thresholds: precision_min 0.9, recall_min 0.9, fdr_max 0.05, null_fdr_max 0.05.

## Tier: component

| Measure | Value |
|---|---|
| replicates (with effects + global null) | 45 |
| hypotheses tested | 825 |
| verified findings | 120 (true 120, false 0) |
| **precision** of verified findings | **1.000** |
| **recall** of planted effects | **1.000** (100/100) |
| **FDR** (replicates with effects) vs α 0.05 | **0.000** |
| **FDR under the global null** (= FWER there) | **0.000** |
| null hypotheses tested | 705 |
| null hypotheses with raw p < α (before effect-size gates, BH, second method) | 0.050 |

| Domain | replicates | verified | precision | recall | FDR | null FDR | null tests | raw p<α on nulls | seconds |
|---|---|---|---|---|---|---|---|---|---|
| itsm | 15 | 50 | 1.000 | 1.000 (40/40) | 0.000 | 0.000 | 190 | 0.021 | 88.6 |
| sales | 15 | 40 | 1.000 | 1.000 (30/30) | 0.000 | 0.000 | 350 | 0.060 | 64.2 |
| finance | 15 | 30 | 1.000 | 1.000 (30/30) | 0.000 | 0.000 | 165 | 0.061 | 18.5 |

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
