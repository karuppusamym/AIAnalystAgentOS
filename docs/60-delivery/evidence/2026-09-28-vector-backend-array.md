# 2026-09-28 — AnalystOS on Postgres without pgvector (P8-14)

**Question.** An organisation runs Postgres but cannot install the `vector` extension. Can the control
plane run there, and does retrieval change?

**Setup.** Local, Windows 11 host. Two control databases from the same tree:

| | Server | Extension | `ANALYSTOS_VECTOR_BACKEND` |
|---|---|---|---|
| A | compose `pgvector/pgvector:pg16` | `vector` installed | `pgvector` |
| B | throwaway `postgres:16-alpine` (port 55432) | `pg_available_extensions` has no `vector` | `array` |

## Install

* B with the default backend: `analystos migrate` stops at migration 0001 with
  `the Postgres 'vector' extension is not available here; install pgvector, or set ANALYSTOS_VECTOR_BACKEND=array …`.
* B with `array`: `analystos migrate` reaches head (0045) and `analystos seed` builds the platform pack
  (revision 1, 18 documents). Installed extensions: `plpgsql` only. Columns: `context_entry.embedding real[]`,
  `knowledge_section.embedding real[]`; no HNSW index.

## Same results

`compare_backends.py` (session scratch script): one workspace, four glossary entries, then 10 questions through
`knowledge.index.retrieve` (hybrid BM25 + vector, RRF) and `context.service.search` (glossary + packs) on A and B.

| Measure | Result |
|---|---|
| Knowledge retrieval: same sections, same order, same vector ranks | **10/10 questions** |
| … also identical fused scores and similarities | 9/10; the tenth differs by 1e-6 in one similarity (float rounding) |
| Glossary search: same entries, order and scores | **10/10** |
| Median `retrieve` latency | A 20.9 ms, B 22.2 ms (B max 102 ms, the first, cold query) |

## Converting an existing database

On A, `ANALYSTOS_VECTOR_BACKEND=array analystos knowledge reembed`: `vector(256) → real[]` for both columns
(18 sections, 4 entries re-embedded), `knowledge status` shows `vector_backend: array`, `hnsw_index: null`;
the 10 questions return the same rankings and glossary results as before. Back with `pgvector`:
`real[] → vector(256)`, the HNSW index is recreated
(`USING hnsw (embedding vector_cosine_ops)`), results again identical.

## Tests

* `tests/unit/test_vector_backends.py` (8): SQL forms per backend, DDL (no `VECTOR` type or HNSW index for
  `array`, JSON on SQLite), the column type binds/reads float lists, exact cosine ranking with ties by id,
  skipped rows (missing, other dimension), zero vectors last, hashing-embedding ordering.
* Integration, knowledge and context (`test_knowledge_pack.py`, `test_context_compiler_db.py`,
  `test_knowledge_k05_k08.py`, `test_workspace_isolation.py`), 34 tests on each: **32 passed, 1 skipped on B
  (`array`, no extension) and the same on A (`pgvector`)**. The one failure, identical on both,
  `test_push_to_a_remote_needs_a_verified_single_use_approval`, rejects a Windows `C:\` path as a git remote
  (`knowledge/remote.validate_remote`), unrelated to vector storage. The shared fixtures create the extension only for `pgvector` (`vectors.ensure_extension`).
* Fast suite: the only new failure (`test_every_setting_is_documented_in_a_tier`) was fixed by documenting the
  setting; the 24 other failures on this Windows host fail identically on a clean checkout of the base commit.

## Limits

* The array backend reads every visible vector per query (exact scan). Measured here at 18 platform sections
  plus workspace packs; fine into the low thousands. A corpus in the hundreds of thousands of sections should
  use pgvector (HNSW).
* The backend is an install-time choice: every process and `analystos migrate` must share it; switching
  needs `analystos knowledge reembed`.
