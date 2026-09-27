# Held-out evaluation (P4-08) — 2026-09-27

Generated 2026-09-27 01:26 UTC by the held-out runner (`evaluation/heldout`) at `ae5e412` (Python 3.11.15). Corpus `evaluation/heldout/corpus.yaml` version 1 (frozen 2026-09-27, sha256 `0f36bfa7cb8c`). Models: **off** (no provider key: every model purpose takes its rule path).

Every task ends in one status (`evaluation/heldout/runner.py`): accepted, correct_abstention, confident_wrong, incomplete, unnecessary_abstention, wrong_abstention or error. Rubrics were sealed before the first scored run; the corpus lock (`corpus.lock.json`) hashes every task's data, reference and rubric.

Proposed thresholds (non-blocking `heldout` gate, owner to confirm): accepted_output_rate ≥ 0.9, confident_wrong ≤ 0, abstention_recall ≥ 1.0, errors ≤ 0.

## Tier: component

Corpus lock: matches. Wall time 34.4 s.

| Measure | Value |
|---|---|
| tasks (deliver / abstain) | 34 (20 / 14) |
| **accepted-output rate** (accepted / deliver tasks) | **0.850** (17/20) |
| **confident-wrong** | **0** |
| **abstention correctness** (abstain tasks ended as the rubric requires) | **1.000** |
| abstention precision (abstentions that were on abstain tasks) | 0.933 |
| unnecessary abstentions | 1 |
| task success (accepted + correct abstentions) / tasks | 0.912 |
| errors | 0 |
| latency p50 / p95 / max (s, per task) | 0.740 / 2.857 / 3.608 |
| model calls / tokens / USD | 0 / 0 / 0.0 |
| cost per accepted output: model USD / CPU s / wall s / infrastructure | 0.000 / 2.288 / 1.984 / unpriced |

| Domain | tasks | accepted | accepted-output rate | confident-wrong | abstention correctness | unnecessary abstentions | errors | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|---|
| itsm | 6 | 2 | 0.500 | 0 | 1.000 | 0 | 0 | 0.826 | 2.734 |
| sales | 6 | 4 | 1.000 | 0 | 1.000 | 0 | 0 | 0.899 | 0.951 |
| finance | 6 | 4 | 1.000 | 0 | 1.000 | 0 | 0 | 0.748 | 0.942 |
| engineering | 8 | 5 | 1.000 | 0 | 1.000 | 0 | 0 | 0.538 | 3.370 |
| ml | 8 | 2 | 0.667 | 0 | 1.000 | 1 | 0 | 0.410 | 2.062 |

### Every task

