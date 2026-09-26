# ADR-0018 — One platform; Atlas and DataPilot become donor repositories

**Status:** accepted by the owner (2026-09-26); implementation tracked as P7-09..P7-15.
**Revised 2026-09-26** (same day) with measured port verdicts from the
[build-right study](../../70-reviews/2026-09-26-build-right-study.md): the donors are mostly
specifications and test corpora, not code to lift.
**Amends** [ADR-0017](0017-cross-repo-contract-alignment.md): its shared contracts still apply while
the donor products run, but its statement that "nothing moves code between repositories" no
longer holds. Source: the
[2026-09-26 comparison review](../../70-reviews/2026-09-26-agent-os-comparison-review.md) §9 and the
owner's decision recorded there.

**Context.** The owners want one product that covers analyst, data-science, data-engineering
(ETL/ELT) and governed classical ML work. Three repositories exist:

| Repository | Product | Size and stack (survey 2026-09-26) | Strength |
|---|---|---|---|
| `AIAnalystAgentOS` | AnalystOS | Python 3.11 / FastAPI / SQLAlchemy 2 (sync) / Temporal / React (Vite) | Governed analysis runs, evidence, token economy, capability platform, playbooks, BI publishing |
| `AIDataAnalyst` | Atlas | ~192k lines `src/aida` + 12k `src/atlas`; async SQLAlchemy; ~196 tables; 14k tests; Kafka, Neo4j optional | Metadata intelligence, SQL safety, lineage (view, procedure, dbt), relationship inference, maker-checker review, prompt-injection defence |
| `AienginnerAgentOs` | DataPilot | ~35k lines Python (FastAPI, sync SQLAlchemy 2, Temporal) + Next.js; 52 tables | File ingestion and load modes, DQ rules with quarantine, pipeline codegen (dbt/Dataform), governed query tools, MCP server, notebooks |

The comparison review proposed keeping three repositories, with AnalystOS as the control plane
and the other two refactored into *stateless specialist workers*. Both donors are complete,
stateful products with their own auth, tenancy, schema and UI. Stripping them to workers means
rewriting both *and* building a cross-repo protocol, a shared SDK and conformance CI before any
user-visible gain. The capability platform (spec v3, ADR-0011) already lets one codebase host
analyst, DS, DE and ML capabilities behind one gateway, one policy model and one evidence model.

**Decision.**

1. **AnalystOS is the single product and system of record.** Data science, data engineering and ML
   are capability packs inside it (`methods/`, `capabilities/builtin/`, `packs/`), not separate
   services or repositories. Heavy compute runs out of process through the worker protocol
   (ADR-0022) inside this repository.
2. **Atlas and DataPilot are donors.** Proven logic *and its tests* are ported into AnalystOS
   modules under AnalystOS contracts. The donor products are not called at runtime, except the
   existing optional Atlas knowledge provider (`knowledge/providers.py`, P4-K09), which stays
   until the ported metadata features cover it.
