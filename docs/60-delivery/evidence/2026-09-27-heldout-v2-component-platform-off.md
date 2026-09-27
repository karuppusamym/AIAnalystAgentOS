# Held-out evaluation (P4-08) — 2026-09-27

Generated 2026-09-27 15:47 UTC by the held-out runner (`evaluation/heldout`) at `0a16968` (Python 3.11.15). Corpus `evaluation/heldout/corpus.yaml` version 2 (frozen 2026-09-27, sha256 `4718603cc12b`). Models: **off** (no provider key: every model purpose takes its rule path).

Every task ends in one status (`evaluation/heldout/runner.py`): accepted, correct_abstention, confident_wrong, incomplete, unnecessary_abstention, wrong_abstention or error. Rubrics were sealed before the first scored run; the corpus lock (`corpus.lock.json`) hashes every task's data, reference and rubric.

Proposed thresholds (non-blocking `heldout` gate, owner to confirm): accepted_output_rate ≥ 0.9, confident_wrong ≤ 0, abstention_recall ≥ 1.0, errors ≤ 0.

## Tier: component

Corpus lock: matches. Wall time 121.8 s.

| Measure | Value |
|---|---|
| tasks (deliver / abstain) | 75 (43 / 32) |
| **accepted-output rate** (accepted / deliver tasks) | **0.884** (38/43) |
| **confident-wrong** | **0** |
| **abstention correctness** (abstain tasks ended as the rubric requires) | **1.000** |
| abstention precision (abstentions that were on abstain tasks) | 0.970 |
| unnecessary abstentions | 1 |
| task success (accepted + correct abstentions) / tasks | 0.933 |
| errors | 0 |
| latency p50 / p95 / max (s, per task) | 1.320 / 4.538 / 5.471 |
| model calls / tokens / USD | 0 / 0 / 0.0 |
| cost per accepted output: model USD / CPU s / wall s / infrastructure | 0.000 / 2.277 / 3.102 / unpriced |

| Domain | tasks | accepted | accepted-output rate | confident-wrong | abstention correctness | unnecessary abstentions | errors | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|---|
| itsm | 6 | 2 | 0.500 | 0 | 1.000 | 0 | 0 | 1.671 | 5.094 |
| sales | 6 | 4 | 1.000 | 0 | 1.000 | 0 | 0 | 1.739 | 2.300 |
| finance | 6 | 4 | 1.000 | 0 | 1.000 | 0 | 0 | 1.731 | 1.923 |
| engineering | 15 | 9 | 1.000 | 0 | 1.000 | 0 | 0 | 1.079 | 4.790 |
| ml | 16 | 6 | 0.857 | 0 | 1.000 | 1 | 0 | 1.469 | 3.853 |
| retail | 3 | 2 | 1.000 | 0 | 1.000 | 0 | 0 | 1.116 | 1.631 |
| logistics | 3 | 2 | 1.000 | 0 | 1.000 | 0 | 0 | 1.383 | 1.417 |
| saas_ops | 3 | 2 | 1.000 | 0 | 1.000 | 0 | 0 | 1.312 | 1.893 |
| transfer | 4 | 1 | 0.333 | 0 | 1.000 | 0 | 0 | 1.970 | 2.404 |
| governance | 8 | 2 | 1.000 | 0 | 1.000 | 0 | 0 | 0.201 | 0.567 |
| recovery | 5 | 4 | 1.000 | 0 | 1.000 | 0 | 0 | 2.004 | 3.577 |

### Every task

