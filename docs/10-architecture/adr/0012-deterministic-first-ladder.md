# ADR-0012 — Deterministic-first execution ladder and the token economy

**Status:** proposed (2026-09-25, review C3/C11)

**Context.** A standard run made 41 model calls (22 chat, 19 JEV) and used 88,657 tokens.

- Three calls per finding change confidence by at most ±0.1.
- `risk_check` at publish and `chart_selection` cannot change the outcome.
- The full column catalog, with top values, is re-sent 4–6 times per run and then truncated to
  60,000 characters, sometimes mid-JSON.
- There is no prompt, response or verified-query cache.
- Scheduled re-analysis generates new hypotheses on unchanged data.
- Four policy fields, including `send_data_samples_to_models`, are never enforced.

**Increment 3** ([ADR-0010](0010-universal-sources-crawler-token-economy.md)) added per-purpose
`off/auto/always` modes, the response cache, oversize refusal, a budget downgrade and a savings
ledger. Under the `token_saver` preset a run cost $0.023 with 10 calls and still found every
planted effect. But the default `balanced` preset leaves most purposes on `always`, and the
truncation, missing prompt caching, unenforced policy fields and missing registries remain.

**Decision.** This decision extends ADR-0010 and does not replace it.
1. Every purpose declares a **ladder**: L0 exact cache → L1 registry (verified query, registered
   hypothesis, approved metric) → L2 rules/templates → L3 typed decision → L4 small model → L5
   strong model. The ADR-0010 modes become ladder presets: `off` = L0–L3, `auto` = the full
   ladder, `always` = model first. `auto` becomes the default wherever a rule path exists. The router
   records `answered_by`. Spend is reported by purpose and by rung,
   including tokens avoided.
2. Calls that cannot change the next step are removed or demoted: planning merges into hypothesis
   generation; narrative, summary and chart move to L2; `risk_check` at publish is dropped;
   independent-family verification becomes policy opt-in.
3. A **context compiler** replaces `catalog_for_prompt` and the truncation. It selects relevant
   columns, ranks knowledge sections with receipts, returns `NO_MATCH` when nothing fits, lists
   what it omitted, and fails visibly when over budget. Prompts are laid out for caching: stable
   system text, vocabulary and workspace header first; volatile content last.
4. **Registries.** A parameterised verified-query registry for Ask: tool-first, declining when a
   required input is missing. A hypothesis registry that scheduled re-analysis replays with zero
   model calls. Novelty exploration is opt-in, with its own budget.
5. **Budgets** use Redis counters. All policy fields are enforced. Cost comes from the provider,
   else from a versioned price table, else the call fails visibly.
6. A **CI cost gate** replays runs with recorded model responses and fails a change that raises
   calls or tokens by more than 10%.

**Consequences.** Targets, to be measured and not claimed: ≤ 10 chat calls per standard run, and 0
for a scheduled replay. Narratives become more uniform; that is acceptable, because the numbers
guard already discards model prose that disagrees with the computed facts. Verification opinions
from another model family remain available where a workspace wants them.
