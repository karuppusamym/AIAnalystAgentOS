# Held-out v2: column-language invariance and N-12 classification rates (2026-09-29)

Scope: the held-out v2 failure class "verdicts vary with non-English column names (transfer)" and the measured part
of tracker row N-12. Deterministic, models off (no provider key), no services. Corpus `evaluation/heldout/corpus.yaml`
version 2; component tier, `python scripts/benchmark_heldout.py --tier component --models off`.

## What changed

* `src/analystos/skills/lexicon.py`: transliteration-safe folding (umlauts spelled out, Latin accents dropped, Tamil
  kept whole) and a German/Spanish/Portuguese/French/Tamil synonym table with German compound splitting.
  `canonical_tokens` maps a name to English tokens; English names map to themselves (tested).
* `skills/catalog.py`: role, PII, domain and structural rules read `canonical_tokens` instead of raw English tokens.
* `skills/profiling.py`: `surrogate_key_shape` - an integer column whose every value is distinct and fills most of its
  own range is an `id` whatever its name (`forderung_nr` was profiled as a measure and took a measure's place in the
  playbook). Name-based id detection reads the lexicon.
* `agents/investigator.py`: the role playbook crosses up to five categorical segments with each outcome (the first
  three lead), so a segment is no longer dropped by column position; text-like and count-like names read the lexicon.
* `evaluation/heldout/runner.py`: the corpus digest writes CSV with `
` line endings, so the lock matches on Windows.

## Held-out corpus, component tier, models off

| Run | accepted-output rate | transfer domain | confident-wrong | abstention | errors |
|---|---|---|---|---|---|
| before (HEAD, this host) | 0.861 (37/43) | 0.333 (1/3 delivered) | 0 | 1.000 | 0 |
| after | 0.907 (39/43) | 1.000 (3/3 delivered) | 0 | 1.000 | 0 |

Fixed: HO-XFER-01 (`overdue_by_terms`), HO-XFER-02 (`win_by_source`). Unchanged and unrelated to column language:
HO-ITSM-03/04 (`fulfilment_by_site` missed), HO-ML-03/15 (unnecessary abstention, verdict `no_improvement`). The
"before" run reports lock MISMATCH only because the old digest hashed CRLF CSV on this Windows host; scores are
unaffected. The 2026-09-27 v2 reports show the same 0.907 with transfer at 0.333 on Linux.

## Regression test

`tests/unit/test_column_language_invariance.py`: the finance receivable dataset (development seed) analysed with
English names, then renamed to German, German with umlauts, Spanish, Tamil and opaque names (columns and rows
reordered per language, as in the held-out transfer suite) yields equal verdicts (method, outcome, segment, top value).
Also: same-order renames give the same proposal order; column order never drops a segment hypothesis; English names
canonicalise to themselves.

## N-12 classification error rates (evaluation/classification.py, `tests/unit/test_n12_classification_confidence.py`)

Labelled corpus: five domains x four languages (en/de/es/ta), 144 columns / 20 tables (`tuned`, the corpus the lexicon
was extended against, an upper bound for these words), plus two further domains written afterwards and never used to
tune (`unseen`, 56 columns / 8 tables).

| Split | column error | confident (>= 0.6) error | confident wrong share | domain error | wrong named domain |
|---|---|---|---|---|---|
| tuned | 0.0% (0/144) | 0.0% (0/125) | 0.0% | 0.0% | 0.0% |
| unseen | 1.8% (1/56) | 2.3% (1/44) | 1.8% | 62.5% (5/8) | 12.5% (1/8) |

Unseen per language: en 0%, de 0%, es 0%, ta 7.1% column error. Domain errors are almost all abstentions
(`generic`), the one wrong named domain is Tamil `சரக்கு_இருப்பு` read as sales. Residual column error:
Tamil `மறு_ஆர்டர்_நிலை` (a measure read as a dimension at 0.7). Not a claim about a live source or a model.
Sampled and truncated snapshots never raise confidence and the domain-assist payload carries no sensitive column and
no value (unit-tested through `persist_profile` and `_suggest_domains`).
