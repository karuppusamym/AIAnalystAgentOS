# Ask accuracy benchmark (P4-V02) — tier `off` — 2026-09-26

Generated 2026-09-26 04:10 UTC by `scripts/benchmark_ask.py --models off` at `f19869f` (Python 3.11.15), seed 1, 17.6 s.

Tier: **off** — no provider key: the verified-query registry and the Ask rules (pack distributions and the catalog-built simple shapes) answer; any question they do not cover is refused (`no_api_key`, before 2026-09-26 `no_model`). This is the no-model floor, not the model's accuracy.

Environment: the local compose Postgres/Redis, with a throwaway control plane (analystos_test_askx, created, migrated and seeded by the control_db fixture) and a throwaway analytics plane (analystos_test_dp_askx*) — evaluation.ask.run("off") invoked through the integration fixtures of tests/conftest.py, as for the earlier reports. Change measured: the Ask rules rung (src/analystos/agents/ask_rules.py: simple single-table shapes answered from catalog metadata through skills/sqlbuild and the gateway, clarify on ambiguity, fall-through on anything unexplained) and the per-cause refusal kinds (a question nothing answers is now refused as no_api_key in this tier, not no_model). Before (2026-09-26-ask-benchmark-off.md, same frozen question set, registry compatibility check): execution accuracy 0.495 (52/105), 52/105 answered, 0 confident wrong, 0 leaks.

Data: the V01 datasets (`evaluation.datasets`, seed 1, with effects) plus one restricted column per domain (the workspace policy denies it), uploaded as Parquet, discovered and staged by the loader. The Ask budget of the benchmark workspaces is raised to 5000 statements/hour so the budget never refuses a question.

| Domain | table | rows | verified queries | setup s |
|---|---|---|---|---|
| itsm | `src_src_636c903cd7b5.incident` | 6000 | 9 | 0.8 |
| sales | `src_src_9bc917c27b8a.orders` | 8000 | 9 | 0.3 |
| finance | `src_src_a4cfb3d35ac6.ap_invoice` | 6000 | 9 | 0.3 |

## Results

| Measure | Overall | itsm | sales | finance |
|---|---|---|---|---|
| questions (answer / needs_input / clarify / decline) | 105 / 15 / 15 / 27 | 35 / 5 / 5 / 9 | 35 / 5 / 5 / 9 | 35 / 5 / 5 / 9 |
| **execution accuracy** | **0.619** (65/105) | **0.629** (22/35) | **0.571** (20/35) | **0.657** (23/35) |
| answer items answered (coverage) | 65/105 | 22/35 | 20/35 | 23/35 |
| precision of everything answered | 1.000 | 1.000 | 1.000 | 1.000 |
| **confident wrong** (answered, not right) | **0** (0.000) | **0** (0.000) | **0** (0.000) | **0** (0.000) |
| outcome accuracy (all items) | 0.667 | 0.685 | 0.630 | 0.685 |
| needs_input: precision / recall | 0.923 / 0.800 (12/15, predicted 13) | 1.000 / 0.800 (4/5, predicted 4) | 1.000 / 0.800 (4/5, predicted 4) | 0.800 / 0.800 (4/5, predicted 5) |
| clarify: precision / recall | 1.000 / 0.267 (4/15, predicted 4) | 1.000 / 0.400 (2/5, predicted 2) | 1.000 / 0.200 (1/5, predicted 1) | 1.000 / 0.200 (1/5, predicted 1) |
| decline: precision / recall | 0.338 / 1.000 (27/27, predicted 80) | 0.346 / 1.000 (9/9, predicted 26) | 0.310 / 1.000 (9/9, predicted 29) | 0.360 / 1.000 (9/9, predicted 25) |
| decline out_of_scope: declined / governed of n | 12 / 0 of 12 | 4 / 0 of 4 | 4 / 0 of 4 | 4 / 0 of 4 |
| decline write: declined / governed of n | 3 / 0 of 3 | 1 / 0 of 1 | 1 / 0 of 1 | 1 / 0 of 1 |
| decline restricted: declined / governed of n | 12 / 0 of 12 | 4 / 0 of 4 | 4 / 0 of 4 | 4 / 0 of 4 |
| restricted answers (leaks) | 0 | 0 | 0 | 0 |
| latency p50 / p95 ms | 73 / 95 | 68 / 83 | 73 / 89 | 79 / 105 |
| provider calls (per question) | 0 (0.000) | 0 (0.000) | 0 (0.000) | 0 (0.000) |
| tokens in / out (per question) | 0 / 0 (0.000) | 0 / 0 (0.000) | 0 / 0 (0.000) | 0 / 0 (0.000) |
| model cost USD | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| tokens avoided (registry and rules skips) | 65077 | 21278 | 21438 | 22361 |

