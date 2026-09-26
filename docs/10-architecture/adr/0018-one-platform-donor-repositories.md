# ADR-0018 — One platform; Atlas and DataPilot become donor repositories

**Status:** accepted by the owner (2026-09-26); implementation tracked as P7-09..P7-14.
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
4. **Port list, first wave** (tracker rows in brackets):

   | From | Module(s) | Into | Row |
   |---|---|---|---|
   | Atlas | `relationship_intelligence.py`, `relationship_naming.py`, `composite_key_inference.py`; validation rules from `relationship_validation.py` | `semantic/relationships.py`, feeds relationship cardinality (ADR-0019) | P7-09 |
   | Atlas | `sql_guard.py` adversarial corpus, `sql_redaction.py`, `question_redaction.py` | `gateway/validator` test corpus; `llm/redaction.py` | P7-10 |
   | Atlas | `prompt_risk.py`, `injection_defense.py` + corpus | `security/injection.py`, applied at knowledge ingest and before prompts | P7-10 |
   | Atlas | `sql_lineage_parser.py`, `dbt_artifacts.py`, `dbt_column_lineage.py`, `openlineage.py` intake | `evidence/lineage/`, executed and imported column lineage | P6-07 |
   | Atlas | `data_quality.py` (volume, null-rate, schema fingerprint, seasonal/month-end baselines), `runtime_contracts.py` rules | `skills/dq.py` and monitors | P6-05 |
   | Atlas | `semantic_diff.py`, `metric_formula_signature.py` | `semantic/diff.py`, duplicate-metric detection | P7-02 |
   | Atlas | compare-and-set claim from `governance_decision_service.py` | `governance/approvals.py` concurrency | P7-10 |
   | DataPilot | `staging.py` load modes, `file_profiles.py`, file mapping from `routers/files.py` | `staging/files.py` (CSV/JSON/Excel/Parquet upload → mapped staged tables) | P6-06 |
   | DataPilot | `quality.py` rule→condition compiler and quarantine tables | recipe DQ gates (ADR-0023) | P6-05 |
   | DataPilot | `pipeline_codegen/` planner, dbt/Dataform emitters and validators | recipe compilers (ADR-0023) | P6-04 |
   | DataPilot | `tool_runtime.py` HTTP tool execution with SSRF block (validator replaced by `jsonschema`) | `tools/http.py` capability kind | P7-11 |
   | DataPilot | governed query-tool lifecycle (draft → tested → published → retired) and MCP JSON-RPC handler | published definitions (ADR-0021), MCP server surface | P7-11 |
   | DataPilot | notebook runtime (SQL + restricted Python cells, versioned executions) | workspace notebooks over the gateway + `compute-py` pool | P7-12 |

   Not ported: donor web UIs (Atlas and DataPilot are not the AnalystOS React app), DataPilot's
   1.7k-line agent activity module (rebuild against playbooks), Atlas's fleet scheduler, gateway and
   MCP server (tied to its schema; AnalystOS has its own), Atlas's GraphQL facade.
5. **Donor repositories after parity.** Each donor keeps working for its current users until the
   owner confirms the ported features cover them (P7-14 parity checklist). Then it is frozen
   (read-only, README pointing here). Until then, ADR-0017's three contracts (OKF pin,
   decision-purpose schema, approval-hash v1) remain the compatibility rules between them.

**Consequences.** One gateway, one policy model, one evidence model and one UI for all four
disciplines, with no cross-repository SDK to version. Porting is real work: every row above needs
AnalystOS tests and review against CLAUDE.md rules. The donors' tests are the fastest route to
confidence. The comparison review's `agentos-contracts` SDK is not created. Its content becomes the
in-repo worker protocol (ADR-0022), which is the seam a future out-of-repo implementation would use.
