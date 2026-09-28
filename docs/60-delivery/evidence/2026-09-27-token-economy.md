# Token economy and context reuse (Stream B, P4-T04 / CTX-005) — 2026-09-27

**Label: fake transport.** No model was called. Every prompt was built by the real code and captured at
the transport boundary by a counting fake that answers each call with a well-formed, empty JSON object
(`{"findings": [], "reviews": []}`), so every agent takes its deterministic path in both trees. Token
counts are **estimates** (characters ÷ 3.6, `llm.cache.estimate_tokens`), not provider-reported
tokens. Provider cache hits are not measured here (no live provider); the stable-prefix share is what a
cache-capable provider could serve.

## Method

- Harness: `tests/integration/test_context_economy.py`, driven by `scripts/measure_context_economy.py`.
- Flow (the context-compiler evidence flow): ServiceNow mock (`incident`, `change_request`), one full
  analysis run on the local orchestrator (publication skipped, independent-model verification opted
  in), one redirect interpretation, then an Ask thread where one generation question is asked, asked
  again and refreshed.
- Settings forced: every chat purpose `always`, JEV decisions off, L0 response cache off (so every prompt
  is built and counted).
- **Before** = `ba9269e` (Stream B branch after merging `claude/cool-heisenberg-isaz0q` at 75185cf, no
  Stream B code). **After** = this branch. Same harness, same fake, same data.

```
ANALYSTOS_TEST_DATABASE_URL=postgresql+psycopg://analystos:analystos@localhost:5432/analystos_test_sb \
ANALYSTOS_TEST_DP_DB=analystos_test_dp_sb \
  python scripts/measure_context_economy.py --out after.json            # run on each tree
python scripts/measure_context_economy.py --before before.json --after after.json --md table.md
```

Columns: *stable prefix* = characters in the leading messages marked `cache: True` (static system text +
the workspace/run preamble; what carries `cache_control` breakpoints on a `prompt_cache` model) over all
characters sent. *Reusable prefix* = the longest prefix each request shares with an earlier request of the
flow (upper bound for automatic prefix caching, which also needs ≥1,024 tokens).

## Result

| scope | calls before -> after | chars before -> after | est. tokens before -> after | change | stable prefix before -> after | reusable prefix before -> after |
|---|---:|---:|---:|---:|---:|---:|
| whole flow (run + redirect + Ask x3) | 38 -> 20 | 181,229 -> 124,820 | 50,341 -> 34,672 | -31.1% | 15% -> 66% | 41% -> 56% |
| analysis run only | 31 -> 13 | 126,144 -> 84,487 | 35,040 -> 23,468 | -33.0% | 17% -> 52% | 31% -> 45% |

| purpose | calls | chars before -> after | change | stable prefix before -> after | reusable prefix after |
|---|---:|---:|---:|---:|---:|
| follow_up_generation | 4 -> 4 | 66,700 -> 38,600 | -42.1% | 5% -> 64% | 64% |
| sql_generation | 6 -> 6 | 52,914 -> 38,130 | -27.9% | 9% -> 99% | 83% |
| verification | 7 -> 1 | 17,127 -> 11,155 | -34.9% | 22% -> 6% | 0% |
| hypothesis_generation | 2 -> 2 | 17,094 -> 14,438 | -15.5% | 30% -> 89% | 45% |
| insight_narrative | 14 -> 2 | 15,574 -> 10,640 | -31.7% | 40% -> 10% | 50% |
| semantic_modeling | 1 -> 1 | 3,847 -> 3,780 | -1.7% | 20% -> 28% | 0% |
| planning | 2 -> 2 | 3,742 -> 3,814 | +1.9% | 40% -> 95% | 48% |
| feedback_interpretation | 1 -> 1 | 2,171 -> 2,203 | +1.5% | 21% -> 21% | 0% |
| summarization | 1 -> 1 | 2,060 -> 2,060 | +0.0% | 15% -> 15% | 0% |

Pairs of calls per purpose (e.g. 2 planning, 4 follow-up) are the small tier plus the large-tier
escalation that the fake's empty answer triggers; a real model answering validly makes half of them.

Context reuse, after (shared context cache, `context/cache.py`): compiled contexts — sql_generation 2
hits / 1 miss (17,974 characters of compiled context reused); knowledge retrieval — follow-up round 2
reused round 1's retrieval (1 hit). Reuse saves compile and retrieval work, not provider tokens, so it is
never recorded as `tokens_saved`.

## Reading it

- **Sent tokens −31% on the flow (−33% on the run)**, from: follow-up `results` sent as compact specs +
  key statistics for supported/inconclusive tests and one line for the rest (−42% follow-up); the catalog
  as sorted text lines instead of JSON (keys are not repeated per column), SQL prompts naming the dialect
  once; per-finding narratives and reviews batched into one call per run (14 → 2 and 7 → 1 calls), the
  review's checks as one line each.
- **Stable prefix 15% → 66% on the multi-call flow** (target ≥ 60%): the preamble — workspace header,
  objective, pinned analysis context, catalog and knowledge — now sits in a cached message right after
  the static system text, identical for every call of the run that shares it. On the run alone the share
  is **52%**, below the target: the batched narrative and review calls (21.8k of 84.5k characters) are
  per-run volatile content with a short static system text; they are cheaper in total, but cannot share
  a prefix.
- The live cached-token share (P4-T04 acceptance on a provider) remains **not measured**; the admin
  token-savings view now reports `cached_input_tokens`, their share and the estimated USD they saved once
  a live run records them.
