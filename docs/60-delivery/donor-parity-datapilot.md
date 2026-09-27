# Donor parity checklist — DataPilot (`AienginnerAgentOs`) → AnalystOS (P7-14)

Prepared 2026-09-27 against DataPilot `AienginnerAgentOs@15235dc` (branch `claude/cool-heisenberg-isaz0q`)
and AnalystOS at the increment-7 wave-2 merge (`2cb8380`) plus this branch. **Unsigned.** The
[freeze](#freeze-procedure) happens only after the owner signs the [sign-off table](#sign-off).

**What this is.** Every user-facing capability DataPilot offers, taken from its README ("Verified local
workflow", 20 paths), its implementation status matrix (`docs/IMPLEMENTATION_STATUS_MATRIX.md` §0–§5,
reconciled 2026-09-24) and its UI views (`apps/web/app/components/*View.tsx`). DataPilot's tracker is a
spreadsheet (`docs/DataPilot_Spec_Completion_Tracker.xlsx`); the status matrix is its reconciled text
form and is used here. For each capability: the AnalystOS equivalent with a code path and a test, or
**not covered** with a disposition. Per [ADR-0018](../10-architecture/adr/0018-one-platform-donor-repositories.md),
parity means the user's job can be done in AnalystOS under AnalystOS contracts; DataPilot's own status is
not inherited (much of it is API-level tested only, study §4).

**Legend.** Covered = the job is done in AnalystOS today with a test. Partial = part of the job is
covered; the rest is dropped or needs a row as stated. Not covered = no equivalent. Disposition:
**Dropped** (with the reason) or **Row** (a tracker row is needed; proposed wording given, the owner adds
it — this checklist does not edit the tracker).

## 1. Identity, projects, roles

| # | DataPilot capability | AnalystOS equivalent (code · test) | Status | Disposition |
|---|---|---|---|---|
| A1 | Local users; admins create users, assign roles, activate/deactivate; self-service password change | Local users for dev/demo (`security/auth.py`, `api/routers/auth.py` `/users`); deactivation ends live streams (P4-01) · `tests/unit/test_stream_revocation.py` | Partial | Dropped: self-service passwords — production identity is OIDC (A4); local users are a dev/demo path |
| A2 | Projects: create, switch, memberships, a tested model pinned per project | Workspaces with members and a policy that restricts models (`services/workspaces.py`, `llm/router.py` model policy) · `tests/integration/test_workspace_lifecycle_api.py`, `tests/unit/test_model_policy.py` | Covered | — |
| A3 | Fine-grained RBAC by project role; viewer blocked from SQL, agents and tools | Workspace roles on every route with workspace binding (P4-01) · `tests/unit/test_route_workspace_binding.py`, `tests/integration/test_workspace_binding_api.py` | Covered | — |
| A4 | PingFederate SSO (admin configuration contract only) | Full OIDC authorization-code + PKCE, JWKS validation, group → role/ABAC mapping (`security/oidc.py`) · `tests/integration/test_oidc_sso.py`, `tests/unit/test_oidc_abac.py` | Covered | AnalystOS goes further than the donor |
| A5 | Role-based landing pages | Two-state Overview, admin screens behind the gear by role (P7-18, `web/`; commits `548d38b`, `e29599a`) | Covered | — |

## 2. Getting data in

| # | DataPilot capability | AnalystOS equivalent (code · test) | Status | Disposition |
|---|---|---|---|---|
| B1 | Upload CSV, JSON, Excel, Parquet; review the source-to-target mapping; replace, append or key-merge loads, versioned | `services/file_ingest.py` (P6-06: mapping contract, replace/append/merge on the Arrow + COPY loader, content fingerprints) · `tests/unit/test_file_ingest.py`, `tests/integration/test_file_ingest_loads.py` | Covered | Stronger: bad values are refused with the column named, never silently nulled |
| B2 | Bulk catalog-metadata import from a CSV (no live data) | — | Not covered | Dropped: an asset with no data cannot be analysed; descriptions and glossaries import as knowledge documents or OKF bundles (`knowledge/documents.py`, `knowledge/bundle.py`) |
| B3 | Driver-backed connectors: SQL Server, Oracle, Teradata, BigQuery (metadata + fixed parameterised read-only query tools) | `config/source_kinds.yaml` (SQL Server, Oracle, BigQuery and 12 more), every query through `QueryGateway.execute` · `tests/unit/test_source_kinds.py`, `tests/integration/test_generic_sources.py` | Partial | Dropped for Teradata until a pilot names it (then the `add-connector` checklist) |
| B4 | A source connected through an upstream MCP server; metadata scan of its tools as catalog assets | MCP client for tools, SSRF-guarded (`mcp/client.py`, P7-11) · `tests/integration/test_mcp_client.py` | Partial | Dropped: an MCP server as a *data source* bypasses SQL validation and scope (CLAUDE.md rule 4); MCP tools stay tools |
| B5 | Durable external extraction into PostgreSQL staging | Staging loader through the gateway (`staging/`, `services/sources.py`) · `tests/integration/test_staging_servicenow_load.py`, `tests/integration/test_snapshot_population.py` | Covered | — |
| B6 | Approved recurring append/merge schedules with a persisted watermark | Incremental recipes and pipelines with watermarks, late data, replay and backfill (P6-01/P6-02, `services/pipelines.py`) · `tests/unit/test_incremental_staging.py`, `tests/integration/test_pipelines.py` | Covered | — |
| B7 | Schema drift detection and acknowledgment | Crawler drift (`services/crawler.py`); recipe schema policy `evolve`/`warn`/`strict` (P6-05) · `tests/unit/test_crawler_rules.py`, `tests/unit/test_recipe_ir.py` | Covered | — |
| B8 | Frictionless (Open Knowledge Foundation) Data Package export, validate and descriptive import | OKF v0.2 bundles (ADR-0013) and ODCS v3.2 data contracts (`knowledge/okf.py`, `evidence/odcs.py`) · `tests/unit/test_knowledge_okf.py`, `tests/unit/test_evidence_formats.py` | Partial | Dropped: OKF and ODCS are the interchange formats; a Frictionless exporter is a small addition if a customer asks |

## 3. Catalog, knowledge, semantics

| # | DataPilot capability | AnalystOS equivalent (code · test) | Status | Disposition |
|---|---|---|---|---|
| C1 | Catalog with hybrid search (Postgres keyword + Qdrant vector) | Layered retrieval on Postgres + pgvector (`context/service.py`, `knowledge/index.py`) · `tests/integration/test_knowledge_k05_k08.py`, `tests/unit/test_context_k05.py` | Covered | No Qdrant: one database |
| C2 | Manual metadata edit (description, tags, owner, sensitivity, freshness, status); column business names | Knowledge studio edits OKF documents (`knowledge/studio.py`); owner tags never overwritten · `tests/integration/test_knowledge_studio.py` | Partial | Covered by the existing row P4-07 ("glossary/column curation") |
| C3 | LLM-suggested table and column descriptions | Crawler with an optional model purpose and a rule path (`services/crawler.py`, `config/models.yaml`) · `tests/unit/test_crawler_rules.py` | Covered | — |
| C4 | SOP / glossary document upload (PDF, text) for grounding | `knowledge/documents.py` (Markdown, text, PDF → reviewed drafts), injection-screened · `tests/integration/test_crawler_sources.py`, `tests/unit/test_security_injection.py` | Covered | — |
| C5 | PII detection and output masking | Crawler PII tags, policy-restricted columns, masks and row filters in the semantic compiler (`skills/catalog.py`, `semantic/compiler.py`, `governance/row_filters.py`) · `tests/unit/test_crawler_rules.py`, `tests/unit/test_semantic_compiler.py`, `tests/unit/test_row_filters.py` | Covered | — |
| C6 | Semantic metrics, approved joins, metric-to-asset binding | Semantic layer with reviewed structure, cardinality and a deterministic compiler (P4-05, P7-02, P7-09) · `tests/unit/test_semantic_compiler_joins.py`, `tests/integration/test_semantic_layer.py` | Covered | — |
| C7 | Semantic relationship graph | Semantic graph in the knowledge studio; Postgres graph of record (`graph/projection.py`) · `tests/integration/test_graph_postgres.py` | Covered | — |

## 4. SQL, Ask, conversations

| # | DataPilot capability | AnalystOS equivalent (code · test) | Status | Disposition |
|---|---|---|---|---|
| D1 | Catalog-grounded, dialect-aware SQL generation with guarded read-only previews; writes and DDL refused | Ask (`services/ask.py`): verified queries, semantic compiler, model generation with repair, all through the gateway validator · `tests/unit/test_ask_rules.py`, `tests/unit/test_gateway_validator.py`, `tests/integration/test_ask_benchmark.py` | Covered | — |
| D2 | Persistent conversations with memory and summary; 3-panel chat with an inspector | Ask threads (`services/ask.py`), turn evidence and decisions · `tests/unit/test_ask_threads.py`, `tests/integration/test_ask_threads_api.py` | Covered | — |
| D3 | Paste-your-own-SQL explain/validate mode | SQL explanation (`skills/sqlexplain.py`), run console · `tests/unit/test_sqlexplain.py` | Partial | Row (P2), shared with Atlas E4: "paste-SQL review: validate and explain a user's statement, run it once through the gateway on an explicit action" |
| D4 | SQL query history | Ask turns and query executions per workspace (`api/routers/artifacts.py` `/queries/{id}`) · `tests/integration/test_ask_threads_api.py` | Covered | — |
| D5 | "Why this result?" drawer (source, model, validation, grounding) | "Why this number?" API (`evidence/why.py`, P7-08) · `tests/unit/test_verification_p701.py`, `tests/integration/test_verification_records_live.py` | Partial | Covered by the existing row P7-08 (UI drawer pending) |
| D6 | Data-driven charts (KPI, line, bar, stacked, pie, scatter, table) with a switcher | Chart inference and dashboards (`skills/viz.py`, `skills/dashboards.py`) · `tests/unit/test_skills_viz_dashboards.py`, `tests/unit/test_publishing_charts.py` | Covered | — |
| D7 | Verified-query memory: exact reuse, few-shot, thumbs-up capture, thumbs-down review | Verified-query registry (ladder L1) and the learning loop into the review queue (`registries/verified_queries.py`, `knowledge/learning.py`) · `tests/unit/test_registry_logic.py`, `tests/integration/test_registries.py` | Covered | — |
| D8 | Multi-model SQL vote (cascade: the third model only on disagreement); Jev judges split votes | — | Not covered | Dropped for now: AnalystOS answers from verified queries and the semantic compiler first and validates deterministically; a vote multiplies model spend (ADR-0012). Re-open if the live P4-V02 tier misses its threshold |
| D9 | GEPA offline prompt optimisation with approval to activate | — | Not covered | Dropped for now: prompts ship with the code and are pinned per run; an optimiser needs the live eval gates (P7-07) as its objective first |
| D10 | DDL suggestions from slow queries (execution only with a flag and approval) | — | Not covered | Dropped: AnalystOS never writes to customer sources outside the managed writer (P6-03); index advice is out of scope |
| D11 | Save SQL as an artifact; versions, diffs, version comments, approve / request changes | Steps with versions and downstream voiding (P7-04), definition versions with diffs (P7-03), approvals bound to hashes (`governance/approvals.py`) · `tests/unit/test_steps.py`, `tests/unit/test_definitions_pins.py`, `tests/integration/test_governance_approvals.py` | Partial | Dropped: free-form comments on versions; review is the hash-bound approval, and branches compare versions (P7-05) |
| D12 | Quick actions: publish an answer as a tool, eject to a notebook, per-message feedback | Promote to a verified query, notebook cells as steps (P7-12), feedback into the learning loop · `tests/unit/test_notebooks.py`, `tests/integration/test_knowledge_k05_k08.py` | Covered | — |

## 5. Agents, tools, decisions, external gateway

| # | DataPilot capability | AnalystOS equivalent (code · test) | Status | Disposition |
|---|---|---|---|---|
| E1 | Agent registry: versioned instructions, bound models and tools, reviewed publication, scorecard and publish gate | Enforced agent manifests (`config/agents/*.yaml`, `capabilities/agents.py`), capability certification gate and per-workspace enablement, definition versions · `tests/unit/test_agent_contract.py`, `tests/unit/test_capability_platform.py` | Covered | — |
| E2 | Bounded multi-agent jobs on Temporal; risky objectives wait for approval; plan hash verified before every step | Runs with hashed plans (`runtime/plan.py`, `runtime/engine.py`), approvals, Temporal or the local orchestrator · `tests/unit/test_local_orchestrator.py`, `tests/integration/test_e2e_local_run.py`, `tests/integration/test_temporal_queues.py` | Covered | — |
| E3 | Agent self-healing on tool errors; reviewer/critic pass | Critic agent and replan (`agents/critic.py`, `runtime/engine.py` `replan`) · `tests/unit/test_agent_logic.py`, `tests/unit/test_engine_unit.py` | Covered | — |
| E4 | Jev decides route, agent and tools; escalates consequential actions only | Decision service with JEV authority limits (ADR-0015, `decisions/`, `llm/jev.py`) · `tests/unit/test_decision_service.py`, `tests/integration/test_decisions_db.py` | Covered | — |
| E5 | Parameterised HTTP tools with JSON Schema contracts, approval and audit; SSRF-safe egress | `tools/http.py` (ported, `not ip.is_global` fix) · `tests/unit/test_outbound_http.py` | Covered | — |
| E6 | Governed query tools: wizard, lifecycle draft → tested → published → retired, searchable purpose/source/LOB/owner/tags | Verified queries promoted from answers; definitions lifecycle (P7-03) · `tests/unit/test_registry_logic.py`, `tests/unit/test_definitions_pins.py` | Partial | Covered by the existing row P7-11 (query-tool lifecycle through definitions, draft tools not callable over MCP) |
| E7 | External gateway for VS Code / Google ADK: REST/OpenAPI/MCP, client tokens with expiry, per-grant daily quota, rate limits, per-connector concurrency ceiling | AnalystOS MCP server with clients, grants and quotas (`mcp/server.py`, `mcp/grants.py`) · `tests/integration/test_mcp_server.py`, `tests/unit/test_mcp_logic.py` | Covered | — |
| E8 | Separation of duties on approvals | `governance/approvals.py` (requester cannot approve; always for sensitive actions) · `tests/integration/test_governance_approvals.py` | Covered | — |
| E9 | Data residency: keep a project's decisions on-prem | Air-gapped model config and residency policy (`config/models.airgapped.yaml`, model policy) · `tests/unit/test_model_policy.py` | Covered | — |
| E10 | Model providers: OpenAI-compatible, Gemini, Claude, company; live provider tests; per-purpose routes | `llm/providers.py`, `config/models.yaml` purposes, `config/models.providers.example.yaml`, model health · `tests/unit/test_model_providers.py`, `tests/unit/test_model_router.py` | Covered | — |

## 6. Quality, pipelines, lineage, notebooks, jobs

| # | DataPilot capability | AnalystOS equivalent (code · test) | Status | Disposition |
|---|---|---|---|---|
| F1 | Quality rules (created or suggested), pass rates, failure samples, quarantine tables | `fail`/`warn`/`drop` gates with samples and quarantine through the loader (`recipes/gates.py`, P6-05; DataPilot rule types rewritten), monitors · `tests/unit/test_recipe_ir.py`, `tests/integration/test_recipes.py` | Covered | — |
| F2 | Pipeline generated from a staged dataset; approved deployment; persisted lineage | Engineer agent proposes recipes (P6-08), managed writer with approval, atomic promotion and rollback (P6-03), recipe lineage · `tests/unit/test_engineer_agent.py`, `tests/integration/test_pipelines.py` | Covered | — |
| F3 | dbt and Dataform package emission | dbt emitter passing `check_files` (`recipes/dbt.py`), dbt builds on customer engines (`build/`) · `tests/unit/test_recipe_ir.py`, `tests/integration/test_elt_build.py` | Partial | Dropped: Dataform `.sqlx` (optional idea in ADR-0018 §4) |
| F4 | Lineage list and bounded multi-hop traversal | `artifacts/registry.py` `lineage_for` (recursive CTE), column lineage (`evidence/lineage/`) · `tests/unit/test_lineage_unit.py`, `tests/unit/test_sql_lineage.py` | Covered | — |
| F5 | Governed notebooks: markdown, read-only SQL, restricted Python; executions versioned | Notebooks where each cell is a step: SQL through the gateway, Python in `compute-py` (P7-12, `services/notebooks.py`) · `tests/unit/test_notebooks.py`, `tests/integration/test_brief_steps_api.py` | Covered | Stronger: DataPilot's SQL cells bypassed its guard (study §4) |
| F6 | Jobs: cancel, retry, diagnose failures into incidents | Run cancel and resume after restart (P7-16), retryable errors (`core/errors.py`) · `tests/unit/test_local_orchestrator.py`, `tests/unit/test_lite_profile.py` | Partial | Dropped: incident diagnosis objects; a failed run shows its error and retryability |

## 7. Analytics, evaluation, governance, deployment

| # | DataPilot capability | AnalystOS equivalent (code · test) | Status | Disposition |
|---|---|---|---|---|
| G1 | Superset dashboards embedded in the portal with short-lived dashboard-scoped guest tokens; admin editor handoff | Publish to Superset with per-workspace logins and roles (`publishing/superset.py`, `services/bi_access.py`) · `tests/integration/test_superset_publish.py`, `tests/integration/test_bi_workspace_isolation.py` | Partial | Row (P2): "embed a published dashboard in AnalystOS with a short-lived, dashboard-scoped guest token (no second login)" |
| G2 | Publish a SQL or notebook result to Superset as a dataset after approval | Approval-gated publication of dashboards and datasets (`publishing/`, `governance/approvals.py`) · `tests/integration/test_superset_publish.py`, `tests/unit/test_publishing_superset.py` | Covered | — |
| G3 | Evaluations: replay model evaluation sets, red-team suite, baselines, persisted scores | Model replay (`llm/replay.py`), evaluation suites and owner-versioned gates (`evaluation/`, `config/eval_gates.yaml`), injection corpus · `tests/unit/test_model_replay.py`, `tests/unit/test_eval_gates.py`, `tests/unit/test_security_injection.py` | Covered | — |
| G4 | Prompt versioning and rollback | Prompt versions recorded on every model call and listed for admins (`agents/prompts.py`, `/api/admin/prompts`) · `tests/unit/test_prompt_payload.py` | Partial | Dropped: runtime prompt rollback — prompts ship and roll back with the release, and a run records the version it used |
| G5 | Feedback → human-review learning suggestions, with repeat and trend detection | Learning loop into the knowledge review queue (`knowledge/learning.py`, `knowledge/suggestions.py`) · `tests/integration/test_knowledge_k05_k08.py` | Covered | — |
| G6 | Retention policies | Read-only workspace inventory before retention or erasure (`services/workspace_inventory.py`); cache and idempotency retention | Partial | Row (P2): "workspace retention policies for artifacts, query results and conversations, with audited erasure" |
| G7 | Audit event log | `governance/audit.py`, `/api/admin/audit` · `tests/integration/test_governance_approvals.py` | Covered | — |
| G8 | Security posture dashboard (injection, PII and toxic-content scoring, trends) | Injection verdicts and PII refusals are recorded in audit and turn evidence | Not covered | Dropped: no separate screen (spec v4 §15 screen budget); the events are in the audit log |
| G9 | Governance telemetry fan-out: AgentGuard, OTLP (Phoenix, Langfuse), signed webhook | OpenLineage events for queries and recipes (`evidence/openlineage.py`) · `tests/integration/test_evidence_open_formats.py` | Partial | AgentGuard dropped (proprietary dependency, ADR-0018 §3). Row (P2): "OpenTelemetry export of model, tool, query and approval spans" |
| G10 | Kubernetes manifests: non-root, securityContext, HPAs, PDB, NetworkPolicy | Helm chart with `values-small.yaml`, isolated pools with NetworkPolicy (P7-06, P7-17) · `tests/unit/test_helm_chart.py` | Covered | — |

## Summary

| Status | Count |
|---|---|
| Covered | 38 |
| Partial | 15 |
| Not covered | 5 |
| **Total capabilities** | **58** |

Rows the owner is asked to add (none added here): D3 (shared with Atlas E4), G1, G6, G9. C2, D5 and E6 are
already covered by open rows (P4-07, P7-08, P7-11). Everything else not covered is dropped with the
reason given.

## Sign-off

| Role | Name | Decision (accept / accept with rows / reject) | Date | Signature |
|---|---|---|---|---|
| Product owner (Context2AI) | | | | |
| DataPilot product owner or maintainer | | | | |

Accepting means: every Covered row is good enough for DataPilot's current users, every Dropped reason is
agreed, and every Row has been added to `docs/60-delivery/01-tracker.md` (or explicitly declined).

## Freeze procedure

Do these in order, only after the sign-off above. Until then DataPilot keeps working for its users and
the ADR-0017 contracts still apply.

1. **Record.** In AnalystOS, attach the signed copy of this file to tracker row P7-14 (evidence in the
   capability register with the date) and note the freeze in ADR-0018 §5.
2. **Migrate users.** Re-create each DataPilot project as an AnalystOS workspace; re-upload staged files
   through file ingestion (B1) or register the sources; re-promote the verified queries that users relied
   on; re-publish the Superset dashboards through AnalystOS approvals. Confirm each team can run its
   usual jobs there.
3. **Last commit in DataPilot.** On the default branch, replace the top of `README.md` with the pointer
   below, commit it, and tag it `final-<yyyy-mm-dd>`.
4. **Read-only.** Archive the GitHub repository (Settings → General → Danger zone → *Archive this
   repository*). If archiving is not allowed, protect every branch with no push access and remove write
   collaborators.
5. **Stop what runs.** Disable GitHub Actions workflows, delete deploy keys and repository secrets, and
   rotate any provider keys the DataPilot `.env` held (its own status matrix §0.6 lists keys to rotate);
   bring down the Compose stack or Kubernetes release after step 2 is confirmed; keep a database backup
   for the retention period the owner sets.
6. **Verify.** A push to the archived repository is refused, and the README pointer renders on the
   repository page. Record both in the P7-14 evidence.

README pointer (step 3):

```markdown
> **This repository is frozen (read-only) as of <yyyy-mm-dd>.** DataPilot's capabilities now live in
> **AnalystOS** (`AIAnalystAgentOS`), the single platform for analyst, data-science, data-engineering and
> ML work. See `docs/60-delivery/donor-parity-datapilot.md` there for what moved, what was dropped and why.
> Do not open issues or pull requests here.
```

This checklist was prepared without modifying the DataPilot repository.
