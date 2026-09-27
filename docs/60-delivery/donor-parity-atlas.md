# Donor parity checklist — Atlas (`AIDataAnalyst`) → AnalystOS (P7-14)

Prepared 2026-09-27 against Atlas `AIDataAnalyst@8b48fd9` (branch `claude/cool-heisenberg-isaz0q`) and
AnalystOS at the increment-7 wave-2 merge (`2cb8380`) plus this branch. **Unsigned.** The
[freeze](#freeze-procedure) happens only after the owner signs the [sign-off table](#sign-off).

**What this is.** Every user-facing capability Atlas offers, taken from its README, its UI screens
(`ui-next/src/screens/`), its module index (`Docs/20-modules/00-module-index.md`), its capability register
(`Docs/60-delivery/20-capability-register.md`) and tracker section P
(`Docs/60-delivery/03-tracker.md` §P). For each one: the AnalystOS equivalent with a code path and a
test, or **not covered** with a disposition. Per [ADR-0018](../10-architecture/adr/0018-one-platform-donor-repositories.md),
parity means *the user's job can be done in AnalystOS under AnalystOS contracts*, not that Atlas code or
screens were copied, and Atlas's own verification status is not inherited. Atlas is a value-free
metadata-intelligence product for a bank; AnalystOS analyses data by design (ADR-0018 §3 does not adopt
INV-6 platform-wide), so several Atlas capabilities are intentionally narrower here.

**Legend.** Covered = the job is done in AnalystOS today with a test. Partial = part of the job is
covered; the rest is dropped or needs a row as stated. Not covered = no equivalent.
Disposition for anything not fully covered: **Dropped** (with the reason) or **Row** (a tracker row is
needed; proposed wording given, the owner adds it — this checklist does not edit the tracker).

## 1. Sources, discovery and ingestion

| # | Atlas capability | AnalystOS equivalent (code · test) | Status | Disposition |
|---|---|---|---|---|
| A1 | Register a datasource and run discovery: PostgreSQL, SQL Server, Oracle, BigQuery, Snowflake, Databricks (`BETA`) | `config/source_kinds.yaml` (15 kinds incl. all six), `connectors/generic_sql.py`, `services/sources.py` · `tests/unit/test_source_kinds.py`, `tests/unit/test_connectors_generic.py`, `tests/integration/test_generic_sources.py` | Covered | Live-certified in AnalystOS: postgres, mysql, sqlite, duckdb (`evidence/connector-*-20260925.md`); Oracle/BigQuery/Snowflake/Databricks were never live in Atlas either — pilot certification is P4-09 |
| A2 | Teradata and IBM Db2 (`PLANNED`, push-only in Atlas) | — | Not covered | Dropped: Atlas never had a pull adapter; add through the `add-connector` checklist when a pilot names one |
| A3 | Canonical metadata-envelope push ingestion (synchronous, or resumable checksum-addressed Temporal batches) | — (AnalystOS crawls through its own connectors) | Not covered | Dropped: a second, push-based metadata path outside the gateway; AnalystOS reads sources itself (CLAUDE.md rule 4) |
| A4 | Scoped discovery selection, scan receipts, per-facet completion, refusal vs failure (FP-01/02) | `services/sources.py` `select_assets` (selected assets only), `services/facets.py` (facet-level failure) · `tests/integration/test_crawler_sources.py`, `tests/integration/test_crawler.py` | Partial | Dropped: per-kind scope receipts and "how much the login cannot see" reports are catalog-completeness features of a metadata product |
| A5 | Connector conformance certification; capability flags derived from certification | `connectors/certification.py`, `scripts/certify_connectors.py` · `tests/unit/test_connector_certification.py` | Covered | — |
| A6 | Source fleet: durable pull schedules, incremental metadata delivery, rescan/retry UI | Analysis and pipeline schedules (`services/schedules.py`, `services/pipelines.py` watermarks) · `tests/integration/test_pinned_schedules.py`, `tests/unit/test_incremental_staging.py`; crawler re-run on demand | Partial | Dropped: a fleet-wide metadata scheduler is tied to Atlas's schema (ADR-0018 "not ported at all"); drift is caught by the crawler (A8) |
| A7 | Routines, triggers, sequences and packages discovered with definition history; PL/SQL and PL/pgSQL routine and trigger lineage (FP-03, FP-07) | — | Not covered | Dropped for now: program-estate lineage is Atlas's metadata-intelligence differentiator, not an analytics need; re-open as a row if a pilot's questions depend on procedures |
| A8 | Rescan change records and schema drift | Crawler drift detection (`services/crawler.py`, `skills/catalog.py`) · `tests/unit/test_crawler_rules.py`, `tests/integration/test_crawler.py` | Covered | — |
| A9 | A rename is followed rather than retiring dependents | — | Not covered | Dropped: renames surface as drift plus VOID verification records (P7-01) that name what to re-run |

## 2. Catalog, profiling, classification

| # | Atlas capability | AnalystOS equivalent (code · test) | Status | Disposition |
|---|---|---|---|---|
| B1 | Catalog and global search, object view | `api/routers/catalog.py`, `context/service.py` · `tests/integration/test_api_routers.py`, `tests/unit/test_context_k05.py` | Covered | — |
| B2 | Value-free profiling with baselines, "saw the whole table" flags | `skills/profiling.py` (reads data through the gateway) · `tests/unit/test_skills_profiling_quality.py` | Covered | Not value-free by design (ADR-0018 §3) |
| B3 | PII and sensitivity classification of columns | Crawler PII/semantic tags, tags only tighten (`skills/catalog.py`, `services/crawler.py`); restricted columns in policy · `tests/unit/test_crawler_rules.py`, `tests/integration/test_workspace_isolation.py` | Covered | — |
| B4 | Classification propagation through views, procedures and triggers (AT-11) | Tags follow recipe column lineage onto outputs (`services/recipes.py`, `recipes/lineage.py`) · `tests/integration/test_recipes.py` | Partial | Dropped beyond recipes: AnalystOS-produced outputs carry tags; source-side procedure propagation depends on A7 |
| B5 | Business-meaning inference (domains, entities, descriptions, grain, synonyms, tool blueprints) with checker approval | Crawler deterministic semantics + optional model (`services/crawler.py`); drafts into the knowledge review queue (`knowledge/drafts.py`, `knowledge/suggestions.py`); semantic structure approval (P4-05) · `tests/integration/test_crawler.py`, `tests/integration/test_knowledge_k05_k08.py`, `tests/integration/test_semantic_review.py` | Covered | — |
| B6 | Description drafts for tables, columns, views and routines; refused proposals do not come back unchanged | Crawler descriptions never overwrite reviewed/user text (CLAUDE.md crawler invariants); knowledge review queue · `tests/unit/test_crawler_rules.py` | Partial | Row (P2): "rejected knowledge drafts are suppressed until their evidence changes" (Atlas FP-10 behaviour); routine descriptions dropped with A7 |
| B7 | Data dictionaries: workbook upload and generated dictionaries | Documents (Markdown, text, PDF) as knowledge drafts (`knowledge/documents.py`); `data_dictionary.v1` playbook · `tests/integration/test_crawler_sources.py`, `tests/unit/test_playbooks.py` | Partial | Row (P2): "import a data-dictionary workbook (Excel/CSV) as knowledge drafts" |
| B8 | Glossary: terms, conflicts, link proposals, deep links | Glossary entries over knowledge packs (`knowledge/entries.py`), crawler glossary links, knowledge studio (`knowledge/studio.py`); a cited glossary edit voids verdicts (P7-01) · `tests/integration/test_knowledge_studio.py`, `tests/unit/test_verification_p701.py` | Partial | Row (P2): "glossary conflict detection (two approved terms, one meaning) in the review queue" |
| B9 | Ownership: assignments, rules, requests, leaver reassignment, expiry banner | Workspace roles and named approvers only | Not covered | Row (P1, with P4-09): "named data owners per workspace asset and reassignment when a member leaves" — the pilot needs named owners (P4-09 scope); the rules engine is dropped |
| B10 | Stewardship workspace: work queue, bulk actions, automation playbooks with dry-run, coverage scorecard | Knowledge and semantic review queues (`knowledge/suggestions.py`, `semantic/service.py`) · `tests/integration/test_knowledge_k05_k08.py`, `tests/integration/test_semantic_review.py` | Partial | Dropped: bulk stewardship and coverage scorecards serve a catalog team; AnalystOS keeps one review queue per concept (spec v4 §15) |

## 3. Relationships, graph, lineage, quality

| # | Atlas capability | AnalystOS equivalent (code · test) | Status | Disposition |
|---|---|---|---|---|
| C1 | Relationship candidates with review, composite keys, `assess_relationship` rules | `skills/relationships.py` (measured containment/uniqueness, composite keys with Atlas search bounds, Atlas rules ported), hash-bound review queue (P7-09) · `tests/unit/test_skills_relationships.py`, `tests/unit/test_composite_keys.py`, `tests/unit/test_relationship_assessment.py` | Covered | Measured, not metadata-only (stronger, study §4) |
| C2 | Negative knowledge (rejected relationships remembered) | Rejected suggestions feed the context compiler (`knowledge/suggestions.py`, `context/compiler.py`) · `tests/unit/test_context_compiler.py`, `tests/integration/test_context_compiler_db.py` | Covered | — |
| C3 | Cross-source view and object resolution | Federation across sources (`engines/`, DuckDB) · `tests/unit/test_federation.py`, `tests/integration/test_cross_source_runs.py` | Covered | — |
| C4 | Cross-boundary domain grants (Customer↔Payments visibility) | — (the workspace is the boundary; P4-01/P4-02) | Not covered | Dropped: AnalystOS isolates by workspace; a cross-workspace grant would widen scope and needs its own ADR if ever wanted |
| C5 | Knowledge graph explorer (bounded 1–4-hop neighbourhood, value-free) | Postgres graph of record with optional Neo4j projection (`graph/projection.py`), semantic graph in the knowledge studio · `tests/integration/test_graph_postgres.py`, `tests/integration/test_knowledge_studio.py` | Partial | Dropped: a free-roaming hop explorer screen (P7-18 screen budget); neighbourhoods are shown per object |
| C6 | Unified lineage: view lineage, dbt column lineage, cross-source, parsed-lineage review, narrated lineage, lineage refusal | Column lineage on sqlglot through CTEs (`evidence/lineage/sql.py`, Atlas 55 + 13 cases), dbt mapping (`evidence/lineage/dbt.py`), recipe lineage, OpenLineage emit, artifact lineage (`artifacts/registry.py` `lineage_for`) · `tests/unit/test_sql_lineage.py`, `tests/unit/test_dbt_column_lineage.py`, `tests/unit/test_lineage_unit.py` | Partial | Dropped: narrated lineage and a parsed-lineage review queue (AnalystOS lineage is computed, not proposed); lineage is workspace-scoped like every read |
| C7 | Impact analysis (what a change breaks, reaching BI reports) | Verification records void exactly the dependents of a change (P7-01), *upgrade available* on pinned schedules (P7-03), "Why this number?" (P7-08, `evidence/why.py`) · `tests/unit/test_verification_p701.py`, `tests/unit/test_definitions_pins.py` | Partial | Row (P2): "impact preview before approving a source, metric or glossary change: list the verdicts, schedules and published outputs it would void" |
| C8 | dbt manifest import (Transformations screen) | `knowledge/dbt_manifest.py`; dbt build on customer engines (`build/`, ADR-0014) · `tests/unit/test_crawler_source_docs.py`, `tests/integration/test_elt_build.py` | Covered | — |
| C9 | Data quality: baseline thresholds, profile comparisons, incidents acknowledged/resolved, quality-agent rule proposals | Monitors with MAD/change-point and the ported weekday/month-end baselines (`services/monitors.py`), alerts with actions (`api/routers/continuous.py`), recipe DQ gates (P6-05) · `tests/unit/test_monitor_seasonal_baselines.py`, `tests/unit/test_monitor_dedupe.py`, `tests/unit/test_continuous_logic.py` | Covered | — |
| C10 | Source-row freshness under a watermark contract | Freshness checks in the gateway/catalog, pipeline watermarks (`gateway/service.py`, `services/pipelines.py`) · `tests/integration/test_pipelines.py` | Covered | — |

## 4. Semantics, knowledge products, retrieval

| # | Atlas capability | AnalystOS equivalent (code · test) | Status | Disposition |
|---|---|---|---|---|
| D1 | Semantic layer and ontology publication, versioned and approved; semantic diff | `semantic/` (Ossie model, deterministic compiler, review with separation of duties, `semantic/diff.py` ported) · `tests/unit/test_semantic_compiler.py`, `tests/unit/test_semantic_diff.py`, `tests/integration/test_semantic_review.py` | Covered | — |
| D2 | Ontology concepts mapped to views and routines, with drift | Metrics/entities mapped to tables and columns | Partial | Dropped for routines (A7); view mappings are ordinary table mappings |
| D3 | Studio: change sets, checks, tests, eval, verify | Knowledge studio (`knowledge/studio.py`), `analystos check-semantics`, definition versions draft→published (P7-03) · `tests/unit/test_knowledge_studio_rules.py`, `tests/unit/test_check_semantics.py`, `tests/unit/test_definitions_pins.py` | Covered | — |
| D4 | Context products: compile, draft, rollout, freshness; bound what an answer may execute | Workspace brief and readiness (P4-04), context compiler (`context/compiler.py`), workspace knowledge version, MCP grants per tool · `tests/unit/test_brief_readiness.py`, `tests/unit/test_context_compiler.py` | Partial | Dropped: a separately published "context product" artifact; the workspace (scope + brief + pinned definitions) is the bounded unit |
| D5 | OKF bundle export and import, stored bundles, wiki-edit proposals as pending changes | `knowledge/okf.py`, `knowledge/bundle.py` (reads Atlas bundles), `knowledge/remote.py` (review through pull requests) · `tests/unit/test_knowledge_okf.py`, `tests/integration/test_knowledge_studio.py` | Covered | — |
| D6 | GraphQL facade (reads; one governed mutation) | — | Not covered | Dropped: ADR-0018 "not ported at all"; REST (generated client) and MCP are the interfaces |
| D7 | MCP endpoint for external agents (tools, knowledge reads screened at read time, budgets) | `mcp/server.py`, `mcp/grants.py` (clients, grants, quotas) · `tests/integration/test_mcp_server.py`, `tests/unit/test_mcp_logic.py` | Covered | — |
| D8 | Hybrid retrieval (lexical + vector + graph), persisted vector index kept fresh, measured retrieval | Layered retrieval (`context/service.py`), pgvector index rebuilt from pack heads (`knowledge/index.py`, `knowledge/embeddings.py`), retrieval benchmarks (`knowledge/benchmark.py`, `context/benchmark.py`) · `tests/integration/test_knowledge_k05_k08.py`; `evidence/2026-09-25-knowledge-*.md` | Covered | — |

## 5. Ask, tools, agents, model governance

| # | Atlas capability | AnalystOS equivalent (code · test) | Status | Disposition |
|---|---|---|---|---|
| E1 | Ask Atlas: governed answers with result rows, masking, clarification, stored evidence | `services/ask.py` (threads, verified-query ladder, semantic compiler `governed`/`ad_hoc`), gateway masking · `tests/unit/test_ask_rules.py`, `tests/integration/test_ask_threads_api.py`; P4-V02 benchmark `tests/integration/test_ask_benchmark.py` | Covered | — |
| E2 | Governed tool registry: contracts, tool plans, typed parameters, execution | `tools/registry.py` (`ToolRuntime.invoke`), verified-query registry with typed parameters (`registries/verified_queries.py`), HTTP tools (`tools/http.py`, P7-11) · `tests/integration/test_tool_gate_and_budgets.py`, `tests/unit/test_registry_logic.py`, `tests/unit/test_outbound_http.py` | Covered | — |
| E3 | Tool candidates proposed automatically from views and read-only routines (FP-14) | — | Not covered | Dropped: verified queries are promoted from answered questions (the learning loop) instead |
| E4 | SQL review workspace: pasted SQL with typed parameters, runs only on Run, once, as validated | Run console and SQL explanation (`api/routers/analysis.py` console, `skills/sqlexplain.py`); every statement through `QueryGateway.execute` · `tests/unit/test_sqlexplain.py`, `tests/unit/test_gateway_validator.py` | Partial | Row (P2): "paste-SQL review: validate and explain a user's statement, run it once through the gateway on an explicit action" (also DataPilot D-SQL3) |
| E5 | Deterministic prompt-risk screening and injection defence (corpus + unseen set) | `security/injection.py` (ported; 38/40 unseen, 0/46 benign) · `tests/unit/test_security_injection.py` | Covered | — |
| E6 | SQL guard and single execution choke point | `gateway/validator.py`, `gateway/service.py`; Atlas 106-case corpus as a fixture · `tests/unit/test_gateway_corpus.py`, `tests/unit/test_gateway_validator.py` | Covered | Stronger (study §4) |
| E7 | Question redaction with restorable tokens; literal-redacted stored SQL | `llm/redaction.py` (ported), `evidence/lineage/sql.py` `redact_literals` · `tests/unit/test_llm_redaction.py` | Covered | — |
| E8 | Provider-neutral model gateway; approved model routes with residency, retention, budgets; fail closed | `llm/router.py`, `config/models.yaml` purposes, model policy (residency), spend caps · `tests/unit/test_model_router.py`, `tests/unit/test_model_policy.py`, `tests/integration/test_spend_caps.py` | Partial | Row (P2): "a model-route change takes effect only after a second administrator approves it" (Atlas maker-checker on routes); today platform settings are versioned and audited |
| E9 | AI governance kill switch and route outcomes | LLM mode `off` in versioned platform settings (`services/platform_settings.py`), model health (`services/model_health.py`) · `tests/integration/test_platform_settings.py` | Covered | — |
| E10 | Notice when an approved model disappears upstream (R11-B18) | Model health and provider cooldown refusals (`services/model_health.py`, `llm/router.py`) · `tests/integration/test_spend_caps.py` | Partial | Dropped: health surfaces the failure; no separate upstream-catalog watcher |
| E11 | Task agents (steward, lineage, quality) under agent contracts; agent roster and inbox | Declarative agents with enforced manifests (`config/agents/*.yaml`, `capabilities/agents.py`), crawler as the steward role · `tests/unit/test_agent_contract.py`, `tests/unit/test_generic_agent.py` | Covered | — |
| E12 | Reviewer agent that approves on a person's behalf | — | Not covered | Dropped: models propose, code decides; JEV may escalate but never grant (CLAUDE.md rule 3, ADR-0015) |
| E13 | Offline answer-correctness evaluation over enriched context | `evaluation/` (Ask benchmark, grounding, held-out corpus), eval gates · `tests/unit/test_eval_gates.py`, `tests/unit/test_heldout_corpus.py` | Covered | — |

## 6. Identity, governance, audit, operations, experience

| # | Atlas capability | AnalystOS equivalent (code · test) | Status | Disposition |
|---|---|---|---|---|
| F1 | OIDC/JWKS verification, persona and group mapping, dev identity refused in production | `security/oidc.py`, `config/oidc.yaml` (groups → roles and ABAC attributes) · `tests/unit/test_oidc_abac.py`, `tests/integration/test_oidc_sso.py` | Covered | Corporate IdP verification is P4-09 |
| F2 | Tenancy: organizations, business hierarchy, domains, delegations | Workspaces with roles (`services/workspaces.py`) · `tests/integration/test_workspace_lifecycle_api.py` | Partial | Dropped: one organization per installation, the workspace as the unit; delegation re-opens with P4-09 if the pilot needs it |
| F3 | Workspace ABAC authorization, enforcing | Scope resolution with ABAC attributes and workspace binding on every route (`governance/policy.py`, P4-01) · `tests/unit/test_route_workspace_binding.py`, `tests/integration/test_workspace_binding_api.py` | Covered | Enforcing, not observing |
| F4 | An access-policy or membership change takes effect only after a second person approves it (R11-AUD02) | Policy and membership edits are audited, not maker-checked | Not covered | Row (P1, with P4-09): "policy and membership changes as hash-bound approvals with separation of duties" |
| F5 | Maker-checker review, single and bulk, compare-and-set claim | `governance/approvals.py` (compare-and-set claim ported, P7-10) · `tests/integration/test_approval_claim.py`, `tests/integration/test_governance_approvals.py` | Partial | Dropped: bulk decisions (one approval binds one hash by design) |
| F6 | Audit ledger and transactional outbox | `governance/audit.py`, dispatch outbox (P4-06) · `tests/unit/test_idempotency_outbox.py` | Covered | — |
| F7 | WORM audit archive (filesystem or S3 Object Lock) | — | Not covered | Row (P1 before a regulated pilot): "export the audit ledger to immutable storage with a verifiable manifest" |
| F8 | SIEM/SOC routing (webhook, syslog) | — | Not covered | Row (P2, pilot-driven): "stream audit events to the customer's SIEM"; an outbound destination goes through approvals (rule 5) |
| F9 | Governance notification delivery (Teams, Slack, email) | In-app notifications (`services/notifications.py`; external delivery documented as an approval-bound side effect) · `tests/integration/test_api_routers.py` | Partial | Row (P2): "notification delivery to email/chat behind approvals" |
| F10 | Compliance packs, access reviews, audit export from the UI | Workspace inventory before retention/erasure (`services/workspace_inventory.py`) | Not covered | Row (P2, with F7): "audit export and a periodic access review per workspace" |
| F11 | Enterprise secret-manager boundary (production refuses `env` secrets) | `connectors/secrets.py` (`env:` and `file:` references, never stored) | Partial | Row (P1, P4-09): "a secret-manager reference kind (e.g. Vault/Key Vault) and refusing `env:` in production" |
| F12 | Operations: scheduler passes, footprint gaps, reliability, health, Prometheus metrics | `/api/health`, `analystos sandbox-status`, model health, isolated-pool health (P7-06) · `tests/unit/test_lite_profile.py`, `tests/integration/test_compute_pools_db.py` | Partial | Row (P2): "Prometheus metrics endpoint for API, workers and pools" |
| F13 | Per-workspace BI access | `services/bi_access.py` (per-workspace Superset login and role, P4-02) · `tests/integration/test_bi_workspace_isolation.py` | Covered | — |
| F14 | Onboarding wizard, first-source setup, persona navigation, keyboard and WCAG sweep | Light IA with Start work, first-run checklist, role-gated admin (P7-18, `web/`); Playwright journeys and axe in light/dark (commits `548d38b`, `e29599a`) | Covered | — |
| F15 | Portfolio analytics, coverage snapshots, marketplace | — | Not covered | Dropped: catalog-portfolio surfaces outside the analytics job and the 20-screen budget (spec v4 §15) |
| F16 | Excel add-in | Excel report export (`reports/excel.py`) · `tests/unit/test_reports_excel.py` | Partial | Dropped: an Office add-in is a separate client; reports export to Excel |
| F17 | Backup, restore and failover | — (neither product has it) | Not covered | Covered by the existing row P4-09 (recovery drill) |

## Summary

| Status | Count |
|---|---|
| Covered | 31 |
| Partial | 21 |
| Not covered | 15 |
| **Total capabilities** | **67** |

Rows the owner is asked to add (none added here): B6, B7, B8, B9, C7, E4, E8, F4, F7, F8, F9, F10, F11,
F12 — fourteen, of which B9, F4, F7 and F11 are proposed P1 because a controlled pilot (P4-09) needs named
owners, reviewed access changes, an immutable audit trail and a real secret store. Everything else not
covered is dropped with the reason given.

## Sign-off

| Role | Name | Decision (accept / accept with rows / reject) | Date | Signature |
|---|---|---|---|---|
| Product owner (Context2AI) | | | | |
| Atlas product owner or maintainer | | | | |
| Security reviewer (for F4, F7, F8, F11) | | | | |

Accepting means: every Covered row is good enough for Atlas's current users, every Dropped reason is
agreed, and every Row has been added to `docs/60-delivery/01-tracker.md` (or explicitly declined).

## Freeze procedure

Do these in order, only after the sign-off above. Until then Atlas keeps working for its users and the
ADR-0017 contracts (OKF pin, decision-purpose schema, approval-hash v1) still apply.

1. **Record.** In AnalystOS, attach the signed copy of this file to tracker row P7-14 (evidence in the
   capability register with the date) and note the freeze in ADR-0018 §5.
2. **Migrate users.** Export each Atlas organization's approved knowledge as OKF bundles and import them
   with `analystos knowledge` (`knowledge/bundle.py` reads Atlas bundles); re-register the sources in
   AnalystOS workspaces; confirm each team can run its usual jobs there.
3. **Last commit in Atlas.** On the default branch, replace the top of `README.md` with the pointer below,
   commit it, and tag it `final-<yyyy-mm-dd>`.
4. **Read-only.** Archive the GitHub repository (Settings → General → Danger zone → *Archive this
   repository*), which makes code, issues and pull requests read-only. If archiving is not allowed, protect
   every branch with no push access and remove write collaborators instead.
5. **Stop what runs.** Disable scheduled GitHub Actions and delete deploy keys and repository secrets;
   bring down any Atlas deployment (`docker compose down`, Helm uninstall) after step 2 is confirmed; keep a
   database backup for the retention period the owner sets.
6. **Verify.** A push to the archived repository is refused, and the README pointer renders on the
   repository page. Record both in the P7-14 evidence.

README pointer (step 3):

```markdown
> **This repository is frozen (read-only) as of <yyyy-mm-dd>.** Atlas's capabilities now live in
> **AnalystOS** (`AIAnalystAgentOS`), the single platform for analyst, data-science, data-engineering and
> ML work. See `docs/60-delivery/donor-parity-atlas.md` there for what moved, what was dropped and why.
> Do not open issues or pull requests here.
```

This checklist was prepared without modifying the Atlas repository.
