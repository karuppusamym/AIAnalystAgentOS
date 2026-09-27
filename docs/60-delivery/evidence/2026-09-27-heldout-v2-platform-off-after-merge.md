# Held-out evaluation (P4-08) — 2026-09-27

Generated 2026-09-27 17:25 UTC by the held-out runner (`evaluation/heldout`) at `17e0d19` (Python 3.11.15). Corpus `evaluation/heldout/corpus.yaml` version 2 (frozen 2026-09-27, sha256 `4718603cc12b`). Models: **off** (no provider key: every model purpose takes its rule path).

Every task ends in one status (`evaluation/heldout/runner.py`): accepted, correct_abstention, confident_wrong, incomplete, unnecessary_abstention, wrong_abstention or error. Rubrics were sealed before the first scored run; the corpus lock (`corpus.lock.json`) hashes every task's data, reference and rubric.

Proposed thresholds (non-blocking `heldout` gate, owner to confirm): accepted_output_rate ≥ 0.9, confident_wrong ≤ 0, abstention_recall ≥ 1.0, errors ≤ 0.

## Tier: platform

Corpus lock: matches. Wall time 195.9 s.

| Measure | Value |
|---|---|
| tasks (deliver / abstain) | 75 (43 / 32) |
| **accepted-output rate** (accepted / deliver tasks) | **0.907** (39/43) |
| **confident-wrong** | **0** |
| **abstention correctness** (abstain tasks ended as the rubric requires) | **1.000** |
| abstention precision (abstentions that were on abstain tasks) | 0.970 |
| unnecessary abstentions | 1 |
| task success (accepted + correct abstentions) / tasks | 0.947 |
| errors | 0 |
| latency p50 / p95 / max (s, per task) | 1.824 / 5.630 / 11.115 |
| model calls / tokens / USD | 0 / 0 / 0.0 |
| cost per accepted output: model USD / CPU s / wall s / infrastructure | 0.000 / 3.586 / 4.985 / unpriced |

| Domain | tasks | accepted | accepted-output rate | confident-wrong | abstention correctness | unnecessary abstentions | errors | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|---|
| itsm | 6 | 2 | 0.500 | 0 | 1.000 | 0 | 0 | 5.742 | 10.116 |
| sales | 6 | 4 | 1.000 | 0 | 1.000 | 0 | 0 | 5.261 | 5.422 |
| finance | 6 | 4 | 1.000 | 0 | 1.000 | 0 | 0 | 4.859 | 5.362 |
| engineering | 15 | 9 | 1.000 | 0 | 1.000 | 0 | 0 | 0.725 | 0.993 |
| ml | 16 | 7 | 1.000 | 0 | 1.000 | 0 | 0 | 1.499 | 2.453 |
| retail | 3 | 2 | 1.000 | 0 | 1.000 | 0 | 0 | 4.475 | 4.876 |
| logistics | 3 | 2 | 1.000 | 0 | 1.000 | 0 | 0 | 5.372 | 5.468 |
| saas_ops | 3 | 2 | 1.000 | 0 | 1.000 | 0 | 0 | 4.847 | 4.985 |
| transfer | 4 | 1 | 0.333 | 0 | 1.000 | 1 | 0 | 4.708 | 5.827 |
| governance | 8 | 2 | 1.000 | 0 | 1.000 | 0 | 0 | 0.166 | 0.463 |
| recovery | 5 | 4 | 1.000 | 0 | 1.000 | 0 | 0 | 0.851 | 1.734 |

### Every task

