# Delivery tracker (status authority)

Status vocabulary: **Done** (code + automated test, and live evidence where the row needs it) ·
**Done (mock)** (built and tested only against a mock/synthetic system — not certified) ·
**Partial** (usable, with a named gap) · **Not started** · **Phase N** (deliberately out of this release).
Evidence lives in the [capability register](02-capability-register.md). IDs are spec v1 §60 IDs.
Last reconciled: 2026-09-25 (after live e2e `evidence/e2e-20260925-054942.md`, 26/26).

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
| CTX-005 | Context caching | Not started | retrieval is fast enough locally; Redis cache for Context2AI calls to add with the live adapter |
| META-001 | PostgreSQL connector | Done | pushdown; integration-tested against compose Postgres |
| META-002 | SQL Server connector | Partial | metadata + dialect + validator tested; never run against a live SQL Server |
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
| QRY-007 | Dialect abstraction | Done | postgres + tsql |
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
| SCH-001..005, RPT-001..003, MON-001..005 | Phase 3 — API returns an explicit "not available" error |
| SEC-001..007, GOV-001..004, OPS-001..005 | Phase 4 — baseline RBAC, column policy, PII denial, destination and model allowlists already enforced; SSO/ABAC/row policy/HA/DR not started |
| AUT-001..006 | Phase 5 — iteration loop, stop criteria and budgets exist in Phase-1 form |
| PRO-001..006 | Phase 6 |
