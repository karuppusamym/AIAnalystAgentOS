# Delivery tracker (status authority)

Status vocabulary: **Done** (code + automated test, and live evidence where the row needs it) ·
**Done (mock)** (built and tested only against a mock/synthetic system — not certified) ·
**Partial** (usable, with a named gap) · **Not started** · **Phase N** (deliberately out of this release).
Evidence lives in the [capability register](02-capability-register.md). IDs are spec v1 §60 IDs.
Last reconciled: 2026-09-25 (after increment 3 live evidence).

## P. Current execution queue

The only list of work in flight. A row moves to **Done** only with a test and, where it says
"live", a dated evidence file in the [capability register](02-capability-register.md). Finished
increments stay here as history.

### Increment 1 — Phase 0 + Phase 1 MVP (2026-09-25) — **Done**
Evidence: `evidence/e2e-20260925-054942.md` (26/26). Rows below in the phase tables.

### Increment 2 — Phase 3: scheduled & continuous analytics (2026-09-25) — **Done**

Evidence: `evidence/e2e-phase3-20260925-064436.md` (13/13, live) and the deterministic
integration test `tests/integration/test_e2e_local_run.py::test_phase3_schedule_monitor_report`.

| # | Scope (v1 IDs) | Status | Evidence / notes |
|---|---|---|---|
| P2-01 | Schedules + scheduler process + run audit (SCH-001, SCH-005) | Done | claim-then-execute; idempotent claim tested; ADR-0009 |
| P2-02 | Scheduled dataset refresh for staged sources (SCH-002) | Done | `refresh_first` on re-analysis; `dataset_refresh` kind |
| P2-03 | Scheduled re-analysis with "what changed" diff (SCH-004) | Done | carried-forward claims + stable KPI definitions; live diff 4 persisting / 0 resolved / 0 not re-tested on unchanged data |
| P2-04 | Narrative report + PDF + Excel, on demand and scheduled (RPT-001..003, SCH-003) | Done | 63 renderer tests; live downloads pdf/xlsx/html |
| P2-05 | Threshold, drift, change-point, data-quality monitors (MON-001..004) | Done | future-dated rows and incomplete periods excluded (found live) |
| P2-06 | Alerts, notifications, JEV triage, automatic investigation (MON-005) | Done | live alert triaged by JEV; auto-investigation run completed |
| P2-07 | UI: schedules, monitoring/alerts, notifications, reports | Done | 50 vitest tests; live smoke |
| P2-08 | Live Phase-3 evidence | Done | 13/13 |

Findings from the live runs that changed the design (kept for history): the first live pass was
9/12 — monitors read future-dated and partial periods as a 97% volume drop (JEV rated it 0.88
material, which is why detection stays deterministic); a later pass showed re-analysis diffs were
noisy because each run asked different questions and KPI definitions drifted (fraction vs percent),
fixed by carrying claims and KPI definitions forward.

### Increment 3 — Universal sources, crawler, token economy, admin control plane (2026-09-25) — **Done**

Requested: connect any database (ServiceNow is only a sample), crawl metadata like the sibling
projects, deterministic-first to reduce tokens, everything controllable from admin. Design:
[ADR-0010](../10-architecture/adr/0010-universal-sources-crawler-token-economy.md). Evidence:
`evidence/e2e-increment3-20260925-150550.md` (live) plus the tests named per row.

| # | Scope (v1 IDs) | Status | Evidence / notes |
|---|---|---|---|
| P3-01 | Source-kind catalog + generic SQL connector; pushdown vs staged by dialect (META-002, v1 §16, §17) | Done (partly certified) | 15 kinds. **Live-tested against a real engine:** Postgres, MySQL 8.4, SQLite, DuckDB (`test_generic_sources.py`). **Catalog + unit tests only, not certified:** SQL Server, Oracle, Snowflake, BigQuery, Databricks, Trino, Redshift, ClickHouse, MariaDB. |
| P3-02 | Metadata crawler: full/incremental, include/exclude, fingerprints, drift, deterministic semantics/PII/glossary links, optional batched LLM enrichment, vector + graph, scheduled (META-005/006, CTX-004, v1 §13.3) | Done | `tests/integration/test_crawler.py`: lifecycle, drift, curation kept, deprecation rules, re-staging. Live: drift detected, curation kept, 0 model calls. |
| P3-03 | Token economy: response cache, per-purpose LLM mode (off/auto/always), prompt compaction, pre-call estimate + downgrade, savings accounting (MOD-003, v1 §51) | Done | 9 router unit tests. Live under `token_saver`: every avoided call recorded as `skipped` with `tokens_saved`. |
| P3-04 | Admin control plane: versioned platform settings (routing, modes, limits, crawl defaults, source kinds, flags) applied at runtime (v1 §52.8) | Done | `tests/integration/test_platform_settings.py`: admin-only, validation, preset → router, rollback, no stale merge. Live: preset applied at runtime, then rolled back. |
| P3-05 | Agents/tools/skills: catalog steward agent, crawl/enrich/explain tools, forecasting skill + forecast-deviation monitor, role-driven hypothesis playbook, KPI × dimension follow-ups | Done | `test_skills_catalog.py` (72), `test_skills_forecast.py` (18), `test_sqlexplain.py`, `test_agent_logic.py`, `test_continuous_logic.py` |
| P3-06 | UI for sources catalog, crawls, catalog curation, SQL explain, admin settings, token savings | Done | 76 vitest tests, typecheck, build; live smoke including Catalog |
| P3-07 | Live evidence for increment 3 | Done | `evidence/e2e-increment3-20260925-150550.md` |