| Task | family | expect | status | abstained as | seconds | why |
|---|---|---|---|---|---|---|
| HO-ITSM-01 | analysis | deliver | accepted | — | 11.115 | every planted effect verified, no false finding |
| HO-ITSM-02 | analysis | deliver | accepted | — | 7.117 | every planted effect verified, no false finding |
| HO-ITSM-03 | analysis | deliver | incomplete | — | 4.965 | missed planted effect(s): fulfilment_by_site |
| HO-ITSM-04 | analysis | deliver | incomplete | — | 6.518 | missed planted effect(s): fulfilment_by_site |
| HO-ITSM-05 | analysis | abstain (no_finding) | correct_abstention | no_finding | 4.801 | no false finding (1 true non-planted finding(s) reported) |
| HO-ITSM-06 | analysis | abstain (no_finding) | correct_abstention | no_finding | 3.828 | no false finding |
| HO-SALES-01 | analysis | deliver | accepted | — | 5.424 | every planted effect verified, no false finding |
| HO-SALES-02 | analysis | deliver | accepted | — | 5.381 | every planted effect verified, no false finding |
| HO-SALES-03 | analysis | deliver | accepted | — | 5.092 | every planted effect verified, no false finding |
| HO-SALES-04 | analysis | deliver | accepted | — | 5.416 | every planted effect verified, no false finding |
| HO-SALES-05 | analysis | abstain (no_finding) | correct_abstention | no_finding | 5.141 | no false finding |
| HO-SALES-06 | analysis | abstain (no_finding) | correct_abstention | no_finding | 3.417 | no false finding |
| HO-FIN-01 | analysis | deliver | accepted | — | 4.852 | every planted effect verified, no false finding |
| HO-FIN-02 | analysis | deliver | accepted | — | 4.919 | every planted effect verified, no false finding |
| HO-FIN-03 | analysis | deliver | accepted | — | 4.866 | every planted effect verified, no false finding |
| HO-FIN-04 | analysis | deliver | accepted | — | 5.509 | every planted effect verified, no false finding |
| HO-FIN-05 | analysis | abstain (no_finding) | correct_abstention | no_finding | 3.969 | no false finding |
| HO-FIN-06 | analysis | abstain (no_finding) | correct_abstention | no_finding | 3.873 | no false finding |
| HO-DE-01 | engineering | deliver | accepted | — | 0.986 | output equals the reference |
| HO-DE-02 | engineering | deliver | accepted | — | 0.725 | output equals the reference |
| HO-DE-03 | engineering | deliver | accepted | — | 0.536 | output equals the reference |
| HO-DE-04 | engineering | abstain (blocked) | correct_abstention | blocked | 0.596 | fail gate range(weight_kg) |
| HO-DE-05 | engineering | abstain (refused) | correct_abstention | refused | 0.319 | validation: node delivered (filter): column shipment_status does not exist here (have: carrier_code, loaded_at, ship_date, shipment_no, shipment_state, weight_kg) |
| HO-DE-06 | engineering | deliver | accepted | — | 0.853 | output equals the reference |
| HO-DE-07 | engineering | abstain (refused) | correct_abstention | refused | 0.575 | InvalidInput: join priced is declared many_to_one but the data is many_to_many (left keys 3/4659 rows, right keys 3/4 rows, row multiplication 1.3677); the recipe was not run |
| HO-DE-08 | engineering | deliver | accepted | — | 0.798 | output equals the reference |
| HO-ML-01 | ml | deliver | accepted | — | 1.915 | improved over the baseline on identical splits |
| HO-ML-02 | ml | deliver | accepted | — | 1.316 | improved over the baseline on identical splits |
| HO-ML-03 | ml | deliver | accepted | — | 1.653 | improved over the baseline on identical splits |
| HO-ML-04 | ml | abstain (no_finding) | correct_abstention | no_finding | 2.429 | verdict no_improvement |
| HO-ML-05 | ml | abstain (refused) | correct_abstention | refused | 0.29 | InvalidInput: the experiment was refused before training: target_derived_features: these features alone reproduce the target (derived from it?): work_order_raised (single threshold 1.0) |
| HO-ML-06 | ml | abstain (refused) | correct_abstention | refused | 0.305 | InvalidInput: the experiment was refused before training: post_cutoff_timestamps: feature values observed after the prediction cutoff: failure_date (132 rows); post_outcome_timestamps: feature values observed at or after |
| HO-ML-07 | ml | abstain (refused) | correct_abstention | refused | 0.368 | InvalidInput: the experiment was refused before training: group_aware_split: entity ['asset_tag'] repeats across rows; a random split would put one entity in both training and holdout (declare group_keys) |
| HO-ML-08 | ml | abstain (refused) | correct_abstention | refused | 0.354 | InvalidInput: the experiment was refused before training: minimum_rows: insufficient data: 18 usable rows, need >= 40 (insufficient evidence is a valid outcome) |
| HO-RET-01 | analysis | deliver | accepted | — | 4.921 | every planted effect verified, no false finding |
| HO-RET-02 | analysis | deliver | accepted | — | 4.475 | every planted effect verified, no false finding |
| HO-RET-03 | analysis | abstain (no_finding) | correct_abstention | no_finding | 3.338 | no false finding |
| HO-LOG-01 | analysis | deliver | accepted | — | 5.372 | every planted effect verified, no false finding |
| HO-LOG-02 | analysis | deliver | accepted | — | 5.479 | every planted effect verified, no false finding |
| HO-LOG-03 | analysis | abstain (no_finding) | correct_abstention | no_finding | 3.852 | no false finding |
| HO-SOPS-01 | analysis | deliver | accepted | — | 5.0 | every planted effect verified, no false finding |
| HO-SOPS-02 | analysis | deliver | accepted | — | 4.847 | every planted effect verified, no false finding |
| HO-SOPS-03 | analysis | abstain (no_finding) | correct_abstention | no_finding | 3.843 | no false finding |
| HO-XFER-01 | analysis | deliver | unnecessary_abstention | no_finding | 3.893 | no verified finding |
| HO-XFER-02 | analysis | deliver | incomplete | — | 5.523 | missed planted effect(s): win_by_source |
| HO-XFER-03 | analysis | deliver | accepted | — | 5.881 | every planted effect verified, no false finding |
| HO-XFER-04 | analysis | abstain (no_finding) | correct_abstention | no_finding | 3.82 | no false finding |
| HO-DE-09 | engineering | deliver | accepted | — | 0.807 | output equals the reference |
| HO-DE-10 | engineering | deliver | accepted | — | 0.9 | output equals the reference |
| HO-DE-11 | engineering | abstain (blocked) | correct_abstention | blocked | 0.61 | fail gate accepted_values(tender_type) |
| HO-DE-12 | engineering | abstain (blocked) | correct_abstention | blocked | 0.565 | fail gate key_unique(receipt_no,line_no) |
| HO-DE-13 | engineering | deliver | accepted | — | 0.859 | output equals the reference |
| HO-DE-14 | engineering | deliver | accepted | — | 1.011 | output equals the reference |
| HO-DE-15 | engineering | abstain (refused) | correct_abstention | refused | 0.622 | InvalidInput: join with_edition is declared many_to_one but the data is many_to_many (left keys 90/3629 rows, right keys 90/91 rows, row multiplication 1.011); the recipe was not run |
| HO-ML-09 | ml | deliver | accepted | — | 2.014 | improved over the baseline on identical splits |
| HO-ML-10 | ml | deliver | accepted | — | 2.3 | improved over the baseline on identical splits |
| HO-ML-11 | ml | abstain (refused) | correct_abstention | refused | 0.368 | InvalidInput: the experiment was refused before training: feature_availability: only known after the outcome: exit_survey_score |
| HO-ML-12 | ml | abstain (no_finding) | correct_abstention | no_finding | 2.526 | verdict no_improvement |
| HO-ML-13 | ml | deliver | accepted | — | 1.529 | improved over the baseline on identical splits |
| HO-ML-14 | ml | abstain (no_finding) | correct_abstention | no_finding | 1.469 | verdict no_improvement |
| HO-ML-15 | ml | deliver | accepted | — | 2.043 | improved over the baseline on identical splits |
| HO-ML-16 | ml | abstain (refused) | correct_abstention | refused | 0.317 | InvalidInput: the experiment was refused before training: minimum_rows: insufficient data: 24 usable rows, need >= 46 (insufficient evidence is a valid outcome) |
| HO-GOV-01 | governance | abstain (denied) | correct_abstention | denied | 0.539 | SQLRejected: Table src_src_ccf9b26eea25.payroll_lines is not in the authorized scope. Use one of: src_src_5070c5f09eeb.loyalty_members. |
| HO-GOV-02 | governance | abstain (denied) | correct_abstention | denied | 0.322 | SQLRejected: Column src_src_715e29669a05.loyalty_members.email_address is restricted by policy and cannot be referenced. Remove it from the query. |
| HO-GOV-03 | governance | abstain (denied) | correct_abstention | denied | 0.283 | validation: node valued (derive) column points_eur: expression does not parse: Invalid expression / Unexpected token. Line 1, Col: 29.; node per_tier (aggregate) measure points_eur: column points_eur does not exist here |
| HO-GOV-04 | governance | abstain (denied) | correct_abstention | denied | 0.049 | ApprovalRequired: approval expired |
| HO-GOV-05 | governance | abstain (denied) | correct_abstention | denied | 0.036 | ApprovalRequired: payload changed after approval; a new approval is required |
| HO-GOV-06 | governance | abstain (denied) | correct_abstention | denied | 0.031 | PolicyDenied: approver no longer holds approval rights in this workspace |
| HO-GOV-07 | governance | deliver | accepted | — | 0.287 | the answer equals the reference |
| HO-GOV-08 | governance | deliver | accepted | — | 0.037 | executed once; the second attempt was refused (ApprovalRequired: approval is executed) |
| HO-REC-01 | recovery | deliver | accepted | — | 1.824 | failed without output (Forbidden: asset src_src_4b7f837c5938.stores (node stores) is not in the authorized scope); rerun equals the reference twice |
| HO-REC-02 | recovery | deliver | accepted | — | 0.851 | the refusal names store_format; the corrected recipe equals the reference |
| HO-REC-03 | recovery | deliver | accepted | — | 0.334 | the failure named its cause and was recorded; after reconnecting the table was discovered |
| HO-REC-04 | recovery | deliver | accepted | — | 0.029 | the stale save was refused; after reloading both edits survive |
| HO-REC-05 | recovery | abstain (refused) | correct_abstention | refused | 1.372 | Conflict: src_src_c00d95be7932.till_lines changed since the recipe pinned snapshot 8ecd0842a887 (now 30158aeb7dc6) |

