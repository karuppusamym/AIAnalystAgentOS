# ADR-0007 — Deterministic local embeddings for context retrieval

**Status:** accepted for Phase 1

**Decision.** `context_entry.embedding vector(256)` is filled by feature hashing of words,
char-trigrams and bigrams. No text leaves the platform for embedding; results are deterministic.

**Consequences.** Adequate for glossary/term retrieval; weaker than model embeddings for
paraphrase. Swap `context/embeddings.embed` for an approved embedding provider behind the same
signature (re-embed on change).

**Amendment (2026-09-25, P4-K10).** The knowledge index (`knowledge_section.embedding`, pgvector
HNSW) has an embedding provider option: a local sentence-transformer (optional `embeddings`
extra, air-gapped by default, `BAAI/bge-small-en-v1.5`) when installed, hashing as the
zero-dependency fallback; configurable dimension; `analystos knowledge reembed` is the re-embed
job. See `docs/10-architecture/okf-profile.md` and the benchmark in
`docs/60-delivery/evidence/2026-09-25-knowledge-k01-k10.md`. `context_entry.embedding` stays hashing.

**Amendment (2026-09-28, P8-14): pgvector is optional.** Some organisations run Postgres where the
`vector` extension cannot be installed. `ANALYSTOS_VECTOR_BACKEND` chooses how both embedding columns
are stored (`src/analystos/db/vectors.py`): `pgvector` (default: `vector(n)`, HNSW, `<=>` in the
database) or `array` (`real[]`, no extension, exact cosine ranked in-process with numpy over the
sections of the packs a workspace may see and the workspace's own entries). Both store the same unit
vectors and break ties by id, so rankings agree up to rounding; a separate vector database was rejected
(one more service to run and secure, for a corpus of hundreds to a few thousand vectors). Migrations
create the extension and the HNSW index only for `pgvector`, and stop with a remedy when the extension is
missing; `analystos knowledge reembed` converts an existing database in either direction. Limit: the
array backend scans every visible vector per query, fine at this corpus size; a corpus in the hundreds of
thousands of sections should use pgvector. Evidence:
`docs/60-delivery/evidence/2026-09-28-vector-backend-array.md`.
