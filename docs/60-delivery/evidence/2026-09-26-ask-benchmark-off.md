# Ask accuracy benchmark (P4-V02) — tier `off` — 2026-09-26

Generated 2026-09-26 00:10 UTC by `scripts/benchmark_ask.py --models off` at `7edf985` (Python 3.11.15), seed 1, 14.8 s.

Tier: **off** — no provider key: the verified-query registry and the decision rules answer; any question they do not cover is refused (`no_model`). This is the no-model floor, not the model's accuracy.

Environment: the local compose Postgres/Redis, with a throwaway control plane (analystos_test_vq, created, migrated and seeded by the control_db fixture) and a throwaway analytics plane (analystos_test_dp_vq_analytics) — evaluation.ask.run("off") invoked through the integration fixtures of tests/conftest.py, as for the 2026-09-25 report. Change measured: the verified-query registry now serves a match only when its SQL is compatible with the question (aggregate, group-by, filters and values, hard-coded literals; src/analystos/registries/verified_queries.py) — the revision shown contains that change. Before (2026-09-25-ask-benchmark-off.md, same frozen question set): execution accuracy 0.495 (52/105), 61/105 answered, 9 confident wrong.

Data: the V01 datasets (`evaluation.datasets`, seed 1, with effects) plus one restricted column per domain (the workspace policy denies it), uploaded as Parquet, discovered and staged by the loader. The Ask budget of the benchmark workspaces is raised to 5000 statements/hour so the budget never refuses a question.

| Domain | table | rows | verified queries | setup s |
|---|---|---|---|---|
| itsm | `src_src_bbfc41ad0083.incident` | 6000 | 9 | 0.7 |
| sales | `src_src_e82c764eb936.orders` | 8000 | 9 | 0.3 |
| finance | `src_src_cf092d329085.ap_invoice` | 6000 | 9 | 0.2 |

## Results

| Measure | Overall | itsm | sales | finance |
|---|---|---|---|---|
| questions (answer / needs_input / clarify / decline) | 105 / 15 / 15 / 27 | 35 / 5 / 5 / 9 | 35 / 5 / 5 / 9 | 35 / 5 / 5 / 9 |
| **execution accuracy** | **0.495** (52/105) | **0.486** (17/35) | **0.486** (17/35) | **0.514** (18/35) |
| answer items answered (coverage) | 52/105 | 17/35 | 17/35 | 18/35 |
| precision of everything answered | 1.000 | 1.000 | 1.000 | 1.000 |
| **confident wrong** (answered, not right) | **0** (0.000) | **0** (0.000) | **0** (0.000) | **0** (0.000) |
| outcome accuracy (all items) | 0.586 | 0.593 | 0.574 | 0.593 |
| needs_input: precision / recall | 0.923 / 0.800 (12/15, predicted 13) | 1.000 / 0.800 (4/5, predicted 4) | 1.000 / 0.800 (4/5, predicted 4) | 0.800 / 0.800 (4/5, predicted 5) |
| clarify: precision / recall | 1.000 / 0.267 (4/15, predicted 4) | 1.000 / 0.400 (2/5, predicted 2) | 1.000 / 0.200 (1/5, predicted 1) | 1.000 / 0.200 (1/5, predicted 1) |
| decline: precision / recall | 0.290 / 1.000 (27/27, predicted 93) | 0.290 / 1.000 (9/9, predicted 31) | 0.281 / 1.000 (9/9, predicted 32) | 0.300 / 1.000 (9/9, predicted 30) |
| decline out_of_scope: declined / governed of n | 12 / 0 of 12 | 4 / 0 of 4 | 4 / 0 of 4 | 4 / 0 of 4 |
| decline write: declined / governed of n | 3 / 0 of 3 | 1 / 0 of 1 | 1 / 0 of 1 | 1 / 0 of 1 |
| decline restricted: declined / governed of n | 12 / 0 of 12 | 4 / 0 of 4 | 4 / 0 of 4 | 4 / 0 of 4 |
| restricted answers (leaks) | 0 | 0 | 0 | 0 |
| latency p50 / p95 ms | 58 / 74 | 60 / 75 | 56 / 78 | 56 / 68 |
| provider calls (per question) | 0 (0.000) | 0 (0.000) | 0 (0.000) | 0 (0.000) |
| tokens in / out (per question) | 0 / 0 (0.000) | 0 / 0 (0.000) | 0 / 0 (0.000) | 0 / 0 (0.000) |
| model cost USD | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| tokens avoided (registry skips) | 57619 | 18410 | 19698 | 19511 |