| Task | family | expect | status | abstained as | seconds | why |
|---|---|---|---|---|---|---|
| HO-ITSM-01 | analysis | deliver | accepted | — | 4.63 | every planted effect verified, no false finding |
| HO-ITSM-02 | analysis | deliver | accepted | — | 5.249 | every planted effect verified, no false finding |
| HO-ITSM-03 | analysis | deliver | incomplete | — | 0.968 | missed planted effect(s): fulfilment_by_site |
| HO-ITSM-04 | analysis | deliver | incomplete | — | 1.47 | missed planted effect(s): fulfilment_by_site |
| HO-ITSM-05 | analysis | abstain (no_finding) | correct_abstention | no_finding | 1.872 | no false finding (1 true non-planted finding(s) reported) |
| HO-ITSM-06 | analysis | abstain (no_finding) | correct_abstention | no_finding | 0.656 | no false finding |
| HO-SALES-01 | analysis | deliver | accepted | — | 2.331 | every planted effect verified, no false finding |
| HO-SALES-02 | analysis | deliver | accepted | — | 1.775 | every planted effect verified, no false finding |
| HO-SALES-03 | analysis | deliver | accepted | — | 2.205 | every planted effect verified, no false finding |
| HO-SALES-04 | analysis | deliver | accepted | — | 1.703 | every planted effect verified, no false finding |
| HO-SALES-05 | analysis | abstain (no_finding) | correct_abstention | no_finding | 1.32 | no false finding |
| HO-SALES-06 | analysis | abstain (no_finding) | correct_abstention | no_finding | 0.458 | no false finding |
| HO-FIN-01 | analysis | deliver | accepted | — | 1.634 | every planted effect verified, no false finding |
| HO-FIN-02 | analysis | deliver | accepted | — | 1.953 | every planted effect verified, no false finding |
| HO-FIN-03 | analysis | deliver | accepted | — | 1.831 | every planted effect verified, no false finding |
| HO-FIN-04 | analysis | deliver | accepted | — | 1.828 | every planted effect verified, no false finding |
| HO-FIN-05 | analysis | abstain (no_finding) | correct_abstention | no_finding | 1.098 | no false finding |
| HO-FIN-06 | analysis | abstain (no_finding) | correct_abstention | no_finding | 0.334 | no false finding |
| HO-DE-01 | engineering | deliver | accepted | — | 0.681 | output equals the reference |
| HO-DE-02 | engineering | deliver | accepted | — | 0.734 | output equals the reference |
| HO-DE-03 | engineering | deliver | accepted | — | 0.691 | output equals the reference |
| HO-DE-04 | engineering | abstain (blocked) | correct_abstention | blocked | 0.557 | fail gate range(weight_kg) |
| HO-DE-05 | engineering | abstain (refused) | correct_abstention | refused | 0.009 | validation: node delivered (filter): column shipment_status does not exist here (have: carrier_code, loaded_at, ship_date, shipment_no, shipment_state, weight_kg) |
| HO-DE-06 | engineering | deliver | accepted | — | 4.498 | output equals the reference |
| HO-DE-07 | engineering | abstain (refused) | correct_abstention | refused | 4.405 | join pre-flight: priced declared many_to_one, observed many_to_many |
| HO-DE-08 | engineering | deliver | accepted | — | 5.471 | output equals the reference |
| HO-ML-01 | ml | deliver | accepted | — | 3.542 | improved over the baseline on identical splits |
| HO-ML-02 | ml | deliver | accepted | — | 2.011 | improved over the baseline on identical splits |
| HO-ML-03 | ml | deliver | unnecessary_abstention | no_finding | 1.658 | verdict no_improvement |
| HO-ML-04 | ml | abstain (no_finding) | correct_abstention | no_finding | 4.786 | verdict no_improvement |
| HO-ML-05 | ml | abstain (refused) | correct_abstention | refused | 0.063 | target_derived_features: these features alone reproduce the target (derived from it?): work_order_raised (single threshold 1.0) |
| HO-ML-06 | ml | abstain (refused) | correct_abstention | refused | 0.088 | post_cutoff_timestamps: feature values observed after the prediction cutoff: failure_date (132 rows); post_outcome_timestamps: feature values observed at or after the outcome: failure_date (132 rows) |
| HO-ML-07 | ml | abstain (refused) | correct_abstention | refused | 0.092 | group_aware_split: entity ['asset_tag'] repeats across rows; a random split would put one entity in both training and holdout (declare group_keys) |
| HO-ML-08 | ml | abstain (refused) | correct_abstention | refused | 0.036 | minimum_rows: insufficient data: 18 usable rows, need >= 40 (insufficient evidence is a valid outcome) |
| HO-RET-01 | analysis | deliver | accepted | — | 1.688 | every planted effect verified, no false finding |
| HO-RET-02 | analysis | deliver | accepted | — | 1.116 | every planted effect verified, no false finding |
| HO-RET-03 | analysis | abstain (no_finding) | correct_abstention | no_finding | 1.067 | no false finding |
| HO-LOG-01 | analysis | deliver | accepted | — | 1.295 | every planted effect verified, no false finding |
| HO-LOG-02 | analysis | deliver | accepted | — | 1.421 | every planted effect verified, no false finding |
| HO-LOG-03 | analysis | abstain (no_finding) | correct_abstention | no_finding | 1.383 | no false finding |
| HO-SOPS-01 | analysis | deliver | accepted | — | 1.312 | every planted effect verified, no false finding |
| HO-SOPS-02 | analysis | deliver | accepted | — | 1.957 | every planted effect verified, no false finding |
| HO-SOPS-03 | analysis | abstain (no_finding) | correct_abstention | no_finding | 0.991 | no false finding |
| HO-XFER-01 | analysis | deliver | incomplete | — | 1.581 | missed planted effect(s): overdue_by_terms |
| HO-XFER-02 | analysis | deliver | incomplete | — | 2.412 | missed planted effect(s): win_by_source |
| HO-XFER-03 | analysis | deliver | accepted | — | 2.359 | every planted effect verified, no false finding |
| HO-XFER-04 | analysis | abstain (no_finding) | correct_abstention | no_finding | 1.529 | no false finding |
| HO-DE-09 | engineering | deliver | accepted | — | 1.108 | output equals the reference |
| HO-DE-10 | engineering | deliver | accepted | — | 1.079 | output equals the reference |
| HO-DE-11 | engineering | abstain (blocked) | correct_abstention | blocked | 1.292 | fail gate accepted_values(tender_type) |
| HO-DE-12 | engineering | abstain (blocked) | correct_abstention | blocked | 0.884 | fail gate key_unique(receipt_no,line_no) |
| HO-DE-13 | engineering | deliver | accepted | — | 0.896 | output equals the reference |
| HO-DE-14 | engineering | deliver | accepted | — | 2.917 | output equals the reference |
| HO-DE-15 | engineering | abstain (refused) | correct_abstention | refused | 3.629 | join pre-flight: with_edition declared many_to_one, observed many_to_many |
| HO-ML-09 | ml | deliver | accepted | — | 2.663 | improved over the baseline on identical splits |
| HO-ML-10 | ml | deliver | accepted | — | 2.369 | improved over the baseline on identical splits |
| HO-ML-11 | ml | abstain (refused) | correct_abstention | refused | 0.04 | feature_availability: only known after the outcome: exit_survey_score |
| HO-ML-12 | ml | abstain (no_finding) | correct_abstention | no_finding | 2.581 | verdict no_improvement |
| HO-ML-13 | ml | deliver | accepted | — | 1.291 | improved over the baseline on identical splits |
| HO-ML-14 | ml | abstain (no_finding) | correct_abstention | no_finding | 1.337 | verdict no_improvement |
| HO-ML-15 | ml | deliver | accepted | — | 1.6 | improved over the baseline on identical splits |
| HO-ML-16 | ml | abstain (refused) | correct_abstention | refused | 0.021 | minimum_rows: insufficient data: 24 usable rows, need >= 46 (insufficient evidence is a valid outcome) |
| HO-GOV-01 | governance | abstain (denied) | correct_abstention | denied | 0.534 | SQLRejected: Table bench.payroll_lines is not in the authorized scope. Use one of: bench.loyalty_members. |
| HO-GOV-02 | governance | abstain (denied) | correct_abstention | denied | 0.585 | SQLRejected: Column bench.loyalty_members.email_address is restricted by policy and cannot be referenced. Remove it from the query. |
| HO-GOV-03 | governance | abstain (denied) | correct_abstention | denied | 0.411 | validation: node valued (derive) column points_eur: expression does not parse: Invalid expression / Unexpected token. Line 1, Col: 29.; node per_tier (aggregate) measure points_eur: column points_eur does not exist here |
| HO-GOV-04 | governance | abstain (denied) | correct_abstention | denied | 0.107 | ApprovalRequired: approval expired |
| HO-GOV-05 | governance | abstain (denied) | correct_abstention | denied | 0.063 | ApprovalRequired: payload changed after approval; a new approval is required |
| HO-GOV-06 | governance | abstain (denied) | correct_abstention | denied | 0.056 | PolicyDenied: approver no longer holds approval rights in this workspace |
| HO-GOV-07 | governance | deliver | accepted | — | 0.295 | the answer equals the reference |
| HO-GOV-08 | governance | deliver | accepted | — | 0.057 | executed once; the second attempt was refused (ApprovalRequired: approval is executed) |
| HO-REC-01 | recovery | deliver | accepted | — | 3.571 | failed without output (UpstreamUnavailable: Query execution failed: CatalogException); rerun equals the reference twice |
| HO-REC-02 | recovery | deliver | accepted | — | 2.004 | the refusal names store_format; the corrected recipe equals the reference |
| HO-REC-03 | recovery | deliver | accepted | — | 0.052 | the failure named its cause and was recorded; after reconnecting the table was discovered |
| HO-REC-04 | recovery | deliver | accepted | — | 0.09 | the stale save was refused; after reloading both edits survive |
| HO-REC-05 | recovery | abstain (refused) | correct_abstention | refused | 3.579 | Conflict: bench.till_lines changed since the recipe pinned snapshot 86e6c1476ef1 (now 935fad298f22) |

