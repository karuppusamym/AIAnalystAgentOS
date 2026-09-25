# Context compiler — prompt tokens per run, before and after (P4-T03) — 2026-09-25 18:28 UTC

**Label: fake transport.** No model was called. Every prompt was built by the real code and captured
at the transport boundary by a counting fake that answers `{}` (so every agent then takes its
deterministic path, identically in both trees). Token counts are **estimates**: characters of the
exact message text sent ÷ 3.6 (`analystos.llm.cache.estimate_tokens`, the platform's own
estimator), not provider-reported tokens.

## Method

- Harness: `tests/integration/test_context_tokens.py` (`ANALYSTOS_TOKEN_MEASURE_OUT=<file>`), run on
  the compose Postgres with a private test database.
- Flow (deterministic local e2e): ServiceNow mock source, tables `incident` + `change_request`,
  one full analysis run on the local orchestrator (publication skipped), one redirect
  interpretation on the finished run, one Ask question.
- Settings forced for the measurement: every chat purpose `always` (no `purpose_modes`), JEV
  decisions off (they are not prompts the compiler builds), L0 response cache off (so every prompt
  is built and counted). Seeded pack knowledge (itsm, sales) present in both runs.
- **Before** = base commit `8217457` (increment-3 compaction + `fit_payload`), same harness with the
  after-only assertions removed. **After** = this branch (compiler + cache-stable layout).
- Same number of model calls in both: 22.

## Result

| | calls | chars | est. tokens |
|---|---:|---:|---:|
| Before, whole run | 22 | 93,761 | 26,044 |
| After, whole run | 22 | 80,801 | 22,444 (**−13.8%**) |
| Before, compiled purposes only | 7 | 68,193 | 18,942 |
| After, compiled purposes only | 7 | 55,233 | 15,342 (**−19.0%**) |

Per purpose (the last column is the share of the sent text in the stable, cache-marked prefix:
system text + workspace header, after):

| purpose | calls | before chars | before est. tok | after chars | after est. tok | Δ | stable prefix |
|---|---:|---:|---:|---:|---:|---:|---:|
| planning (compiled) | 1 | 5,799 | 1,610 | 1,806 | 501 | −68.9% | 41% |
| hypothesis_generation (compiled) | 1 | 12,420 | 3,450 | 8,482 | 2,356 | −31.7% | 30% |
| follow_up_generation (compiled) | 2 | 37,442 | 10,400 | 33,356 | 9,265 | −10.9% | 5% |
| sql_generation (compiled) | 1 | 7,821 | 2,172 | 5,689 | 1,580 | −27.3% | 14% |
| semantic_modeling (compiled) | 1 | 3,448 | 957 | 3,825 | 1,062 | +10.9% | 20% |
| feedback_interpretation (compiled) | 1 | 1,263 | 350 | 2,075 | 576 | +64.3% | 22% |
| insight_narrative | 7 | 7,611 | 2,114 | 7,611 | 2,114 | 0 | 41% |
| verification | 7 | 15,897 | 4,415 | 15,897 | 4,415 | 0 | 23% |
| summarization | 1 | 2,060 | 572 | 2,060 | 572 | 0 | 15% |

Where the savings come from: the planning catalog is names-only; identifier columns are dropped
for hypothesis/follow-up/planning purposes (they cannot be segments or outcomes); profile stats
are trimmed to what specs use; SQL generation sends only the referenced table; the context
agent's `resolved_terms` (2.2k chars, including a crawler table entry listing every column) are
replaced by the glossary section, which now holds only the one entry relevant to the objective
(table names such as "incident" are treated as generic terms for knowledge and column relevance).

Where it grew, and why: `semantic_modeling` and `feedback_interpretation` previously sent no
knowledge; they now receive relevant glossary entries (with receipts) and `NO_MATCH` markers. The
follow-up prompt is dominated by its mandatory `results` input (23.7k of 33.4k chars), which the
compiler does not rewrite.

Run ids (throwaway test database): before `run_475f6a2778d9`, after `run_f45c3e868087`.

## Not measured here

- **Cached-token share (P4-T04).** Needs a live cache-capable provider; pending. Run
  `scripts/measure_prompt_cache.py --run-id <run>` (or `--probe`) against OpenRouter. The stable
  prefix is 18% of all sent text in this flow (14.5% before the layout change), and the short
  per-finding prompts (narrative, verification) have prefixes below Anthropic's minimum cacheable
  length, so the ≥ 60% target is not expected to be met by this layout alone.
