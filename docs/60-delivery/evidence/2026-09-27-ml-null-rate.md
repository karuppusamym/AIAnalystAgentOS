# P5-07 — null false-positive control for ML `improved` (2026-09-27)

**Row:** P5-07 (tracker). **Rule now:** `ml/evaluation.decide` promotes a candidate only when (1) the holdout point gain
exceeds `min_improvement`, (2) the 95% paired-bootstrap interval of the gain excludes zero (unchanged), **and (3) the
win is confirmed** (`MLSpec.confirmation`, `ImprovementConfirmation`, default on):

* **cross-validation** — the search's paired out-of-fold folds (baseline and candidate on the same validation rows,
  never holdout rows): the candidate is better in a majority of folds and on average;
* **holdout at the confirmation level** — the gain's interval at `holdout_level` (default **0.99**, read from ≥ 1000
  resamples) excludes zero.

Applied in every task: tabular classify/regress, forecast (rolling-origin backtest folds), cluster (random-partition
baseline folds, plus the stability check) and labelled anomaly. `confirmation: {cross_validation: false,
holdout_level: 0.95}` restores the pre-P5-07 rule for an owner who wants it; `holdout_level` may not go below 0.95.
The decision records the evidence (`decision.confirmation`: fold gains, the 99% bound, pass/fail and why).

## Measurement

Held-out generator `evaluation/heldout/generators.py`, `ml_pumps` variant `null_target` (label independent of every
feature), trained through `analystos.ml.jobs.run_ml_job` exactly as `runner.run_ml_component` does (caps 4 trials /
120 s). Command: `python -m evaluation.ml_null --seeds 8000-8399 --workers 4` (raw result:
`2026-09-27-ml-null-rate.json`).

| Rule | Seeds | `improved` | Null rate |
|---|---|---|---|
| before P5-07 (95% interval only) | 8000–8199 | 3 (8076, 8099, 8174) | **1.5%** (matches the register's 3/200) |
| before P5-07 | 8000–8399 | 9 | **2.25%** |
| **P5-07 default (confirmed)** | 8000–8199 | 1 (8174) | **0.5%** |
| **P5-07 default (confirmed)** | 8000–8399 | 1 (8174) | **0.25%** |
| gate sample (`ml` tier, `GATE_SEEDS` 9000–9059) | 60 | 0 | 0.0% |

Target ≤ 0.5%: met on both ranges. What refused the eight former false positives (per seed, holdout gain / CV / 99% bound):
8076 0.160 / pass / −0.012; 8099 0.147 / pass / −0.020; 8209 0.139 / pass / −0.015; 8256 0.129 / pass / −0.003;
8272 0.111 / **fail** / −0.052; 8274 0.104 / pass / −0.050; 8314 0.104 / **fail** / −0.059; 8394 0.116 / pass / −0.035.
The 99% holdout bound does most of the work; the selection-biased CV evidence passes ~70% of nulls on its own
(measured 292/400 majority-positive before the change) and adds a check the holdout cannot give (two of the nine).
The one remaining (8174) passes all three: gain 0.151 AUC, 3/3 folds positive, 99% bound +0.004.

## Power on planted signals

| Signal | Verdict (confirmed rule) |
|---|---|
| `evaluation/ml.py` classify / regress / forecast / cluster | improved / improved / improved / improved |
| held-out HO-ML-01 classify (`ml_pumps` 7301) | improved |
| held-out HO-ML-02 regress (`ml_pumps` 7302) | improved |
| held-out HO-ML-03 forecast (`ml_calls` 7303) | no_improvement — **unchanged**: the unconfirmed rule also says no_improvement (95% interval reaches −0.82 MAE), as the 2026-09-27 component-tier report recorded (split sensitivity) |

Every planted signal the old rule detected is still detected (6/6; 6/7 including HO-ML-03, which neither rule detects).

## Cost

The confirmation bound reads 1000 bootstrap resamples. Binary ROC AUC is now computed as the Mann-Whitney statistic
(`ml/evaluation.binary_auc`, identical to `roc_auc_score` within 2.2e-16 over 2000 random cases with ties), ~20× faster,
so the bootstrap is cheaper than the 200-resample sklearn version it replaced.

## Gates

`config/eval_gates.yaml` v4: the `ml` tier gains `null_rate {max: 0.005}` and `null_seeds {min: 60}` (fixed seeds, so
deterministic). `python scripts/eval_gates.py` → **PASS** (ml: null_rate 0.0 over 60 seeds, signal_detected_share 1.0,
null_abstain_share 1.0; heldout stays a non-blocking report at 0.85 accepted, unchanged).