| Task | family | expect | status | abstained as | seconds | why |
|---|---|---|---|---|---|---|
| HO-ITSM-01 | analysis | deliver | accepted | — | 2.744 | every planted effect verified, no false finding |
| HO-ITSM-02 | analysis | deliver | accepted | — | 2.704 | every planted effect verified, no false finding |
| HO-ITSM-03 | analysis | deliver | incomplete | — | 0.697 | missed planted effect(s): fulfilment_by_site |
| HO-ITSM-04 | analysis | deliver | incomplete | — | 0.483 | missed planted effect(s): fulfilment_by_site |
| HO-ITSM-05 | analysis | abstain (no_finding) | correct_abstention | no_finding | 0.955 | no false finding (1 true non-planted finding(s) reported) |
| HO-ITSM-06 | analysis | abstain (no_finding) | correct_abstention | no_finding | 0.227 | no false finding |
| HO-SALES-01 | analysis | deliver | accepted | — | 0.921 | every planted effect verified, no false finding |
| HO-SALES-02 | analysis | deliver | accepted | — | 0.908 | every planted effect verified, no false finding |
| HO-SALES-03 | analysis | deliver | accepted | — | 0.961 | every planted effect verified, no false finding |
| HO-SALES-04 | analysis | deliver | accepted | — | 0.889 | every planted effect verified, no false finding |
| HO-SALES-05 | analysis | abstain (no_finding) | correct_abstention | no_finding | 0.677 | no false finding |
| HO-SALES-06 | analysis | abstain (no_finding) | correct_abstention | no_finding | 0.337 | no false finding |
| HO-FIN-01 | analysis | deliver | accepted | — | 0.79 | every planted effect verified, no false finding |
| HO-FIN-02 | analysis | deliver | accepted | — | 0.705 | every planted effect verified, no false finding |
| HO-FIN-03 | analysis | deliver | accepted | — | 0.862 | every planted effect verified, no false finding |
| HO-FIN-04 | analysis | deliver | accepted | — | 0.969 | every planted effect verified, no false finding |
| HO-FIN-05 | analysis | abstain (no_finding) | correct_abstention | no_finding | 0.506 | no false finding |
| HO-FIN-06 | analysis | abstain (no_finding) | correct_abstention | no_finding | 0.25 | no false finding |
| HO-DE-01 | engineering | deliver | accepted | — | 0.531 | output equals the reference |
| HO-DE-02 | engineering | deliver | accepted | — | 0.544 | output equals the reference |
| HO-DE-03 | engineering | deliver | accepted | — | 0.429 | output equals the reference |
| HO-DE-04 | engineering | abstain (blocked) | correct_abstention | blocked | 0.419 | fail gate range(weight_kg) |
| HO-DE-05 | engineering | abstain (refused) | correct_abstention | refused | 0.009 | validation: node delivered (filter): column shipment_status does not exist here (have: carrier_code, loaded_at, ship_date, shipment_no, shipment_state, weight_kg) |
| HO-DE-06 | engineering | deliver | accepted | — | 3.608 | output equals the reference |
| HO-DE-07 | engineering | abstain (refused) | correct_abstention | refused | 2.927 | join pre-flight: priced declared many_to_one, observed many_to_many |
| HO-DE-08 | engineering | deliver | accepted | — | 2.82 | output equals the reference |
| HO-ML-01 | ml | deliver | accepted | — | 2.016 | improved over the baseline on identical splits |
| HO-ML-02 | ml | deliver | accepted | — | 0.847 | improved over the baseline on identical splits |
| HO-ML-03 | ml | deliver | unnecessary_abstention | no_finding | 0.776 | verdict no_improvement |
| HO-ML-04 | ml | abstain (no_finding) | correct_abstention | no_finding | 2.087 | verdict no_improvement |
| HO-ML-05 | ml | abstain (refused) | correct_abstention | refused | 0.036 | target_derived_features: these features alone reproduce the target (derived from it?): work_order_raised (single threshold 1.0) |
| HO-ML-06 | ml | abstain (refused) | correct_abstention | refused | 0.036 | post_cutoff_timestamps: feature values observed after the prediction cutoff: failure_date (132 rows); post_outcome_timestamps: feature values observed at or after the outcome: failure_date (132 rows) |
| HO-ML-07 | ml | abstain (refused) | correct_abstention | refused | 0.044 | group_aware_split: entity ['asset_tag'] repeats across rows; a random split would put one entity in both training and holdout (declare group_keys) |
| HO-ML-08 | ml | abstain (refused) | correct_abstention | refused | 0.015 | minimum_rows: insufficient data: 18 usable rows, need >= 40 (insufficient evidence is a valid outcome) |

Failures and non-accepted outcomes (3): HO-ITSM-03 incomplete; HO-ITSM-04 incomplete; HO-ML-03 unnecessary_abstention.
Abstentions: HO-ITSM-05 no_finding (correct_abstention); HO-ITSM-06 no_finding (correct_abstention); HO-SALES-05 no_finding (correct_abstention); HO-SALES-06 no_finding (correct_abstention); HO-FIN-05 no_finding (correct_abstention); HO-FIN-06 no_finding (correct_abstention); HO-DE-04 blocked (correct_abstention); HO-DE-05 refused (correct_abstention); HO-DE-07 refused (correct_abstention); HO-ML-03 no_finding (unnecessary_abstention); HO-ML-04 no_finding (correct_abstention); HO-ML-05 refused (correct_abstention); HO-ML-06 refused (correct_abstention); HO-ML-07 refused (correct_abstention); HO-ML-08 refused (correct_abstention).