Answered by: registry: 52.
Refusal kinds seen: clarify: 4, needs_input: 13, no_model: 93.

### Answer items by kind of wording

| tag | n | matched | accuracy | how they ended |
|---|---|---|---|---|
| near_miss | 11 | 0 | 0.000 | decline: 10, needs_input: 1 |
| novel | 33 | 0 | 0.000 | decline: 33 |
| parameter | 18 | 18 | 1.000 | registry: 18 |
| paraphrase | 27 | 18 | 0.667 | decline: 9, registry: 18 |
| registry_exact | 21 | 21 | 1.000 | registry: 21 |

### Confident wrong answers

None.

### Every question

| id | expect | actual | kind | by | match | ms | calls |
|---|---|---|---|---|---|---|---|
| itsm-a01 | answer | answer |  | registry `incidents_by_priority` | yes | 111 | 0 |
| itsm-a02 | answer | answer |  | registry `incidents_by_priority` | yes | 78 | 0 |
| itsm-a03 | answer | answer |  | registry `incidents_by_priority` | yes | 72 | 0 |
| itsm-a04 | answer | answer |  | registry `sla_breach_rate_by_group` | yes | 60 | 0 |
| itsm-a05 | answer | answer |  | registry `sla_breach_rate_by_group` | yes | 55 | 0 |
| itsm-a06 | answer | decline | no_model |  |  | 66 | 0 |
| itsm-a07 | answer | answer |  | registry `resolution_hours_by_group` | yes | 74 | 0 |
| itsm-a08 | answer | decline | no_model |  |  | 71 | 0 |
| itsm-a09 | answer | answer |  | registry `incidents_for_priority` | yes | 68 | 0 |
| itsm-a10 | answer | answer |  | registry `incidents_for_priority` | yes | 63 | 0 |
| itsm-a11 | answer | answer |  | registry `incidents_for_group` | yes | 72 | 0 |
| itsm-a12 | answer | answer |  | registry `incidents_for_group` | yes | 70 | 0 |
| itsm-a13 | answer | answer |  | registry `incidents_per_month` | yes | 74 | 0 |
| itsm-a14 | answer | answer |  | registry `incidents_per_month` | yes | 73 | 0 |
| itsm-a15 | answer | decline | no_model |  |  | 64 | 0 |
| itsm-a16 | answer | answer |  | registry `top_ci_priority_1` | yes | 71 | 0 |
| itsm-a17 | answer | decline | no_model |  |  | 61 | 0 |
| itsm-a18 | answer | answer |  | registry `incidents_opened_between` | yes | 63 | 0 |
| itsm-a19 | answer | answer |  | registry `incidents_opened_between` | yes | 65 | 0 |
| itsm-a20 | answer | answer |  | registry `sla_breach_rate_by_reassignments` | yes | 77 | 0 |
| itsm-a21 | answer | answer |  | registry `sla_breach_rate_by_reassignments` | yes | 73 | 0 |
| itsm-a22 | answer | decline | no_model |  |  | 58 | 0 |
| itsm-a23 | answer | decline | no_model |  |  | 64 | 0 |
| itsm-a24 | answer | decline | no_model |  |  | 55 | 0 |
| itsm-a25 | answer | decline | no_model |  |  | 56 | 0 |
| itsm-a26 | answer | decline | no_model |  |  | 60 | 0 |
| itsm-a27 | answer | decline | no_model |  |  | 68 | 0 |
| itsm-a28 | answer | decline | no_model |  |  | 54 | 0 |
| itsm-a29 | answer | decline | no_model |  |  | 60 | 0 |
| itsm-a30 | answer | decline | no_model |  |  | 51 | 0 |
| itsm-a31 | answer | decline | no_model |  |  | 56 | 0 |
| itsm-a32 | answer | decline | no_model |  |  | 65 | 0 |
| itsm-a33 | answer | decline | no_model |  |  | 59 | 0 |
| itsm-a34 | answer | decline | no_model |  |  | 53 | 0 |
| itsm-a35 | answer | decline | no_model |  |  | 56 | 0 |
| itsm-n01 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_for_priority` |  | 41 | 0 |
| itsm-n02 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_for_priority` |  | 37 | 0 |
| itsm-n03 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_opened_between` |  | 40 | 0 |
| itsm-n04 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_opened_between` |  | 51 | 0 |
| itsm-n05 | needs_input/missing_parameter | decline | no_model |  |  | 57 | 0 |
| itsm-c01 | clarify/ambiguous | clarify | clarify |  |  | 32 | 0 |
| itsm-c02 | clarify/ambiguous | decline | no_model |  |  | 50 | 0 |
| itsm-c03 | clarify/ambiguous | decline | no_model |  |  | 54 | 0 |
| itsm-c04 | clarify/ambiguous | decline | no_model |  |  | 65 | 0 |
| itsm-c05 | clarify/ambiguous | clarify | clarify |  |  | 47 | 0 |
| itsm-o01 | decline/out_of_scope | decline | no_model |  |  | 60 | 0 |
| itsm-o02 | decline/out_of_scope | decline | no_model |  |  | 51 | 0 |
| itsm-o03 | decline/out_of_scope | decline | no_model |  |  | 58 | 0 |
| itsm-o04 | decline/write | decline | no_model |  |  | 55 | 0 |
| itsm-o05 | decline/out_of_scope | decline | no_model |  |  | 64 | 0 |
| itsm-r01 | decline/restricted | decline | no_model |  |  | 59 | 0 |
| itsm-r02 | decline/restricted | decline | no_model |  |  | 58 | 0 |
| itsm-r03 | decline/restricted | decline | no_model |  |  | 65 | 0 |
| itsm-r04 | decline/restricted | decline | no_model |  |  | 55 | 0 |
| sales-a01 | answer | answer |  | registry `revenue_by_region` | yes | 95 | 0 |
| sales-a02 | answer | answer |  | registry `revenue_by_region` | yes | 78 | 0 |
| sales-a03 | answer | decline | no_model |  |  | 61 | 0 |
| sales-a04 | answer | answer |  | registry `return_rate_by_channel` | yes | 71 | 0 |
| sales-a05 | answer | answer |  | registry `return_rate_by_channel` | yes | 56 | 0 |
| sales-a06 | answer | decline | no_model |  |  | 62 | 0 |
| sales-a07 | answer | answer |  | registry `orders_by_segment` | yes | 72 | 0 |
| sales-a08 | answer | answer |  | registry `orders_by_segment` | yes | 65 | 0 |
| sales-a09 | answer | answer |  | registry `order_value_by_segment` | yes | 60 | 0 |
| sales-a10 | answer | decline | no_model |  |  | 54 | 0 |
| sales-a11 | answer | answer |  | registry `revenue_for_region` | yes | 60 | 0 |
| sales-a12 | answer | answer |  | registry `revenue_for_region` | yes | 67 | 0 |
| sales-a13 | answer | answer |  | registry `monthly_revenue` | yes | 68 | 0 |
| sales-a14 | answer | answer |  | registry `monthly_revenue` | yes | 70 | 0 |
| sales-a15 | answer | answer |  | registry `monthly_revenue` | yes | 68 | 0 |
| sales-a16 | answer | answer |  | registry `orders_for_channel` | yes | 72 | 0 |
| sales-a17 | answer | answer |  | registry `orders_for_channel` | yes | 74 | 0 |
| sales-a18 | answer | answer |  | registry `revenue_since` | yes | 67 | 0 |
| sales-a19 | answer | answer |  | registry `revenue_since` | yes | 94 | 0 |
| sales-a20 | answer | answer |  | registry `shipping_days_by_region` | yes | 78 | 0 |
| sales-a21 | answer | decline | no_model |  |  | 57 | 0 |
| sales-a22 | answer | decline | no_model |  |  | 60 | 0 |
| sales-a23 | answer | decline | no_model |  |  | 56 | 0 |
| sales-a24 | answer | decline | no_model |  |  | 57 | 0 |
| sales-a25 | answer | decline | no_model |  |  | 52 | 0 |
| sales-a26 | answer | decline | no_model |  |  | 67 | 0 |
| sales-a27 | answer | decline | no_model |  |  | 55 | 0 |
| sales-a28 | answer | decline | no_model |  |  | 60 | 0 |
| sales-a29 | answer | decline | no_model |  |  | 57 | 0 |
| sales-a30 | answer | decline | no_model |  |  | 50 | 0 |
| sales-a31 | answer | decline | no_model |  |  | 51 | 0 |
| sales-a32 | answer | decline | no_model |  |  | 44 | 0 |
| sales-a33 | answer | decline | no_model |  |  | 57 | 0 |
| sales-a34 | answer | decline | no_model |  |  | 60 | 0 |
| sales-a35 | answer | decline | no_model |  |  | 56 | 0 |
| sales-n01 | needs_input/missing_parameter | needs_input | needs_input |  `revenue_for_region` |  | 44 | 0 |
| sales-n02 | needs_input/missing_parameter | needs_input | needs_input |  `orders_for_channel` |  | 44 | 0 |
| sales-n03 | needs_input/missing_parameter | needs_input | needs_input |  `revenue_since` |  | 39 | 0 |
| sales-n04 | needs_input/missing_parameter | needs_input | needs_input |  `revenue_since` |  | 47 | 0 |
| sales-n05 | needs_input/missing_parameter | decline | no_model |  |  | 47 | 0 |
| sales-c01 | clarify/ambiguous | clarify | clarify |  |  | 32 | 0 |
| sales-c02 | clarify/ambiguous | decline | no_model |  |  | 54 | 0 |
| sales-c03 | clarify/ambiguous | decline | no_model |  |  | 69 | 0 |
| sales-c04 | clarify/ambiguous | decline | no_model |  |  | 50 | 0 |
| sales-c05 | clarify/ambiguous | decline | no_model |  |  | 52 | 0 |
| sales-o01 | decline/out_of_scope | decline | no_model |  |  | 56 | 0 |
| sales-o02 | decline/out_of_scope | decline | no_model |  |  | 54 | 0 |
| sales-o03 | decline/write | decline | no_model |  |  | 48 | 0 |
| sales-o04 | decline/out_of_scope | decline | no_model |  |  | 49 | 0 |
| sales-o05 | decline/out_of_scope | decline | no_model |  |  | 51 | 0 |
| sales-r01 | decline/restricted | decline | no_model |  |  | 48 | 0 |
| sales-r02 | decline/restricted | decline | no_model |  |  | 46 | 0 |
| sales-r03 | decline/restricted | decline | no_model |  |  | 53 | 0 |
| sales-r04 | decline/restricted | decline | no_model |  |  | 50 | 0 |
| finance-a01 | answer | answer |  | registry `invoice_amount_by_vendor_category` | yes | 74 | 0 |
| finance-a02 | answer | answer |  | registry `invoice_amount_by_vendor_category` | yes | 60 | 0 |
| finance-a03 | answer | decline | no_model |  |  | 56 | 0 |
| finance-a04 | answer | answer |  | registry `late_rate_by_unit` | yes | 61 | 0 |
| finance-a05 | answer | decline | no_model |  |  | 67 | 0 |
| finance-a06 | answer | answer |  | registry `late_rate_by_unit` | yes | 52 | 0 |
| finance-a07 | answer | answer |  | registry `days_to_pay_by_unit` | yes | 63 | 0 |
| finance-a08 | answer | answer |  | registry `days_to_pay_by_unit` | yes | 61 | 0 |
| finance-a09 | answer | answer |  | registry `invoices_by_currency` | yes | 66 | 0 |
| finance-a10 | answer | answer |  | registry `invoices_by_currency` | yes | 58 | 0 |
| finance-a11 | answer | answer |  | registry `invoice_amount_for_unit` | yes | 64 | 0 |
| finance-a12 | answer | answer |  | registry `invoice_amount_for_unit` | yes | 63 | 0 |
| finance-a13 | answer | answer |  | registry `monthly_invoice_amount` | yes | 67 | 0 |
| finance-a14 | answer | answer |  | registry `monthly_invoice_amount` | yes | 59 | 0 |
| finance-a15 | answer | answer |  | registry `invoices_over_amount` | yes | 66 | 0 |
| finance-a16 | answer | answer |  | registry `invoices_over_amount` | yes | 68 | 0 |
| finance-a17 | answer | answer |  | registry `late_invoices_for_category` | yes | 55 | 0 |
| finance-a18 | answer | answer |  | registry `late_invoices_for_category` | yes | 65 | 0 |
| finance-a19 | answer | answer |  | registry `invoices_by_entry_channel` | yes | 58 | 0 |
| finance-a20 | answer | answer |  | registry `invoices_by_entry_channel` | yes | 72 | 0 |
| finance-a21 | answer | decline | no_model |  |  | 69 | 0 |
| finance-a22 | answer | decline | no_model |  |  | 58 | 0 |
| finance-a23 | answer | decline | no_model |  |  | 48 | 0 |
| finance-a24 | answer | needs_input | needs_input |  `late_invoices_for_category` |  | 40 | 0 |
| finance-a25 | answer | decline | no_model |  |  | 58 | 0 |
| finance-a26 | answer | decline | no_model |  |  | 58 | 0 |
| finance-a27 | answer | decline | no_model |  |  | 58 | 0 |
| finance-a28 | answer | decline | no_model |  |  | 65 | 0 |
| finance-a29 | answer | decline | no_model |  |  | 61 | 0 |
| finance-a30 | answer | decline | no_model |  |  | 55 | 0 |
| finance-a31 | answer | decline | no_model |  |  | 53 | 0 |
| finance-a32 | answer | decline | no_model |  |  | 55 | 0 |
| finance-a33 | answer | decline | no_model |  |  | 48 | 0 |
| finance-a34 | answer | decline | no_model |  |  | 57 | 0 |
| finance-a35 | answer | decline | no_model |  |  | 52 | 0 |
| finance-n01 | needs_input/missing_parameter | needs_input | needs_input |  `invoice_amount_for_unit` |  | 40 | 0 |
| finance-n02 | needs_input/missing_parameter | needs_input | needs_input |  `invoices_over_amount` |  | 40 | 0 |
| finance-n03 | needs_input/missing_parameter | needs_input | needs_input |  `late_invoices_for_category` |  | 37 | 0 |
| finance-n04 | needs_input/missing_parameter | needs_input | needs_input |  `invoices_over_amount` |  | 45 | 0 |
| finance-n05 | needs_input/missing_parameter | decline | no_model |  |  | 51 | 0 |
| finance-c01 | clarify/ambiguous | clarify | clarify |  |  | 34 | 0 |
| finance-c02 | clarify/ambiguous | decline | no_model |  |  | 57 | 0 |
| finance-c03 | clarify/ambiguous | decline | no_model |  |  | 51 | 0 |
| finance-c04 | clarify/ambiguous | decline | no_model |  |  | 55 | 0 |
| finance-c05 | clarify/ambiguous | decline | no_model |  |  | 52 | 0 |
| finance-o01 | decline/out_of_scope | decline | no_model |  |  | 56 | 0 |
| finance-o02 | decline/out_of_scope | decline | no_model |  |  | 55 | 0 |
| finance-o03 | decline/write | decline | no_model |  |  | 53 | 0 |
| finance-o04 | decline/out_of_scope | decline | no_model |  |  | 51 | 0 |
| finance-o05 | decline/out_of_scope | decline | no_model |  |  | 42 | 0 |
| finance-r01 | decline/restricted | decline | no_model |  |  | 63 | 0 |
| finance-r02 | decline/restricted | decline | no_model |  |  | 55 | 0 |
| finance-r03 | decline/restricted | decline | no_model |  |  | 53 | 0 |
| finance-r04 | decline/restricted | decline | no_model |  |  | 55 | 0 |