Answered by: registry: 52, rules: 13.
Refusal kinds seen: clarify: 4, needs_input: 13, no_api_key: 80.

### Answer items by kind of wording

| tag | n | matched | accuracy | how they ended |
|---|---|---|---|---|
| near_miss | 11 | 4 | 0.364 | decline: 6, needs_input: 1, rules: 4 |
| novel | 33 | 7 | 0.212 | decline: 26, rules: 7 |
| parameter | 18 | 18 | 1.000 | registry: 18 |
| paraphrase | 27 | 20 | 0.741 | decline: 7, registry: 18, rules: 2 |
| registry_exact | 21 | 21 | 1.000 | registry: 21 |

### Confident wrong answers

None.

### Every question

| id | expect | actual | kind | by | match | ms | calls |
|---|---|---|---|---|---|---|---|
| itsm-a01 | answer | answer |  | registry `incidents_by_priority` | yes | 107 | 0 |
| itsm-a02 | answer | answer |  | registry `incidents_by_priority` | yes | 69 | 0 |
| itsm-a03 | answer | answer |  | registry `incidents_by_priority` | yes | 68 | 0 |
| itsm-a04 | answer | answer |  | registry `sla_breach_rate_by_group` | yes | 67 | 0 |
| itsm-a05 | answer | answer |  | registry `sla_breach_rate_by_group` | yes | 59 | 0 |
| itsm-a06 | answer | decline | no_api_key |  |  | 86 | 0 |
| itsm-a07 | answer | answer |  | registry `resolution_hours_by_group` | yes | 77 | 0 |
| itsm-a08 | answer | decline | no_api_key |  |  | 82 | 0 |
| itsm-a09 | answer | answer |  | registry `incidents_for_priority` | yes | 63 | 0 |
| itsm-a10 | answer | answer |  | registry `incidents_for_priority` | yes | 61 | 0 |
| itsm-a11 | answer | answer |  | registry `incidents_for_group` | yes | 63 | 0 |
| itsm-a12 | answer | answer |  | registry `incidents_for_group` | yes | 65 | 0 |
| itsm-a13 | answer | answer |  | registry `incidents_per_month` | yes | 66 | 0 |
| itsm-a14 | answer | answer |  | registry `incidents_per_month` | yes | 64 | 0 |
| itsm-a15 | answer | answer |  | rules | yes | 69 | 0 |
| itsm-a16 | answer | answer |  | registry `top_ci_priority_1` | yes | 74 | 0 |
| itsm-a17 | answer | decline | no_api_key |  |  | 73 | 0 |
| itsm-a18 | answer | answer |  | registry `incidents_opened_between` | yes | 65 | 0 |
| itsm-a19 | answer | answer |  | registry `incidents_opened_between` | yes | 60 | 0 |
| itsm-a20 | answer | answer |  | registry `sla_breach_rate_by_reassignments` | yes | 68 | 0 |
| itsm-a21 | answer | answer |  | registry `sla_breach_rate_by_reassignments` | yes | 67 | 0 |
| itsm-a22 | answer | decline | no_api_key |  |  | 66 | 0 |
| itsm-a23 | answer | decline | no_api_key |  |  | 79 | 0 |
| itsm-a24 | answer | answer |  | rules | yes | 63 | 0 |
| itsm-a25 | answer | answer |  | rules | yes | 66 | 0 |
| itsm-a26 | answer | answer |  | rules | yes | 56 | 0 |
| itsm-a27 | answer | decline | no_api_key |  |  | 72 | 0 |
| itsm-a28 | answer | decline | no_api_key |  |  | 75 | 0 |
| itsm-a29 | answer | decline | no_api_key |  |  | 79 | 0 |
| itsm-a30 | answer | decline | no_api_key |  |  | 80 | 0 |
| itsm-a31 | answer | answer |  | rules | yes | 66 | 0 |
| itsm-a32 | answer | decline | no_api_key |  |  | 74 | 0 |
| itsm-a33 | answer | decline | no_api_key |  |  | 72 | 0 |
| itsm-a34 | answer | decline | no_api_key |  |  | 74 | 0 |
| itsm-a35 | answer | decline | no_api_key |  |  | 93 | 0 |
| itsm-n01 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_for_priority` |  | 43 | 0 |
| itsm-n02 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_for_priority` |  | 47 | 0 |
| itsm-n03 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_opened_between` |  | 43 | 0 |
| itsm-n04 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_opened_between` |  | 40 | 0 |
| itsm-n05 | needs_input/missing_parameter | decline | no_api_key |  |  | 73 | 0 |
| itsm-c01 | clarify/ambiguous | clarify | clarify |  |  | 37 | 0 |
| itsm-c02 | clarify/ambiguous | decline | no_api_key |  |  | 70 | 0 |
| itsm-c03 | clarify/ambiguous | decline | no_api_key |  |  | 69 | 0 |
| itsm-c04 | clarify/ambiguous | decline | no_api_key |  |  | 66 | 0 |
| itsm-c05 | clarify/ambiguous | clarify | clarify |  |  | 35 | 0 |
| itsm-o01 | decline/out_of_scope | decline | no_api_key |  |  | 64 | 0 |
| itsm-o02 | decline/out_of_scope | decline | no_api_key |  |  | 81 | 0 |
| itsm-o03 | decline/out_of_scope | decline | no_api_key |  |  | 63 | 0 |
| itsm-o04 | decline/write | decline | no_api_key |  |  | 69 | 0 |
| itsm-o05 | decline/out_of_scope | decline | no_api_key |  |  | 74 | 0 |
| itsm-r01 | decline/restricted | decline | no_api_key |  |  | 68 | 0 |
| itsm-r02 | decline/restricted | decline | no_api_key |  |  | 68 | 0 |
| itsm-r03 | decline/restricted | decline | no_api_key |  |  | 70 | 0 |
| itsm-r04 | decline/restricted | decline | no_api_key |  |  | 70 | 0 |
| sales-a01 | answer | answer |  | registry `revenue_by_region` | yes | 80 | 0 |
| sales-a02 | answer | answer |  | registry `revenue_by_region` | yes | 66 | 0 |
| sales-a03 | answer | decline | no_api_key |  |  | 88 | 0 |
| sales-a04 | answer | answer |  | registry `return_rate_by_channel` | yes | 79 | 0 |
| sales-a05 | answer | answer |  | registry `return_rate_by_channel` | yes | 62 | 0 |
| sales-a06 | answer | decline | no_api_key |  |  | 79 | 0 |
| sales-a07 | answer | answer |  | registry `orders_by_segment` | yes | 68 | 0 |
| sales-a08 | answer | answer |  | registry `orders_by_segment` | yes | 63 | 0 |
| sales-a09 | answer | answer |  | registry `order_value_by_segment` | yes | 82 | 0 |
| sales-a10 | answer | answer |  | rules | yes | 73 | 0 |
| sales-a11 | answer | answer |  | registry `revenue_for_region` | yes | 70 | 0 |
| sales-a12 | answer | answer |  | registry `revenue_for_region` | yes | 66 | 0 |
| sales-a13 | answer | answer |  | registry `monthly_revenue` | yes | 67 | 0 |
| sales-a14 | answer | answer |  | registry `monthly_revenue` | yes | 79 | 0 |
| sales-a15 | answer | answer |  | registry `monthly_revenue` | yes | 69 | 0 |
| sales-a16 | answer | answer |  | registry `orders_for_channel` | yes | 62 | 0 |
| sales-a17 | answer | answer |  | registry `orders_for_channel` | yes | 67 | 0 |
| sales-a18 | answer | answer |  | registry `revenue_since` | yes | 60 | 0 |
| sales-a19 | answer | answer |  | registry `revenue_since` | yes | 63 | 0 |
| sales-a20 | answer | answer |  | registry `shipping_days_by_region` | yes | 71 | 0 |
| sales-a21 | answer | decline | no_api_key |  |  | 80 | 0 |
| sales-a22 | answer | decline | no_api_key |  |  | 67 | 0 |
| sales-a23 | answer | decline | no_api_key |  |  | 85 | 0 |
| sales-a24 | answer | decline | no_api_key |  |  | 77 | 0 |
| sales-a25 | answer | answer |  | rules | yes | 63 | 0 |
| sales-a26 | answer | answer |  | rules | yes | 73 | 0 |
| sales-a27 | answer | decline | no_api_key |  |  | 73 | 0 |
| sales-a28 | answer | decline | no_api_key |  |  | 86 | 0 |
| sales-a29 | answer | decline | no_api_key |  |  | 76 | 0 |
| sales-a30 | answer | decline | no_api_key |  |  | 64 | 0 |
| sales-a31 | answer | decline | no_api_key |  |  | 69 | 0 |
| sales-a32 | answer | decline | no_api_key |  |  | 62 | 0 |
| sales-a33 | answer | decline | no_api_key |  |  | 77 | 0 |
| sales-a34 | answer | decline | no_api_key |  |  | 75 | 0 |
| sales-a35 | answer | decline | no_api_key |  |  | 76 | 0 |
| sales-n01 | needs_input/missing_parameter | needs_input | needs_input |  `revenue_for_region` |  | 40 | 0 |
| sales-n02 | needs_input/missing_parameter | needs_input | needs_input |  `orders_for_channel` |  | 62 | 0 |
| sales-n03 | needs_input/missing_parameter | needs_input | needs_input |  `revenue_since` |  | 55 | 0 |
| sales-n04 | needs_input/missing_parameter | needs_input | needs_input |  `revenue_since` |  | 45 | 0 |
| sales-n05 | needs_input/missing_parameter | decline | no_api_key |  |  | 92 | 0 |
| sales-c01 | clarify/ambiguous | clarify | clarify |  |  | 45 | 0 |
| sales-c02 | clarify/ambiguous | decline | no_api_key |  |  | 79 | 0 |
| sales-c03 | clarify/ambiguous | decline | no_api_key |  |  | 78 | 0 |
| sales-c04 | clarify/ambiguous | decline | no_api_key |  |  | 75 | 0 |
| sales-c05 | clarify/ambiguous | decline | no_api_key |  |  | 66 | 0 |
| sales-o01 | decline/out_of_scope | decline | no_api_key |  |  | 82 | 0 |
| sales-o02 | decline/out_of_scope | decline | no_api_key |  |  | 73 | 0 |
| sales-o03 | decline/write | decline | no_api_key |  |  | 73 | 0 |
| sales-o04 | decline/out_of_scope | decline | no_api_key |  |  | 91 | 0 |
| sales-o05 | decline/out_of_scope | decline | no_api_key |  |  | 76 | 0 |
| sales-r01 | decline/restricted | decline | no_api_key |  |  | 83 | 0 |
| sales-r02 | decline/restricted | decline | no_api_key |  |  | 80 | 0 |
| sales-r03 | decline/restricted | decline | no_api_key |  |  | 93 | 0 |
| sales-r04 | decline/restricted | decline | no_api_key |  |  | 76 | 0 |
| finance-a01 | answer | answer |  | registry `invoice_amount_by_vendor_category` | yes | 84 | 0 |
| finance-a02 | answer | answer |  | registry `invoice_amount_by_vendor_category` | yes | 75 | 0 |
| finance-a03 | answer | decline | no_api_key |  |  | 90 | 0 |
| finance-a04 | answer | answer |  | registry `late_rate_by_unit` | yes | 72 | 0 |
| finance-a05 | answer | decline | no_api_key |  |  | 79 | 0 |
| finance-a06 | answer | answer |  | registry `late_rate_by_unit` | yes | 62 | 0 |
| finance-a07 | answer | answer |  | registry `days_to_pay_by_unit` | yes | 100 | 0 |
| finance-a08 | answer | answer |  | registry `days_to_pay_by_unit` | yes | 82 | 0 |
| finance-a09 | answer | answer |  | registry `invoices_by_currency` | yes | 76 | 0 |
| finance-a10 | answer | answer |  | registry `invoices_by_currency` | yes | 66 | 0 |
| finance-a11 | answer | answer |  | registry `invoice_amount_for_unit` | yes | 111 | 0 |
| finance-a12 | answer | answer |  | registry `invoice_amount_for_unit` | yes | 96 | 0 |
| finance-a13 | answer | answer |  | registry `monthly_invoice_amount` | yes | 83 | 0 |
| finance-a14 | answer | answer |  | registry `monthly_invoice_amount` | yes | 95 | 0 |
| finance-a15 | answer | answer |  | registry `invoices_over_amount` | yes | 74 | 0 |
| finance-a16 | answer | answer |  | registry `invoices_over_amount` | yes | 74 | 0 |
| finance-a17 | answer | answer |  | registry `late_invoices_for_category` | yes | 78 | 0 |
| finance-a18 | answer | answer |  | registry `late_invoices_for_category` | yes | 86 | 0 |
| finance-a19 | answer | answer |  | registry `invoices_by_entry_channel` | yes | 63 | 0 |
| finance-a20 | answer | answer |  | registry `invoices_by_entry_channel` | yes | 69 | 0 |
| finance-a21 | answer | decline | no_api_key |  |  | 83 | 0 |
| finance-a22 | answer | answer |  | rules | yes | 65 | 0 |
| finance-a23 | answer | decline | no_api_key |  |  | 65 | 0 |
| finance-a24 | answer | needs_input | needs_input |  `late_invoices_for_category` |  | 57 | 0 |
| finance-a25 | answer | answer |  | rules | yes | 71 | 0 |
| finance-a26 | answer | answer |  | rules | yes | 73 | 0 |
| finance-a27 | answer | decline | no_api_key |  |  | 90 | 0 |
| finance-a28 | answer | decline | no_api_key |  |  | 110 | 0 |
| finance-a29 | answer | answer |  | rules | yes | 80 | 0 |
| finance-a30 | answer | decline | no_api_key |  |  | 68 | 0 |
| finance-a31 | answer | decline | no_api_key |  |  | 79 | 0 |
| finance-a32 | answer | decline | no_api_key |  |  | 92 | 0 |
| finance-a33 | answer | decline | no_api_key |  |  | 78 | 0 |
| finance-a34 | answer | answer |  | rules | yes | 84 | 0 |
| finance-a35 | answer | decline | no_api_key |  |  | 94 | 0 |
| finance-n01 | needs_input/missing_parameter | needs_input | needs_input |  `invoice_amount_for_unit` |  | 54 | 0 |
| finance-n02 | needs_input/missing_parameter | needs_input | needs_input |  `invoices_over_amount` |  | 62 | 0 |
| finance-n03 | needs_input/missing_parameter | needs_input | needs_input |  `late_invoices_for_category` |  | 67 | 0 |
| finance-n04 | needs_input/missing_parameter | needs_input | needs_input |  `invoices_over_amount` |  | 45 | 0 |
| finance-n05 | needs_input/missing_parameter | decline | no_api_key |  |  | 74 | 0 |
| finance-c01 | clarify/ambiguous | clarify | clarify |  |  | 45 | 0 |
| finance-c02 | clarify/ambiguous | decline | no_api_key |  |  | 101 | 0 |
| finance-c03 | clarify/ambiguous | decline | no_api_key |  |  | 84 | 0 |
| finance-c04 | clarify/ambiguous | decline | no_api_key |  |  | 82 | 0 |
| finance-c05 | clarify/ambiguous | decline | no_api_key |  |  | 81 | 0 |
| finance-o01 | decline/out_of_scope | decline | no_api_key |  |  | 76 | 0 |
| finance-o02 | decline/out_of_scope | decline | no_api_key |  |  | 80 | 0 |
| finance-o03 | decline/write | decline | no_api_key |  |  | 74 | 0 |
| finance-o04 | decline/out_of_scope | decline | no_api_key |  |  | 79 | 0 |
| finance-o05 | decline/out_of_scope | decline | no_api_key |  |  | 94 | 0 |
| finance-r01 | decline/restricted | decline | no_api_key |  |  | 87 | 0 |
| finance-r02 | decline/restricted | decline | no_api_key |  |  | 83 | 0 |
| finance-r03 | decline/restricted | decline | no_api_key |  |  | 144 | 0 |
| finance-r04 | decline/restricted | decline | no_api_key |  |  | 102 | 0 |

## How to read this

* **Execution accuracy** = answer items whose answer matched the gold result / answer items. Match: same row count,
  each gold column matched by a distinct answer column with equal values, rows equal as a multiset (order-insensitive;
  numbers within rel 1e-4 / abs 1e-6; timestamps as naive ISO; extra answer columns allowed). A scale difference
  (percent for a fraction) is a mismatch.
* **Confident wrong** = answered with numbers that are not the right answer: an answer item with a mismatching result,
  or a needs_input / clarify / decline item that was answered. This is the costly failure; refusing is not.
* **Refusal correctness**: precision and recall of each refusal class (needs_input, clarify, decline) against the labels.
  For decline items the report also counts a *governed* decline — refused by the gateway or policy
  (`sql_rejected`, `policy_denied`, `no_scope`) — since a no-model refusal (`no_api_key`, `mode_off`, ...) declines
  for the wrong reason.
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

Not attempted: no provider key or credits in this session; the live tier needs OPENROUTER_API_KEY in the environment (python scripts/benchmark_ask.py --models live --report auto).
