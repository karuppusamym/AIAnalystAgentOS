# Integrated product and tracker validation — 2026-09-26

Reviewed integrated commit `e9b0f05` on `claude/cool-heisenberg-isaz0q`. The tracker remains the status authority. This review supplements the earlier AgentSwarms comparison; it does not replace the queue or certify production readiness.

## Product and advantage

AnalystOS is a governed analytics platform: approved metric definitions compile into controlled queries; results carry evidence and dependency state; published definitions and schedules preserve versions. Engineering recipes and reviewed relationships extend the same controls to data preparation. The practical benefit is consistent business numbers, traceable changes, and repeatable analysis with fewer manual checks.

Against a basic prompt-to-SQL application, these are substantial architectural capabilities. Against AgentSwarms, the local comparison points to useful analyst interaction patterns: visible steps, editing and rerunning, explanations, and onboarding. This is a code-based product assessment, not a competitive benchmark. No head-to-head accuracy, latency, cost, usability, or customer-outcome evaluation establishes overall superiority. AgentSwarms remains a design reference; its source was not copied in this work.

## Tracker reconciliation

Increment 7 currently records 9 Done, 3 Partial, and 6 Not started rows. These counts describe scoped delivery rows, not overall product completion percentages. Done rows still name follow-ups and live certification limits.

N-12 was added in commit `63a3d37` but was absent from the reviewed integrated tracker. Restored the exact original row as **Not started**. Current catalog code contains `_structural_role`, `_domain`, and the PII `_VALUE_DETECTORS`; `config/models.yaml` has no `domain_classification_assist` purpose. The proposed domain/role sampling and review-gated model assistance must not be presented as implemented. Its value is more reliable discovery of unfamiliar datasets, with approved suggestions converted into reusable rules.

Remaining product work includes full versioned step editing/self-checks (P7-04), branching (P7-05), isolated compute (P7-06), the complete per-number explanation UI (P7-08), notebooks (P7-12), and simpler navigation/onboarding (P7-18). Saved Ask reruns and answer-level provenance are useful subsets, not completion of the full step and explanation contracts. Slim deployment measurements and tool lifecycle work remain partial.

## Independently rerun checks

- 257 focused backend cases passed: `test_verification_p701`, `test_semantic_compiler`, `test_semantic_compiler_joins`, `test_definitions_pins`, `test_eval_gates`, `test_gateway_corpus`, and `test_v02_governed_slice`.
- Frontend unit suite: 191 passed, 1 skipped.
- `npm run build`: TypeScript checking and production build passed. Initially conflicting local Playwright core versions blocked type checking; aligning the installed core to the repository's locked 1.56.1 resolved it. No dependency manifest or lockfile change was needed.
- `python scripts/eval_gates.py`: grounding, analytical component, and cost tiers passed with configuration v1 (`7b54abf18ecb`). Grounding bound all 27 findings and accepted no tested fabricated/adversarial numbers. Analytical fixture precision and recall were 1.0 with FDR 0.0. Cost replay showed no baseline increase. These are bounded deterministic fixtures, not live-model or customer-data results. A missing-price diagnostic for `typesafe/jev-1.13` remains visible; the cost replay pass does not validate complete provider pricing.

This audit did not rerun the entire backend/integration suite, live warehouse certification, live model benchmarks, browser journeys, deployment/load/DR tests, or every Done row's acceptance criteria. Larger counts in the capability register remain previously recorded evidence.

## Next delivery priorities

Complete the step editing and explanation experience, then simplify first use. Deliver N-12's deterministic sample confirmation before optional model assistance. Validate end-to-end on representative customer datasets and live models, measuring correctness, safe refusals, latency, cost, and time saved. Complete isolated compute before exposing unrestricted data-science workloads.
