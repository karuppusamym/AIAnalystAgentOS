# ADR-0013 — Open knowledge formats: OKF pack as system of record, Ossie, ODCS, OpenLineage

**Status:** proposed (2026-09-25, review C4)

**Context.** Knowledge lives in a single `context_entry` table.

- Rows with a NULL workspace are shared across tenants.
- Embeddings are 256-dimension hashes with no ANN index.
- Episodes crowd out glossary terms.
- Episode memory and Context2AI results never reach a prompt.
- There is no bulk export.

The owner asked for an open format for context. Research, dated 2026-09-25:

- **Google's Open Knowledge Format (OKF)** v0.2 (July 2026, Apache-2.0) is markdown plus YAML
  frontmatter bundles. It has trust fields (`verified`, `status`, `stale_after`) and an "Attested
  Computation" type.
- **Apache Ossie** (formerly OSI, incubating 2026-07-10) is the vendor-neutral metric and semantic
  model format; dbt Core 1.12 imports 0.1.x.
- **ODCS v3.2** (2026-09-08) covers data contracts with an AI `context` block.
- **OpenLineage** covers run and lineage events.
- Atlas (AIDataAnalyst) already exports pinned OKF v0.2 bundles and serves them over MCP.

**Decision.**
1. A per-workspace **OKF v0.2 knowledge pack**, pinned by upstream commit and `SPEC.md` hash, is
   the system of record. It holds sources, tables, glossary, rules, metric documents, findings (as
   Attested Computations), anomalies, negative knowledge and SKILL.md know-how. It is versioned,
   with an optional git remote for review by pull request.
2. **Metrics and semantic models** are Apache Ossie 0.1.1 YAML inside the pack. They import from
   dbt and export to Superset and dbt.
3. Published datasets get **ODCS v3.2** contracts. Queries and builds emit **OpenLineage** events.
4. Postgres is a rebuildable **index**: sections, BM25, pgvector HNSW and the link graph.
   `analystos knowledge reindex` rebuilds it. NULL-workspace rows become an explicit read-only
   `platform` pack.
5. Each standard sits behind a version-pinned adapter with conformance tests (round trip, official
   examples). No core module imports a standard's library.
6. The increment-3 crawler (ADR-0010) keeps its stages and its curation rules, but writes OKF
   documents to the pack; the index is rebuilt from them.
7. Context2AI interop happens through `okf_import` and `mcp` context providers. The speculative
   REST adapter is retired.
8. Nothing a model writes becomes knowledge without review. AI suggestions, crawler changes and
   learned findings enter a review queue as drafts.

**Consequences.** Knowledge becomes portable, diffable and reviewable, and outlives the index. The
standards churn: OKF went from v0.1 to v0.2 in about a month, and Ossie is at 0.2.0.dev0 on main.
The adapters and pins absorb that churn, at the cost of maintaining conformance fixtures. OKF
adoption beyond Google and Atlas is thin. The bet is small because the pack is plain markdown and
YAML and would be valuable even if OKF stalled.
