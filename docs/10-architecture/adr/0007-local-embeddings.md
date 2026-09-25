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