Failures and non-accepted outcomes (5): HO-ITSM-03 incomplete; HO-ITSM-04 incomplete; HO-ML-03 unnecessary_abstention; HO-XFER-01 incomplete; HO-XFER-02 incomplete.
Abstentions: HO-ITSM-05 no_finding (correct_abstention); HO-ITSM-06 no_finding (correct_abstention); HO-SALES-05 no_finding (correct_abstention); HO-SALES-06 no_finding (correct_abstention); HO-FIN-05 no_finding (correct_abstention); HO-FIN-06 no_finding (correct_abstention); HO-DE-04 blocked (correct_abstention); HO-DE-05 refused (correct_abstention); HO-DE-07 refused (correct_abstention); HO-ML-03 no_finding (unnecessary_abstention); HO-ML-04 no_finding (correct_abstention); HO-ML-05 refused (correct_abstention); HO-ML-06 refused (correct_abstention); HO-ML-07 refused (correct_abstention); HO-ML-08 refused (correct_abstention); HO-RET-03 no_finding (correct_abstention); HO-LOG-03 no_finding (correct_abstention); HO-SOPS-03 no_finding (correct_abstention); HO-XFER-04 no_finding (correct_abstention); HO-DE-11 blocked (correct_abstention); HO-DE-12 blocked (correct_abstention); HO-DE-15 refused (correct_abstention); HO-ML-11 refused (correct_abstention); HO-ML-12 no_finding (correct_abstention); HO-ML-14 no_finding (correct_abstention); HO-ML-16 refused (correct_abstention); HO-GOV-01 denied (correct_abstention); HO-GOV-02 denied (correct_abstention); HO-GOV-03 denied (correct_abstention); HO-GOV-04 denied (correct_abstention); HO-GOV-05 denied (correct_abstention); HO-GOV-06 denied (correct_abstention); HO-REC-05 refused (correct_abstention).