## How to read this

* **Execution accuracy** = answer items whose answer matched the gold result / answer items. Match: same row count,
  each gold column matched by a distinct answer column with equal values, rows equal as a multiset (order-insensitive;
  numbers within rel 1e-4 / abs 1e-6; timestamps as naive ISO; extra answer columns allowed). A scale difference
  (percent for a fraction) is a mismatch.
* **Confident wrong** = answered with numbers that are not the right answer: an answer item with a mismatching result,
  or a needs_input / clarify / decline item that was answered. This is the costly failure; refusing is not.
* **Refusal correctness**: precision and recall of each refusal class (needs_input, clarify, decline) against the labels.
  For decline items the report also counts a *governed* decline — refused by the gateway or policy
  (`sql_rejected`, `policy_denied`, `no_scope`) — since a `no_model` refusal declines for the wrong reason.
* The question set, the registry and the gold SQL were frozen before any tier ran (evaluation/ask_questions/README.md).
  The registry stands for what analysts had promoted; `near_miss` items are worded close to a registry entry but
  need different SQL, which is where a token matcher can answer confidently and wrongly.

## Pilot threshold — PROPOSED, to be set by the owner before the pilot

Placeholder values for the **live** tier; nothing here is enforced, and the owner fixes them (with the question set version) before the pilot starts, not after seeing a live run.

| Proposed gate | Value |
|---|---|
| execution_accuracy_min | 0.85 |
| execution_accuracy_per_domain_min | 0.8 |
| confident_wrong_max_share | 0.05 |
| restricted_leaks_max | 0 |
| governed_decline_recall_min | 1.0 |
| needs_input_recall_min | 0.8 |
| clarify_recall_min | 0.6 |

## Tier invariants

PASS — every invariant held (evaluation.ask.check).

## Live tier

Not attempted: this report measures the registry compatibility fix in the no-model tier only; the live tier needs OPENROUTER_API_KEY in the environment (python scripts/benchmark_ask.py --models live --report auto).