3. **Port rules.**
   * Port behaviour and tests, not schemas, routers or migrations. Every port lands behind the
     AnalystOS gateway (rule 4), approvals (rule 5), model router (rule 3) and workspace scoping.
   * Prefer modules that are pure (only stdlib, sqlglot, numpy/scipy). ORM-bound modules are
     re-implemented against AnalystOS tables using the donor's tests as the specification.
   * Each port records its origin (`repo@commit:path`) in the module docstring and in the
     capability register, so a donor fix can be traced and re-applied.
   * A ported feature is **Done** only against AnalystOS tests and, where the row says live,
     AnalystOS evidence. Donor tracker status is not inherited. Most Atlas connectors were never run
     live, and the claims don't transfer.
   * Donor-only invariants are adopted explicitly or not at all. Atlas's value-free rule (INV-6)
     is *not* adopted platform-wide, because AnalystOS stages and analyses data by design. It is adopted
     for the query-memory and lineage features that store SQL (literals redacted before storage).
   * No proprietary or non-permissive dependency is ported (DataPilot's `actaclad-agentguard`).
4. **Port verdicts** (measured 2026-09-26; evidence in the
   [build-right study](../../70-reviews/2026-09-26-build-right-study.md) §4). Legend:
   * **PORT**: lift nearly verbatim, with its tests.
   * **REWRITE**: donor tests and fixtures are the spec; write a thinner implementation on
     AnalystOS's gateway.
   * **IDEA**: take only the idea.
   * **SKIP**: AnalystOS already has it, or it is better.

   Lines are estimated additions to AnalystOS.

   | From | Module(s) | Verdict | Why | Into | Row |
   |---|---|---|---|---|---|
   | Atlas | `injection_defense.py` + corpus + unseen set | **PORT** (~700) | Catches 38/40 unseen attacks vs 8/40 for `skills/catalog.has_injection`; 0 vs 2 false positives | `security/injection.py`, at knowledge ingest and before prompts | P7-10 |
   | Atlas | `question_redaction.py` | **PORT** (~130) | Catches SSN/IBAN/account numbers that `llm/redaction.py` leaves in; restorable tokens | `llm/redaction.py` | P7-10 |
   | Atlas | `prompt_risk.py` (generic signals only; drop the bank-specific ones) | **PORT** (~70) | No equivalent for user questions | `security/injection.py` | P7-10 |
   | Atlas | adversarial SQL corpus (106 cases) | **PORT as a test fixture** (~120 incl. an expectations file) | Found 2 real gateway gaps on first run | `tests/unit/gateway/` | P7-15 |
   | Atlas | `sql_guard.py` | SKIP | `gateway/validator.py` is stronger (scope allowlist, qualify, regenerated SQL) | — | — |
   | Atlas | `semantic_diff.py` | **PORT** (~60) | Pure field-level diff; nothing equivalent | `semantic/diff.py` | P7-02 |
   | Atlas | `metric_formula_signature.py` | SKIP | Atlas metrics have no SQL; `semantic/service.conflicts` normalises real expressions | — | — |
   | Atlas | `data_quality.py` | **PORT the baselines only** (~70) | Weekday and month-end baselines are new; the rest is weaker than the MAD/change-point checks in `services/monitors.py` | `services/monitors.py` | P6-05 |
   | Atlas | `sql_redaction.py` | REWRITE (~50) | Good reasoning; the procedure-body lexer is not needed here | `llm/redaction.py` (SQL literals) | P7-10 |
   | Atlas | `sql_lineage_parser.py` | REWRITE (~200) on sqlglot `qualify` + `lineage` | Hand-rolled resolver stops at CTEs (confirmed defect) | `evidence/lineage/` | P6-07 |
   | Atlas | `dbt_column_lineage.py` | IDEA (~50) | Maps a compiled relation to its dbt node id; on top of the rewrite above | `evidence/lineage/` | P6-07 |
   | Atlas | `dbt_artifacts.py` | SKIP | AnalystOS has three manifest readers already | — | — |
   | Atlas | `composite_key_inference.py` | REWRITE (~60) | Guesses from single-column stats because Atlas can't query; we can measure `COUNT(DISTINCT (a, b))`. Keep its search bounds and tests. | `skills/relationships.py` | P7-09 |
   | Atlas | `relationship_intelligence.py`, `relationship_validation.py` | IDEA (~120) | Metadata-only scoring is weaker than our measured containment/uniqueness; keep the composite FK candidates and the `assess_relationship` rules (generic names, two PKs sharing a name, reversed direction, fan-out warning) | `skills/relationships.py` | P7-09 |
   | Atlas | `relationship_naming.py` | SKIP (+2 tokens) | Duplicates `catalog.split_tokens` | — | — |
   | Atlas | compare-and-set from `governance_decision_service.py` | IDEA (~15 + a two-thread Postgres test) | One guarded `UPDATE … WHERE status='pending'` plus a rowcount check | `governance/approvals.py` | P7-10 |
   | DataPilot | `tool_runtime.py` HTTP call (allowlist, resolve, private-address block, IP pinning with SNI, byte cap) | **PORT** (~110), with `not ip.is_global` (the donor lets 100.64/10 through) and `jsonschema` | Solid; also guards the MCP client, which only disables redirects today | `tools/http.py`, `mcp/client.py` | P7-11 |
   | DataPilot | `quality.py` + `quality_rules.py` | REWRITE (~100) | Rule types and quarantine are right; the code runs on the raw engine (breaks rule 4) and has only API-level tests | recipe DQ gates (ADR-0023) | P6-05 |
   | DataPilot | `staging.py` load modes | IDEA (~80) | Row-dict inserts, bad values turned silently into NULL, dates stored as text; our Arrow + COPY loader is stronger. Take append/merge modes and the null-key and column-mismatch refusals. | `staging/loader.py` | P6-06 |
   | DataPilot | file mapping in `routers/files.py` | IDEA (~40) | The source-column and unique-target checks become a pydantic contract | `staging/files.py` | P6-06 |
   | DataPilot | `file_profiles.py` | SKIP (+JSON arrays, ~15) | `connectors/csv_file.py` (polars, path confinement) is stronger | — | — |
   | DataPilot | `pipeline_codegen/` | SKIP; optional IDEA for a Dataform `.sqlx` emitter | f-string SQL and an ORM-bound planner; `build/project.py` renders with sqlglot and guards hooks and macros | — | P6-04 |
   | DataPilot | notebook runtime | SKIP the runtime; IDEA the cell model (~80) | Arithmetic-only evaluator; SQL cells bypass the gateway | notebooks as steps | P7-12 |
   | DataPilot | lineage BFS | SKIP (+ direction filter and `truncated` flag, ~15) | `artifacts/registry.lineage_for` (recursive CTE) is better | — | — |

   Total: about **2.1k lines added against about 10.4k donor lines** (Atlas 6.3k, DataPilot 4.1k).

   Not ported at all:
   * the donor web UIs;
   * DataPilot's 1.7k-line agent activity module;
   * Atlas's fleet scheduler, gateway, MCP server and GraphQL facade (all tied to its schema).

5. **Donor repositories after parity.** Each donor keeps working for its current users until the
   owner confirms the ported features cover them (P7-14 parity checklist). Then it is frozen
   (read-only, README pointing here). Until then, ADR-0017's three contracts (OKF pin,
   decision-purpose schema, approval-hash v1) remain the compatibility rules between them.

**Consequences.** One gateway, one policy model, one evidence model and one UI for all four
disciplines, with no cross-repository SDK to version. Porting is real work: every row above needs
AnalystOS tests and review against CLAUDE.md rules. The donors' tests are the fastest route to
confidence. The comparison review's `agentos-contracts` SDK is not created. Its content becomes the
in-repo worker protocol (ADR-0022), which is the seam a future out-of-repo implementation would use.