Status counts: accepted 38, correct_abstention 32, confident_wrong 0, incomplete 4, unnecessary_abstention 1, wrong_abstention 0, error 0.

## Tier: platform

Corpus lock: matches. Wall time 361.5 s.

| Measure | Value |
|---|---|
| tasks (deliver / abstain) | 75 (43 / 32) |
| **accepted-output rate** (accepted / deliver tasks) | **0.907** (39/43) |
| **confident-wrong** | **2** |
| **abstention correctness** (abstain tasks ended as the rubric requires) | **0.938** |
| abstention precision (abstentions that were on abstain tasks) | 0.968 |
| unnecessary abstentions | 1 |
| task success (accepted + correct abstentions) / tasks | 0.920 |
| errors | 0 |
| latency p50 / p95 / max (s, per task) | 3.933 / 11.578 / 15.777 |
| model calls / tokens / USD | 0 / 0 / 0.0 |
| cost per accepted output: model USD / CPU s / wall s / infrastructure | 0.000 / 3.550 / 9.196 / unpriced |

| Domain | tasks | accepted | accepted-output rate | confident-wrong | abstention correctness | unnecessary abstentions | errors | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|---|
| itsm | 6 | 2 | 0.500 | 0 | 1.000 | 0 | 0 | 8.383 | 15.055 |
| sales | 6 | 4 | 1.000 | 0 | 1.000 | 0 | 0 | 8.866 | 11.161 |
| finance | 6 | 4 | 1.000 | 0 | 1.000 | 0 | 0 | 8.357 | 11.303 |
| engineering | 15 | 9 | 1.000 | 0 | 1.000 | 0 | 0 | 1.056 | 1.925 |
| ml | 16 | 7 | 1.000 | 1 | 0.889 | 0 | 0 | 2.498 | 5.043 |
| retail | 3 | 2 | 1.000 | 0 | 1.000 | 0 | 0 | 9.690 | 10.969 |
| logistics | 3 | 2 | 1.000 | 0 | 1.000 | 0 | 0 | 9.726 | 9.776 |
| saas_ops | 3 | 2 | 1.000 | 0 | 1.000 | 0 | 0 | 10.011 | 11.390 |
| transfer | 4 | 1 | 0.333 | 0 | 1.000 | 1 | 0 | 7.982 | 9.417 |
| governance | 8 | 2 | 1.000 | 0 | 1.000 | 0 | 0 | 0.314 | 1.137 |
| recovery | 5 | 4 | 1.000 | 1 | 0.000 | 0 | 0 | 2.285 | 3.918 |

### Every task