Status counts: accepted 17, correct_abstention 14, confident_wrong 0, incomplete 2, unnecessary_abstention 1, wrong_abstention 0, error 0.

## Tier: platform

Corpus lock: matches. Wall time 90.1 s.

| Measure | Value |
|---|---|
| tasks (deliver / abstain) | 34 (20 / 14) |
| **accepted-output rate** (accepted / deliver tasks) | **0.900** (18/20) |
| **confident-wrong** | **1** |
| **abstention correctness** (abstain tasks ended as the rubric requires) | **0.929** |
| abstention precision (abstentions that were on abstain tasks) | 1.000 |
| unnecessary abstentions | 0 |
| task success (accepted + correct abstentions) / tasks | 0.912 |
| errors | 0 |
| latency p50 / p95 / max (s, per task) | 2.754 / 4.968 / 8.604 |
| model calls / tokens / USD | 0 / 0 / 0.0 |
| cost per accepted output: model USD / CPU s / wall s / infrastructure | 0.000 / 3.571 / 4.965 / unpriced |

| Domain | tasks | accepted | accepted-output rate | confident-wrong | abstention correctness | unnecessary abstentions | errors | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|---|
| itsm | 6 | 2 | 0.500 | 0 | 1.000 | 0 | 0 | 3.928 | 7.950 |
| sales | 6 | 4 | 1.000 | 0 | 1.000 | 0 | 0 | 4.122 | 4.413 |
| finance | 6 | 4 | 1.000 | 0 | 1.000 | 0 | 0 | 4.326 | 4.390 |
| engineering | 8 | 5 | 1.000 | 0 | 1.000 | 0 | 0 | 0.575 | 0.731 |
| ml | 8 | 3 | 1.000 | 1 | 0.800 | 0 | 0 | 0.739 | 2.522 |

### Every task