Findings from the live runs and the governance review that changed the design (kept for history):
- **Silent crawl failure.** The API crawl endpoint scheduled its background task before its
  `crawl_run` row was committed, so the crawl silently never ran. Fixed by committing first; crawls
  with no progress for 10 minutes are recovered as interrupted.
- **Stale staged snapshot.** A drift in a staged source updated the catalog but not the snapshot,
  so profiling (and any analysis) referenced a column the snapshot lacked. Changed selected assets
  are now re-staged within the crawl.
- **Stored PII samples.** The review found that PII value samples would persist in the gateway
  audit preview and the result cache. Added `retain_rows=False`.
- **Budget downgrade.** The review found that the downgrade bypassed the workspace model allowlist.
  It now uses the same model resolution as every call and never applies to verification.
- **Hypothesis breadth.** The first live pass tested only one outcome, and three findings said the
  same thing under one generic title. Added: descriptive driver-model titles, driver-model
  deduplication, a role-driven playbook, outcome-diverse selection, and deterministic KPI × dimension
  follow-ups (no follow-up model calls).
- **Tag-wiping re-discovery.** `discover_source` used to drop owner `restricted` tags on every
  re-discovery. It now runs through the crawler, which never removes tags.

### Next candidates (not started — to be prioritised)

| # | Scope | Why |
|---|---|---|
| N-1 | Phase 2 semantic layer: versioned semantic models, metric approval workflow (SEM-001..005) | KPIs are validated but not yet owned/approved objects |
| N-2 | Existing-dashboard mode (BI-011/012) on top of `SupersetPublisher.inspect_dashboard` | v1 §35 |
| N-3 | Approval-gated external delivery (email/webhook) for reports and alerts | completes SCH-003 delivery |
| N-4 | Cross-source analysis (INT-001..005, TRN-004 Trino) | v1 Phase 2 |
| N-5 | SSO/OIDC + ABAC (SEC-001..003) | pilot readiness |
| N-6 | Sandbox in a network-less container; Unicode PDF font | readiness gaps |

## Phase 0 — Foundation

| ID | Task | Status | Notes |
|---|---|---|---|
| FND-001 | Monorepo | Done | Python package + `web/` + `deploy/`; builds locally |
| FND-002 | Coding standards | Done | `CLAUDE.md` conventions, ruff config |
| FND-003 | CI pipeline | Done | `.github/workflows/ci.yml` (lint, unit, integration with services, web build) |
| FND-004 | CD skeleton | Partial | Images build from compose; no deploy target defined |
| FND-005 | Workspace schema | Done | migration 0001 |
| FND-006 | Agent contract | Done | `AgentSpec`, `config/agents/*.yaml`, `contracts/agent.schema.json` |
| FND-007 | Skill contract | Done | `SkillSpec`, `skills/registry.py` |
| FND-008 | Tool contract | Done | `ToolSpec`, `tools/registry.py` |
| FND-009 | Artifact model | Done | `artifact`, `artifact_version`, `lineage_edge` |
| FND-010 | Event model | Done | `contracts/events.py`, `run_event`, SSE |
| FND-011 | Policy model | Done | `WorkspacePolicyDoc` (versioned), `PolicyDecision` |
| FND-012 | Model router contract | Done | `config/models.yaml`, `llm/router.py` |
| FND-013 | Logging standard | Done | JSON logs with correlation + run ids |
| FND-014 | Error taxonomy | Done | `core/errors.py` (retryable flag, HTTP status) |
| FND-015 | Local Docker environment | Done | `compose.yaml` full stack; Superset derived image |