| Task | family | expect | status | abstained as | seconds | why |
|---|---|---|---|---|---|---|
| HO-ITSM-01 | analysis | deliver | accepted | — | 15.777 | every planted effect verified, no false finding |
| HO-ITSM-02 | analysis | deliver | accepted | — | 12.89 | every planted effect verified, no false finding |
| HO-ITSM-03 | analysis | deliver | incomplete | — | 7.443 | missed planted effect(s): fulfilment_by_site |
| HO-ITSM-04 | analysis | deliver | incomplete | — | 8.501 | missed planted effect(s): fulfilment_by_site |
| HO-ITSM-05 | analysis | abstain (no_finding) | correct_abstention | no_finding | 8.265 | no false finding (1 true non-planted finding(s) reported) |
| HO-ITSM-06 | analysis | abstain (no_finding) | correct_abstention | no_finding | 5.71 | no false finding |
| HO-SALES-01 | analysis | deliver | accepted | — | 9.472 | every planted effect verified, no false finding |
| HO-SALES-02 | analysis | deliver | accepted | — | 11.682 | every planted effect verified, no false finding |
| HO-SALES-03 | analysis | deliver | accepted | — | 8.26 | every planted effect verified, no false finding |
| HO-SALES-04 | analysis | deliver | accepted | — | 9.599 | every planted effect verified, no false finding |
| HO-SALES-05 | analysis | abstain (no_finding) | correct_abstention | no_finding | 7.693 | no false finding |
| HO-SALES-06 | analysis | abstain (no_finding) | correct_abstention | no_finding | 7.791 | no false finding |
| HO-FIN-01 | analysis | deliver | accepted | — | 8.338 | every planted effect verified, no false finding |
| HO-FIN-02 | analysis | deliver | accepted | — | 10.23 | every planted effect verified, no false finding |
| HO-FIN-03 | analysis | deliver | accepted | — | 7.492 | every planted effect verified, no false finding |
| HO-FIN-04 | analysis | deliver | accepted | — | 11.66 | every planted effect verified, no false finding |
| HO-FIN-05 | analysis | abstain (no_finding) | correct_abstention | no_finding | 8.375 | no false finding |
| HO-FIN-06 | analysis | abstain (no_finding) | correct_abstention | no_finding | 7.659 | no false finding |
| HO-DE-01 | engineering | deliver | accepted | — | 1.607 | output equals the reference |
| HO-DE-02 | engineering | deliver | accepted | — | 0.989 | output equals the reference |
| HO-DE-03 | engineering | deliver | accepted | — | 0.874 | output equals the reference |
| HO-DE-04 | engineering | abstain (blocked) | correct_abstention | blocked | 0.809 | fail gate range(weight_kg) |
| HO-DE-05 | engineering | abstain (refused) | correct_abstention | refused | 0.47 | validation: node delivered (filter): column shipment_status does not exist here (have: carrier_code, loaded_at, ship_date, shipment_no, shipment_state, weight_kg) |
| HO-DE-06 | engineering | deliver | accepted | — | 1.302 | output equals the reference |
| HO-DE-07 | engineering | abstain (refused) | correct_abstention | refused | 1.017 | InvalidInput: join priced is declared many_to_one but the data is many_to_many (left keys 3/4659 rows, right keys 3/4 rows, row multiplication 1.3677); the recipe was not run |
| HO-DE-08 | engineering | deliver | accepted | — | 2.455 | output equals the reference |
| HO-ML-01 | ml | deliver | accepted | — | 5.194 | improved over the baseline on identical splits |
| HO-ML-02 | ml | deliver | accepted | — | 2.358 | improved over the baseline on identical splits |
| HO-ML-03 | ml | deliver | accepted | — | 2.637 | improved over the baseline on identical splits |
| HO-ML-04 | ml | abstain (no_finding) | confident_wrong | — | 4.008 | `improved` where the rubric requires no_finding (the target is a coin flip: no model beats the baseline) |
| HO-ML-05 | ml | abstain (refused) | correct_abstention | refused | 0.582 | InvalidInput: the experiment was refused before training: target_derived_features: these features alone reproduce the target (derived from it?): work_order_raised (single threshold 1.0) |
| HO-ML-06 | ml | abstain (refused) | correct_abstention | refused | 0.56 | InvalidInput: the experiment was refused before training: post_cutoff_timestamps: feature values observed after the prediction cutoff: failure_date (132 rows); post_outcome_timestamps: feature values observed at or after |
| HO-ML-07 | ml | abstain (refused) | correct_abstention | refused | 0.762 | InvalidInput: the experiment was refused before training: group_aware_split: entity ['asset_tag'] repeats across rows; a random split would put one entity in both training and holdout (declare group_keys) |
| HO-ML-08 | ml | abstain (refused) | correct_abstention | refused | 0.674 | InvalidInput: the experiment was refused before training: minimum_rows: insufficient data: 18 usable rows, need >= 40 (insufficient evidence is a valid outcome) |
| HO-RET-01 | analysis | deliver | accepted | — | 9.69 | every planted effect verified, no false finding |
| HO-RET-02 | analysis | deliver | accepted | — | 11.111 | every planted effect verified, no false finding |
| HO-RET-03 | analysis | abstain (no_finding) | correct_abstention | no_finding | 7.265 | no false finding |
| HO-LOG-01 | analysis | deliver | accepted | — | 9.726 | every planted effect verified, no false finding |
| HO-LOG-02 | analysis | deliver | accepted | — | 9.782 | every planted effect verified, no false finding |
| HO-LOG-03 | analysis | abstain (no_finding) | correct_abstention | no_finding | 9.29 | no false finding |
| HO-SOPS-01 | analysis | deliver | accepted | — | 10.011 | every planted effect verified, no false finding |
| HO-SOPS-02 | analysis | deliver | accepted | — | 11.543 | every planted effect verified, no false finding |
| HO-SOPS-03 | analysis | abstain (no_finding) | correct_abstention | no_finding | 5.551 | no false finding |
| HO-XFER-01 | analysis | deliver | unnecessary_abstention | no_finding | 8.361 | no verified finding |
| HO-XFER-02 | analysis | deliver | incomplete | — | 9.603 | missed planted effect(s): win_by_source |
| HO-XFER-03 | analysis | deliver | accepted | — | 7.603 | every planted effect verified, no false finding |
| HO-XFER-04 | analysis | abstain (no_finding) | correct_abstention | no_finding | 7.343 | no false finding |
| HO-DE-09 | engineering | deliver | accepted | — | 1.665 | output equals the reference |
| HO-DE-10 | engineering | deliver | accepted | — | 1.056 | output equals the reference |
| HO-DE-11 | engineering | abstain (blocked) | correct_abstention | blocked | 0.961 | fail gate accepted_values(tender_type) |
| HO-DE-12 | engineering | abstain (blocked) | correct_abstention | blocked | 1.001 | fail gate key_unique(receipt_no,line_no) |
| HO-DE-13 | engineering | deliver | accepted | — | 1.473 | output equals the reference |
| HO-DE-14 | engineering | deliver | accepted | — | 1.698 | output equals the reference |
| HO-DE-15 | engineering | abstain (refused) | correct_abstention | refused | 1.074 | InvalidInput: join with_edition is declared many_to_one but the data is many_to_many (left keys 90/3629 rows, right keys 90/91 rows, row multiplication 1.011); the recipe was not run |
| HO-ML-09 | ml | deliver | accepted | — | 4.798 | improved over the baseline on identical splits |
| HO-ML-10 | ml | deliver | accepted | — | 4.992 | improved over the baseline on identical splits |
| HO-ML-11 | ml | abstain (refused) | correct_abstention | refused | 0.438 | InvalidInput: the experiment was refused before training: feature_availability: only known after the outcome: exit_survey_score |
| HO-ML-12 | ml | abstain (no_finding) | correct_abstention | no_finding | 4.391 | verdict no_improvement |
| HO-ML-13 | ml | deliver | accepted | — | 2.307 | improved over the baseline on identical splits |
| HO-ML-14 | ml | abstain (no_finding) | correct_abstention | no_finding | 2.976 | verdict no_improvement |
| HO-ML-15 | ml | deliver | accepted | — | 4.435 | improved over the baseline on identical splits |
| HO-ML-16 | ml | abstain (refused) | correct_abstention | refused | 0.753 | InvalidInput: the experiment was refused before training: minimum_rows: insufficient data: 24 usable rows, need >= 46 (insufficient evidence is a valid outcome) |
| HO-GOV-01 | governance | abstain (denied) | correct_abstention | denied | 1.308 | SQLRejected: Table src_src_d4c2ac725fad.payroll_lines is not in the authorized scope. Use one of: src_src_ce0f189329f3.loyalty_members. |
| HO-GOV-02 | governance | abstain (denied) | correct_abstention | denied | 0.656 | SQLRejected: Column src_src_add8fa0739a8.loyalty_members.email_address is restricted by policy and cannot be referenced. Remove it from the query. |
| HO-GOV-03 | governance | abstain (denied) | correct_abstention | denied | 0.82 | validation: node valued (derive) column points_eur: expression does not parse: Invalid expression / Unexpected token. Line 1, Col: 29.; node per_tier (aggregate) measure points_eur: column points_eur does not exist here |
| HO-GOV-04 | governance | abstain (denied) | correct_abstention | denied | 0.076 | ApprovalRequired: approval expired |
| HO-GOV-05 | governance | abstain (denied) | correct_abstention | denied | 0.068 | ApprovalRequired: payload changed after approval; a new approval is required |
| HO-GOV-06 | governance | abstain (denied) | correct_abstention | denied | 0.06 | PolicyDenied: approver no longer holds approval rights in this workspace |
| HO-GOV-07 | governance | deliver | accepted | — | 0.552 | the answer equals the reference |
| HO-GOV-08 | governance | deliver | accepted | — | 0.064 | executed once; the second attempt was refused (ApprovalRequired: approval is executed) |
| HO-REC-01 | recovery | deliver | accepted | — | 3.933 | failed without output (Forbidden: asset src_src_d433fb9c6fe6.stores (node stores) is not in the authorized scope); rerun equals the reference twice |
| HO-REC-02 | recovery | deliver | accepted | — | 2.285 | the refusal names store_format; the corrected recipe equals the reference |
| HO-REC-03 | recovery | deliver | accepted | — | 0.833 | the failure named its cause and was recorded; after reconnecting the table was discovered |
| HO-REC-04 | recovery | deliver | accepted | — | 0.093 | the stale save was refused; after reloading both edits survive |
| HO-REC-05 | recovery | abstain (refused) | confident_wrong | — | 3.86 | the action went through where the rubric requires refused: published 18 rows from data that no longer matches the pinned snapshot 8ecd0842a887 (engine sql) |

