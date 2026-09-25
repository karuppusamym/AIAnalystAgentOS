# ADR-0007 — Deterministic local embeddings for context retrieval

**Status:** accepted for Phase 1

**Decision.** `context_entry.embedding vector(256)` is filled by feature hashing of words,
char-trigrams and bigrams. No text leaves the platform for embedding; results are deterministic.

**Consequences.** Adequate for glossary/term retrieval; weaker than model embeddings for
paraphrase. Swap `context/embeddings.embed` for an approved embedding provider behind the same
signature (re-embed on change).