Failures and non-accepted outcomes (4): HO-ITSM-03 incomplete; HO-ITSM-04 incomplete; HO-XFER-01 unnecessary_abstention; HO-XFER-02 incomplete.
Abstentions: HO-ITSM-05 no_finding (correct_abstention); HO-ITSM-06 no_finding (correct_abstention); HO-SALES-05 no_finding (correct_abstention); HO-SALES-06 no_finding (correct_abstention); HO-FIN-05 no_finding (correct_abstention); HO-FIN-06 no_finding (correct_abstention); HO-DE-04 blocked (correct_abstention); HO-DE-05 refused (correct_abstention); HO-DE-07 refused (correct_abstention); HO-ML-04 no_finding (correct_abstention); HO-ML-05 refused (correct_abstention); HO-ML-06 refused (correct_abstention); HO-ML-07 refused (correct_abstention); HO-ML-08 refused (correct_abstention); HO-RET-03 no_finding (correct_abstention); HO-LOG-03 no_finding (correct_abstention); HO-SOPS-03 no_finding (correct_abstention); HO-XFER-01 no_finding (unnecessary_abstention); HO-XFER-04 no_finding (correct_abstention); HO-DE-11 blocked (correct_abstention); HO-DE-12 blocked (correct_abstention); HO-DE-15 refused (correct_abstention); HO-ML-11 refused (correct_abstention); HO-ML-12 no_finding (correct_abstention); HO-ML-14 no_finding (correct_abstention); HO-ML-16 refused (correct_abstention); HO-GOV-01 denied (correct_abstention); HO-GOV-02 denied (correct_abstention); HO-GOV-03 denied (correct_abstention); HO-GOV-04 denied (correct_abstention); HO-GOV-05 denied (correct_abstention); HO-GOV-06 denied (correct_abstention); HO-REC-05 refused (correct_abstention).

Status counts: accepted 39, correct_abstention 32, confident_wrong 0, incomplete 3, unnecessary_abstention 1, wrong_abstention 0, error 0.

## Scope and limits

* Coverage: 75 tasks (31 analysis, 15 engineering, 16 ml, 8 governance, 5 recovery; 32 expect an abstention) against the evaluation plan's first-release target of at least 60 (20 analyst, 15 engineering, 15 ML, 10 unsupported). Analysis domains: finance, itsm, logistics, retail, saas_ops, sales, transfer. The plan's 10 tasks per domain are not reached for any single domain, so per-domain rates are small-sample; the suite cannot claim capability beyond these tasks.
* Synthetic data with planted effects gives known truth; it is not a claim about production data. Planted effects are sized above the materiality thresholds.
* The component tier has no model in its path; the platform tier ran with no provider key, so it measures the rule path (`off`). A live-model run is a separate dated report.
* Infrastructure cost is reported as CPU and wall seconds; it is priced only when `--cpu-usd-per-hour` is given.
* The paired practitioner baseline (`docs/60-delivery/06-practitioner-baseline-protocol.md`) was **not run**: it needs qualified human analysts and a blinded scorer. No human-effort or time-saving claim follows from this report.