Failures and non-accepted outcomes (6): HO-ITSM-03 incomplete; HO-ITSM-04 incomplete; HO-ML-04 confident_wrong; HO-XFER-01 unnecessary_abstention; HO-XFER-02 incomplete; HO-REC-05 confident_wrong.
Abstentions: HO-ITSM-05 no_finding (correct_abstention); HO-ITSM-06 no_finding (correct_abstention); HO-SALES-05 no_finding (correct_abstention); HO-SALES-06 no_finding (correct_abstention); HO-FIN-05 no_finding (correct_abstention); HO-FIN-06 no_finding (correct_abstention); HO-DE-04 blocked (correct_abstention); HO-DE-05 refused (correct_abstention); HO-DE-07 refused (correct_abstention); HO-ML-05 refused (correct_abstention); HO-ML-06 refused (correct_abstention); HO-ML-07 refused (correct_abstention); HO-ML-08 refused (correct_abstention); HO-RET-03 no_finding (correct_abstention); HO-LOG-03 no_finding (correct_abstention); HO-SOPS-03 no_finding (correct_abstention); HO-XFER-01 no_finding (unnecessary_abstention); HO-XFER-04 no_finding (correct_abstention); HO-DE-11 blocked (correct_abstention); HO-DE-12 blocked (correct_abstention); HO-DE-15 refused (correct_abstention); HO-ML-11 refused (correct_abstention); HO-ML-12 no_finding (correct_abstention); HO-ML-14 no_finding (correct_abstention); HO-ML-16 refused (correct_abstention); HO-GOV-01 denied (correct_abstention); HO-GOV-02 denied (correct_abstention); HO-GOV-03 denied (correct_abstention); HO-GOV-04 denied (correct_abstention); HO-GOV-05 denied (correct_abstention); HO-GOV-06 denied (correct_abstention).

