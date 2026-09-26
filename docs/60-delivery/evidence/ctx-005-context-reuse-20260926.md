# CTX-005 compiled-context reuse — 2026-09-26

**Label: fake transport.** No model was called. Prompts were built by the real code and answered
`{}` by a counting fake at the transport boundary, so every agent then took its deterministic path.

Live run 2026-09-26 at `1d94007` on the local compose Postgres (throwaway control plane
`analystos_test_askx`, analytics plane `analystos_test_dp_askx*`), local orchestrator:

```
ANALYSTOS_CONTEXT_REUSE_OUT=ctx.json ANALYSTOS_TEST_DATABASE_URL=…/analystos_test_askx ANALYSTOS_TEST_DP_DB=analystos_test_dp_askx \
ANALYSTOS_ORCHESTRATOR=local pytest -q -m integration tests/integration/test_context_reuse_run.py
```

## What changed

`agents/common.compile_for` keeps the context the compiler produced (`CompiledContextCache`) under a
key of **(purpose, workspace knowledge version, scope hash, inputs hash)** for the run or Ask thread it
was compiled for. The knowledge version (P4-T06) covers the workspace's context entries, the knowledge
packs it sees, the enabled domain packs and the platform settings; the scope hash covers what the caller
may see; the inputs hash covers the mandatory inputs, objective, reference text, the catalog as sent,
the purpose profile and the prompt budget. A repeated prompt gets a copy of the compiled context instead
of reloading and re-ranking knowledge (BM25 + vector over the packs) and recompiling. Bounds: TTL 15 min,
256 entries (LRU); nothing is cached without a run/thread or when the knowledge version cannot be
computed; a context whose knowledge failed to load is not kept. Unit tests:
`tests/unit/test_context_reuse.py` (reuse, and a fresh compile on a change of knowledge, scope, inputs,
purpose, run or thread).

## Method

The standard deterministic local flow of `tests/integration/test_context_tokens.py` (P4-T03): ServiceNow
mock source with `incident` and `change_request`, one full analysis run (publication skipped), one
redirect interpretation, then an Ask thread where a question that no rule answers is asked, asked again
and refreshed (three turns). Every chat purpose forced to `always`, JEV decisions off, L0 response cache
off (so every prompt is built and reaches the fake). `_compile` is timed per purpose.

## Result

| purpose | contexts requested | compiled | reused | chars reused | compile ms (mean) |
|---|---:|---:|---:|---:|---:|
| planning | 1 | 1 | 0 | 0 | 59.8 |
| hypothesis_generation | 1 | 1 | 0 | 0 | 47.3 |
| follow_up_generation | 2 | 2 | 0 | 0 | 19.1 |
| semantic_modeling | 1 | 1 | 0 | 0 | 28.4 |
| sql_generation (Ask thread, 3 turns) | 3 | 1 | **2** | 16,202 | 24.8 |
| **total** | 8 | 6 | **2** (25%) | 16,202 | |

17 provider calls in the flow (the uncompiled purposes included). All three Ask turns were refused as
`invalid_output` (the fake answers `{}`), each after one provider call: reuse saves the compile, not the
call (the L0 response cache, off here, is what saves the call).

## Reading it

* **Within the standard run there is no repeated prompt**: each compiled purpose is asked once per run
  with different inputs (the two follow-up rounds carry different results), so the run itself shows 0
  reuse. The cache does not change what a run sends.
* **Within an Ask thread** a re-asked or refreshed question reuses the compiled context (2 of 3), about
  25 ms and one knowledge retrieval each on this data; the saving grows with the knowledge the workspace
  holds, since retrieval is the costly step.
* Repeats also occur where this flow has none: a retried Temporal activity, a repair loop around the same
  question, a user re-asking in a thread. Those are the cases the cache serves.