## Phase 1A–1I — MVP

| ID | Task | Status | Notes |
|---|---|---|---|
| WSP-001..002 | Workspace create/update API | Done | |
| WSP-003 | Member model | Done | roles owner/editor/analyst/approver/viewer |
| WSP-004 | Source registration | Done | |
| WSP-005 | Policy settings | Done | stored, versioned, enforced (scope, gateway, approvals, budgets) |
| WSP-006..007 | Artifact view, activity feed | Done | |
| WSP-008 | Workspace UI | Done | `web/` |
| CTX-001 | Context2AI client | Partial | HTTP adapter written against v1 §11.1 paths; no live Context2AI to test — local store serves the same role |
| CTX-002 | Semantic search | Done | pgvector + deterministic embeddings (ADR-0007) |
| CTX-003 | Neo4j relationship adapter | Done | projection + neighborhood query |
| CTX-004 | Business-term resolver | Done | glossary `mapped_columns` → scope columns |
| CTX-005 | Context caching | Partial | LLM response cache added in increment 3; the Redis cache for Context2AI calls comes with the live adapter |
| META-001 | PostgreSQL connector | Done | pushdown; integration-tested against compose Postgres |
| META-002 | SQL Server connector | Partial | metadata + dialect + validator tested; never run against a live SQL Server (other databases: see P3-01) |
| META-003 | CSV connector | Done | CSV/Parquet; Excel skipped when no engine installed |
| META-004 | ServiceNow connector | Done (mock) | Table API + display values; tested only against the bundled mock |
| META-005 | Metadata normalization | Done | `DiscoveredAsset/Column` |
| META-006 | Source statistics | Done | row counts, freshness, profile stats |
| TLR-001..003 | Tool registry, permission checks, audit | Done | |
| SKL-001 | Skill registry | Done | |
| MOD-001 | Model router | Done | live OpenRouter verified |
| MOD-002 | Provider fallback | Done | model fallback within profile; fail closed when none allowed |
| MOD-003 | Token/cost accounting | Done | `model_call`, run totals, budgets |
| MOD-004 | Prompt version registry | Done | `agents/prompts.py`, version on every call |
| QRY-001..005 | Gateway, read-only validation, timeout, row limit, audit | Done | 109-case validator security suite + live tests |
| QRY-006 | Query cache | Done | Redis, key includes scope hash + source version |
| QRY-007 | Dialect abstraction | Done | pushdown in postgres + tsql; every other kind staged and validated as postgres (ADR-0010) |
| DEX-001 | DuckDB engine | Partial | used for file inspection and skill tests; analysis runs as pushdown SQL |
| DEX-002 | Polars engine | Done | extraction/transforms, profiling inputs |
| DEX-003..005 | Python sandbox, memory, timeout | Done | rlimits + AST allowlist; not a hard security boundary (see readiness) |
| AGT-001..003 | Runtime, task state machine, messages | Done | |
| AGT-004..012 | Supervisor … REV critic | Done | see capability register for live evidence |
| AGT-013 | Pause/resume | Done | |
| AGT-014 | Feedback injection | Done | JEV classification + LLM interpretation + replan |
| AGT-015 | Retry policy | Done | Temporal retry policy, non-retryable domain errors |
| ANA-001..015 | Profiling + statistics skills | Done | benchmarks with planted effects |
| ART-001..003 | Artifact registry, dependencies, lineage | Done | |
| INS-001..004 | Insight schema, evidence, verification, UI | Done | |
| BI-001..008 | Publisher interface … external IDs | Done | live Superset 4.1.1 integration test |
| BI-009 | Update dashboard | Done | update in place, stable ids |
| BI-010 | Publish approval gate | Done | hash-bound approvals |
| UI-001..008 | MVP UI | Done | see `web/README.md` |

## Phase 2+

| ID range | Status |
|---|---|
| INT-001..005, TRN-001..004, SEM-001..005 (beyond Phase-1 KPI validation), PBI-001..002, BI-011..012 | Phase 2 — `inspect_dashboard` exists as a foundation for BI-011 |
| SCH-001..005, RPT-001..003, MON-001..005 | **Done** in increment 2 (see section P) — external delivery channels deliberately not included |
| SEC-001..007, GOV-001..004, OPS-001..005 | Phase 4 — baseline RBAC, column policy, PII denial, destination and model allowlists already enforced; SSO/ABAC/row policy/HA/DR not started |
| AUT-001..006 | Phase 5 — iteration loop, stop criteria and budgets exist in Phase-1 form |
| PRO-001..006 | Phase 6 |