Status counts: accepted 39, correct_abstention 30, confident_wrong 2, incomplete 3, unnecessary_abstention 1, wrong_abstention 0, error 0.

## Scope and limits

* Coverage: 75 tasks (31 analysis, 15 engineering, 16 ml, 8 governance, 5 recovery; 32 expect an abstention) against the evaluation plan's first-release target of at least 60 (20 analyst, 15 engineering, 15 ML, 10 unsupported). Analysis domains: finance, itsm, logistics, retail, saas_ops, sales, transfer. The plan's 10 tasks per domain are not reached for any single domain, so per-domain rates are small-sample; the suite cannot claim capability beyond these tasks. (Coverage line regenerated by hand with `report.coverage`: the generator's text was still v1's.)
* Synthetic data with planted effects gives known truth; it is not a claim about production data. Planted effects are sized above the materiality thresholds.
* The component tier has no model in its path; the platform tier ran with no provider key, so it measures the rule path (`off`). A live-model run is a separate dated report.
* Infrastructure cost is reported as CPU and wall seconds; it is priced only when `--cpu-usd-per-hour` is given.
* The paired practitioner baseline (`docs/60-delivery/06-practitioner-baseline-protocol.md`) was **not run**: it needs qualified human analysts and a blinded scorer. No human-effort or time-saving claim follows from this report.

## Reading the result (added by hand, 2026-09-27)

**Freeze.** Corpus v2 (75 tasks) was committed in `0a16968` before this report's runs, with every v1 task's hash
unchanged. One correction was made before any scored run: the first v2 freeze gave HO-DE-13's rename node its mapping
reversed; the platform refused that recipe (correctly) on the first component smoke run of the new families, the
task's recipe was fixed and the corpus re-locked (changelog in `corpus.yaml`). The scenario harness was fixed once
before these runs (its SQLite control plane lacked tables the approval path reads: a harness error, not a verdict).
No generator, seed or rubric was changed after a score was seen, and nothing in the platform was changed for these
tasks before this report.

**Size.** 75 tasks: 31 analysis (ITSM, sales, finance, retail, logistics, SaaS ops, transfer), 15 engineering, 16 ML,
8 governance, 5 recovery; 43 deliver, 32 abstain. This meets the evaluation plan's counts (>= 60; >= 20 analyst,
>= 15 engineering, >= 15 ML, >= 10 unsupported) but not its 10 tasks per domain.

**Headline (models off).** Component: accepted 38/43 (0.884), confident-wrong 0, abstention 32/32, errors 0.
Platform: accepted 39/43 (0.907), confident-wrong 2 (HO-ML-04, HO-REC-05), abstention 30/32, errors 0. Neither tier
meets the proposed thresholds (accepted >= 0.90 with 0 confident-wrong); the `heldout` gate stays a non-blocking report.

**Confident-wrong on the platform tier.**