| Task | family | expect | status | abstained as | seconds | why |
|---|---|---|---|---|---|---|
| HO-ITSM-01 | analysis | deliver | accepted | — | 8.604 | every planted effect verified, no false finding |
| HO-ITSM-02 | analysis | deliver | accepted | — | 5.987 | every planted effect verified, no false finding |
| HO-ITSM-03 | analysis | deliver | incomplete | — | 3.873 | missed planted effect(s): fulfilment_by_site |
| HO-ITSM-04 | analysis | deliver | incomplete | — | 3.982 | missed planted effect(s): fulfilment_by_site |
| HO-ITSM-05 | analysis | abstain (no_finding) | correct_abstention | no_finding | 3.445 | no false finding (1 true non-planted finding(s) reported) |
| HO-ITSM-06 | analysis | abstain (no_finding) | correct_abstention | no_finding | 2.72 | no false finding |
| HO-SALES-01 | analysis | deliver | accepted | — | 4.394 | every planted effect verified, no false finding |
| HO-SALES-02 | analysis | deliver | accepted | — | 4.318 | every planted effect verified, no false finding |
| HO-SALES-03 | analysis | deliver | accepted | — | 3.926 | every planted effect verified, no false finding |
| HO-SALES-04 | analysis | deliver | accepted | — | 4.42 | every planted effect verified, no false finding |
| HO-SALES-05 | analysis | abstain (no_finding) | correct_abstention | no_finding | 3.831 | no false finding |
| HO-SALES-06 | analysis | abstain (no_finding) | correct_abstention | no_finding | 2.788 | no false finding |
| HO-FIN-01 | analysis | deliver | accepted | — | 4.405 | every planted effect verified, no false finding |
| HO-FIN-02 | analysis | deliver | accepted | — | 4.344 | every planted effect verified, no false finding |
| HO-FIN-03 | analysis | deliver | accepted | — | 4.309 | every planted effect verified, no false finding |
| HO-FIN-04 | analysis | deliver | accepted | — | 4.343 | every planted effect verified, no false finding |
| HO-FIN-05 | analysis | abstain (no_finding) | correct_abstention | no_finding | 3.318 | no false finding |
| HO-FIN-06 | analysis | abstain (no_finding) | correct_abstention | no_finding | 3.275 | no false finding |
| HO-DE-01 | engineering | deliver | accepted | — | 0.648 | output equals the reference |
| HO-DE-02 | engineering | deliver | accepted | — | 0.74 | output equals the reference |
| HO-DE-03 | engineering | deliver | accepted | — | 0.47 | output equals the reference |
| HO-DE-04 | engineering | abstain (blocked) | correct_abstention | blocked | 0.416 | fail gate range(weight_kg) |
| HO-DE-05 | engineering | abstain (refused) | correct_abstention | refused | 0.281 | validation: node delivered (filter): column shipment_status does not exist here (have: carrier_code, loaded_at, ship_date, shipment_no, shipment_state, weight_kg) |
| HO-DE-06 | engineering | deliver | accepted | — | 0.715 | output equals the reference |
| HO-DE-07 | engineering | abstain (refused) | correct_abstention | refused | 0.502 | InvalidInput: join priced is declared many_to_one but the data is many_to_many (left keys 3/4659 rows, right keys 3/4 rows, row multiplication 1.3677); the recipe was not run |
| HO-DE-08 | engineering | deliver | accepted | — | 0.698 | output equals the reference |
| HO-ML-01 | ml | deliver | accepted | — | 2.267 | improved over the baseline on identical splits |
| HO-ML-02 | ml | deliver | accepted | — | 1.193 | improved over the baseline on identical splits |
| HO-ML-03 | ml | deliver | accepted | — | 1.513 | improved over the baseline on identical splits |
| HO-ML-04 | ml | abstain (no_finding) | confident_wrong | — | 2.66 | `improved` where the rubric requires no_finding (the target is a coin flip: no model beats the baseline) |
| HO-ML-05 | ml | abstain (refused) | correct_abstention | refused | 0.285 | InvalidInput: the experiment was refused before training: target_derived_features: these features alone reproduce the target (derived from it?): work_order_raised (single threshold 1.0) |
| HO-ML-06 | ml | abstain (refused) | correct_abstention | refused | 0.241 | InvalidInput: the experiment was refused before training: post_cutoff_timestamps: feature values observed after the prediction cutoff: failure_date (132 rows); post_outcome_timestamps: feature values observed at or after |
| HO-ML-07 | ml | abstain (refused) | correct_abstention | refused | 0.253 | InvalidInput: the experiment was refused before training: group_aware_split: entity ['asset_tag'] repeats across rows; a random split would put one entity in both training and holdout (declare group_keys) |
| HO-ML-08 | ml | abstain (refused) | correct_abstention | refused | 0.214 | InvalidInput: the experiment was refused before training: minimum_rows: insufficient data: 18 usable rows, need >= 40 (insufficient evidence is a valid outcome) |

Failures and non-accepted outcomes (3): HO-ITSM-03 incomplete; HO-ITSM-04 incomplete; HO-ML-04 confident_wrong.
Abstentions: HO-ITSM-05 no_finding (correct_abstention); HO-ITSM-06 no_finding (correct_abstention); HO-SALES-05 no_finding (correct_abstention); HO-SALES-06 no_finding (correct_abstention); HO-FIN-05 no_finding (correct_abstention); HO-FIN-06 no_finding (correct_abstention); HO-DE-04 blocked (correct_abstention); HO-DE-05 refused (correct_abstention); HO-DE-07 refused (correct_abstention); HO-ML-05 refused (correct_abstention); HO-ML-06 refused (correct_abstention); HO-ML-07 refused (correct_abstention); HO-ML-08 refused (correct_abstention).

Status counts: accepted 18, correct_abstention 13, confident_wrong 1, incomplete 2, unnecessary_abstention 0, wrong_abstention 0, error 0.

## Scope and limits