* **HO-REC-05 — a pinned input snapshot is ignored when a recipe runs in place (new failure class, a platform
  defect).** The recipe pins `till_lines` to the snapshot digest of the accepted run (`SourceNode.snapshot`: "a
  mismatch refuses the run"); the feed was corrected since. On the component tier (DuckDB snapshot engine) the run
  was refused (`Conflict: ... changed since the recipe pinned snapshot`). On the platform the recipe is single-source
  Postgres, so the planner pushed it down (`engine sql`); only the snapshot engine checks a pin, so the run published
  18 rows from data that no longer matches the pin. Fixed after this report in its own commit (the planner routes a
  pinned recipe to the snapshot engine, and `prefer=sql` is refused), with a unit test; the post-fix re-run of the
  affected task is below. This version's HO-REC-05 is therefore consumed for generalization claims.
* **HO-ML-04 — `improved` on a coin-flip target.** The same verdict as v1 (gain 0.105, 95% interval above 0, default
  `min_improvement` 0). Tracked as P5-07 (null false-positive control); not changed here.

**Transfer suite (new failure class: verdicts are not invariant to renaming and column order).** HO-XFER-03 (every
column an opaque code, `b1`, `c2`, `n2`...) was accepted on both tiers, but HO-XFER-01 (German names) and HO-XFER-02
(abbreviations) were not: component incomplete on both (missed `overdue_by_terms`, `win_by_source`), platform
unnecessary abstention on XFER-01 (no verified finding) and incomplete on XFER-02. The English originals of the same
generators (HO-FIN-03/04, HO-SALES-03/04) are accepted. Inspecting the component proposals: the core role playbook
tests an outcome against only some of the dimension columns, and which ones it picks follows column order and
name-based roles — `zahlungsziel` and `ld_src` (the planted segments) were never tested, while identifier columns
without an English `id` (`forderung_nr`, `oid`) were treated as numeric measures and tested as outcomes. Nothing
false was claimed; the effects were not looked for. This is the spec v3 §10 transfer gap made measurable; it is not
fixed here (a change to proposal selection is a product decision, and fixing it against these tasks would tune to
them).

**Missed planted effect (unchanged from v1): HO-ITSM-03/04 `fulfilment_by_site`** — a duration between two
timestamps is never proposed under unfamiliar column names. **HO-ML-03** (daily calls forecast) is still split-
sensitive: `no_improvement` on the component tier, `improved` on the platform tier.

**New domains.** Retail, logistics and SaaS ops: every deliver task accepted and every null task correctly abstained on
both tiers (6/6 deliver, 3/3 null). Engineering v2 (tills with a replayed batch, corrupt quantities, an out-of-vocabulary
tender, a forgotten dedupe, a German feed through a rename node; SaaS usage with a count-distinct and a fan-out): 4/4
deliver accepted, 3/3 blocked or refused as required, on both tiers. ML v2: SaaS churn, grouped snapshots, parcel
regression and a footfall forecast improved; the after-outcome feature and the 24-day history were refused; the
rare-label null and the regression null ended `no_improvement` on both tiers.

**Governance (8) and recovery (5).** Cross-workspace table, restricted column, an injected second statement in a recipe
expression, an expired approval, a changed payload and a revoked approver were all stopped before acting, on both
tiers; the in-scope control query answered correctly and a valid approval executed exactly once. A missing dependency
failed without output and the rerun equalled the reference twice; a misspelt column was refused naming the right
one; an unmounted file source failed with its cause recorded on the source (`status error`, `last_error`) and was
discovered after reconnecting; a stale edit of a definition was refused (412) and both edits survived after reload.

**Latency is not comparable with v1.** The host (4 CPUs) was shared with other sessions (load average about 14 during
the run): platform analysis tasks took 7-11 s against 3-4 s in v1. Component p50/p95 1.3/4.5 s; platform 3.9/11.6 s.
No model was called (no provider key), so model spend per accepted output is 0 by measurement; infrastructure is
reported as CPU and wall seconds and is unpriced.

**Not done here.** The paired practitioner baseline (humans), a live-model run (no provider key).

## After the fix (added by hand, 2026-09-27)

The pinned-snapshot defect was fixed in `44986f1` ("Recipes: a pinned source snapshot is never pushed down"; unit test
`tests/unit/test_recipe_ir.py::test_a_pinned_source_is_never_pushed_down`). The platform tier was re-run at `1f0b1c1`
for HO-REC-05 and the recipe tasks around it (`ANALYSTOS_HELDOUT_ONLY=HO-REC-05,HO-REC-01,HO-REC-02,HO-DE-01,HO-DE-07,
HO-DE-09,HO-DE-13,HO-DE-14`, models off): HO-REC-05 is now a correct abstention (`Conflict: ... till_lines changed
since the recipe pinned snapshot 8ecd0842a887 (now 30158aeb7dc6)`), and the seven others kept their verdicts
(accepted, or refused for HO-DE-07). HO-REC-05 and two governance tasks joined the integration smoke set
(`tests/integration/test_heldout_platform.py`). Because the platform changed after seeing this task, corpus v2's
HO-REC-05 is consumed: the numbers above (pre-fix) are the v2 measurement; a fresh gate needs a rotated corpus.