* Coverage: 34 tasks against the evaluation plan's first-release target of at least 60 (20 analyst, 15 engineering, 15 ML, 10 unsupported). This version covers ITSM, sales and finance analysis, one engineering family (recipes: dedupe, late batch, joins, gates) and one ML family (classification, regression, forecast, leakage and small-data refusals). It does not cover transfer renaming, governance, UX/recovery or retail/logistics/SaaS-ops domains, and it cannot claim the full suite's capability.
* Synthetic data with planted effects gives known truth; it is not a claim about production data. Planted effects are sized above the materiality thresholds.
* The component tier has no model in its path; the platform tier ran with no provider key, so it measures the rule path (`off`). A live-model run is a separate dated report.
* Infrastructure cost is reported as CPU and wall seconds; it is priced only when `--cpu-usd-per-hour` is given.
* The paired practitioner baseline (`docs/60-delivery/06-practitioner-baseline-protocol.md`) was **not run**: it needs qualified human analysts and a blinded scorer. No human-effort or time-saving claim follows from this report.

## Reading the result (added by hand, 2026-09-27)

**Freeze.** The corpus, generators, rubrics and lock were committed in `e682b5e` before this report's runs.
During development the corpus self-check (`corpus_problems`) caught one rubric slip before any analysis task
was scored (HO-ITSM-05 listed `risk_tier` as a null column although approvals follow it); nulls are not used
for scoring. No generator, seed or rubric was changed after a score was seen. The harness was fixed twice
after the first platform attempt, neither time to change a verdict's rule: ML tasks now run in a workspace
whose owner enabled training (the first attempt was refused by the disabled-capability check, which the
runner now scores as a separate `denied` abstention that never counts as a correct data refusal).

**Confident-wrong on the platform tier: HO-ML-04.** The target is a coin flip (`noise_flag`, independent of
every feature). Through the platform (CSV source, staging, gateway snapshot, published `ml_spec`,
`services.ml.start_experiment`) the experiment ended `improved`: gradient boosting ROC AUC 0.605 vs baseline
0.500 on a holdout of about 98 rows, gain 0.105 with a 95% interval of 0.012..0.221, against the spec's
default `min_improvement` of 0. The same verdict came back on three consecutive platform runs (the split is
deterministic for a given snapshot). The component tier on the same rows gave `no_improvement`; its snapshot
differs (all columns vs the referenced columns, different row order), so it drew a different split. This is
a real false promotion signal on a null, not a harness artifact: with no practical minimum and a one-sided
95% bound, a null case passes by chance a few percent of the time, and one of the corpus's two ML
null-type cases did. Suggested follow-up (a tracker row, not done here): a non-zero default
`min_improvement` per metric and/or confirmation on a second split or repeated holdout before `improved`.

**Split sensitivity also shows on HO-ML-03** (daily calls forecast): `no_improvement` on the component tier,
`improved` on the platform tier. The rubric expects an improvement (a weekly cycle on a drift), so this
changes the accepted count, not the confident-wrong count, but it is the same effect: a verdict that flips
with the snapshot's row order is fragile.

**Missed planted effect: HO-ITSM-03/04 `fulfilment_by_site`.** Fulfilment time is a duration between two
timestamps (`submitted_ts -> fulfilled_ts`). With unfamiliar column names no domain pack applies, and the
core role playbook proposes measures and flags by segment and a volume trend, not durations, so the
hypothesis was never tested. Both tiers agree. This is a transfer gap (spec v3 §10, evaluation plan §2
"Transfer"), not a statistics error: nothing false was claimed.

**Per-domain reading.** Sales, finance and engineering: every deliver task accepted and every abstain task
correct on both tiers. ITSM: 2/4 accepted (the duration gap). ML: component 2/3 accepted with 0 wrong;
platform 3/3 accepted with 1 confident-wrong.

**Latency and cost.** Platform tasks take 2.8 s median and 5.0 s p95, including upload, discovery and
staging of the task's data. No model was called (no provider key), so model spend per accepted output is
0 by measurement; infrastructure is reported as CPU and wall seconds and is unpriced.

**Not done here.** The paired practitioner baseline was not run (it needs human analysts and a blinded
scorer; protocol in `docs/60-delivery/06-practitioner-baseline-protocol.md`). A live-model run was not made
(no provider key). The corpus has 34 tasks, not the plan's 60.
