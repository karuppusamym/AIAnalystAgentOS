# Architecture

See [spec v2](../00-intent/02-spec-v2.md) for the rules; this document shows the structure and the
runtime flows. Decisions are recorded as ADRs in [`adr/`](adr/).

This page describes the implemented architecture, as of increment 7 waves 1 and 2 (2026-09-26/27).
The workspace-adaptive extension ([ADR-0011](adr/0011-workspace-workflows-and-evidence.md), with a
[workbench interaction design](02-workbench-ux.md) and [API evolution contract](../20-contracts/02-workbench-api.md))
keeps the modular monolith and adds typed work orders, evidence versions, isolated compute and
retry-safe dispatch; most of it now exists (below). The tracker is the status authority; this page
names where things live.

[Spec v4](../00-intent/04-spec-v4-unified-data-platform.md) makes this one platform for analyst,
data-science, data-engineering and governed ML work, with Atlas and DataPilot as donor repositories
([ADR-0018](adr/0018-one-platform-donor-repositories.md); parity checklists in
[`60-delivery/donor-parity-atlas.md`](../60-delivery/donor-parity-atlas.md) and
[`donor-parity-datapilot.md`](../60-delivery/donor-parity-datapilot.md)). Implemented in increment 7:

| Capability | Where | Decision |
|---|---|---|
| Lite install by default: Postgres + API + web; Redis, Temporal, workers, Superset, Neo4j and the demo source are profiles; the local orchestrator resumes runs after a restart, parks runs waiting for a person, and runs steps on a bounded pool; spend caps in Postgres without Redis | `compose.yaml`, `deploy/compose/*.env`, `workflows/orchestrator.py`, `governance/budgets.py`, [runbook 04](../30-runbooks/04-lite-and-profiles.md) | [ADR-0025](adr/0025-lite-by-default.md) |
| Isolated, credential-free compute pools `compute-py` / `compute-ml`: `TaskEnvelope`, scoped short-lived artifact tokens, egress only to the artifact store, a conformance suite | `contracts/worker.py`, `workers/`, `api/routers/worker.py`, `tests/conformance/worker/` | [ADR-0022](adr/0022-compute-worker-protocol.md) |
| Transformation recipes and pipelines: one typed IR compiled to SQL through the gateway or to DuckDB on snapshots; join pre-flight; `fail`/`warn`/`drop` gates with quarantine; schema policy; column lineage and OpenLineage; dbt emitter; incremental loads with watermarks, replay and backfill; a managed writer with approval, atomic promotion and rollback; file ingestion | `contracts/recipe.py`, `recipes/`, `pipelines/`, `services/{recipes,pipelines,file_ingest}.py` | [ADR-0023](adr/0023-transformation-recipe-ir.md) |
| Governed classical ML: `MLSpec`, leakage and readiness refusals, immutable splits, a mandatory baseline, bounded search, a holdout read once, sealed reports and model cards, registry versions, approved batch scoring, monitors, MLflow export | `ml/`, `services/ml.py`, `api/routers/ml.py` | [ADR-0024](adr/0024-governed-classical-ml.md) |
| Semantic compiler: `SemanticQuery` IR compiled deterministically, policy filters and masks inside the compiler, fan-out refused, `governed`/`ad_hoc` label on every answer, semantic diff, `analystos check-semantics` | `semantic/compiler.py`, `governance/row_filters.py`, `semantic/diff.py` | [ADR-0019](adr/0019-semantic-compilation.md) |
| Verification records with dependency fingerprints (query, data, semantic, method, context, model call, policy) that void themselves on change; nightly sweep; "Why this number?" | `evidence/verification.py`, `evidence/why.py` | [ADR-0020](adr/0020-verification-fingerprints.md) |
| Definitions (draft / published / retired) for playbooks, recipes, ML specs and saved analyses; schedules pin definition, metric and method versions; *upgrade available* with a diff | `contracts/definition.py`, `services/definitions.py`, `services/pins.py`, `services/schedules.py` | [ADR-0021](adr/0021-published-versions-and-pinned-schedules.md) |
| Workspace brief and readiness; steps with versions, downstream voiding and pins; branches (fork, compare, merge); notebooks whose cells are steps (SQL through the gateway, Python in `compute-py`); work orders, idempotency keys and a dispatch outbox | `services/{brief,readiness,steps,step_pins,branches,notebooks,work_orders,idempotency}.py`, `api/routers/{workspace_brief,steps,work_orders}.py` | ADR-0011 |
| Donor ports: injection defence and question redaction (Atlas), SSRF-safe HTTP tools and MCP client (DataPilot), sqlglot column lineage, measured composite keys and relationship rules, seasonal DQ baselines, the adversarial SQL corpus as a gateway fixture | `security/injection.py`, `llm/redaction.py`, `tools/http.py`, `mcp/client.py`, `evidence/lineage/`, `skills/relationships.py`, `services/monitors.py`, `tests/unit/test_gateway_corpus.py` | ADR-0018 |
| Evaluation gates: owner thresholds, deterministic tiers in CI, live tiers nightly; grounding suite; held-out corpus (non-blocking report) | `config/eval_gates.yaml`, `evaluation/`, `scripts/eval_gates.py`, `scripts/benchmark_heldout.py` | spec v4 §13 |

Not implemented yet (tracker): multi-fact semantic plans, a draft-tool lifecycle over MCP (P7-11), the paired
practitioner baseline (P4-08), pilot identity and recovery (P4-09).

## Deployment view (compose / Kubernetes)

The default install is **lite**: Postgres (pgvector), `api` and `web`. The API runs the local
orchestrator and the scheduler in-process, publishes to the preview destination, and keeps spend
caps in Postgres. The diagram below is the **standard** profile (Redis, Temporal, `worker`,
`scheduler`) with the optional `bi` (Superset) and `graph` (Neo4j) features; `scale` splits the worker
per queue, and `isolated` adds the credential-free `compute-py` / `compute-ml` pools on an internal
network. Profiles change which services run, never behaviour ([runbook 04](../30-runbooks/04-lite-and-profiles.md)).

```
                  ┌───────────┐   /api (REST + SSE)   ┌──────────────────────────────┐
  Browser ───────▶│  web      │──────────────────────▶│ api  (FastAPI, uvicorn)      │
                  │ nginx+SPA │                       │  auth · workspaces · sources │
                  └───────────┘                       │  runs · approvals · artifacts│
                                                      └───────┬──────────────┬───────┘
                                     start / signal (nudge)   │              │ read/write
                                                      ┌───────▼───────┐      │
                                                      │ Temporal      │      │
                                                      └───────┬───────┘      │
                                             activities       │              │
                                                      ┌───────▼──────────────▼───────┐
                                                      │ worker (same image)          │
                                                      │ engine · agents · skills     │
                                                      │ gateway · llm router · BI    │
                                                      └─┬────────┬────────┬────────┬─┘
                          ┌─────────────────────────────┘        │        │        └──────────────┐
                          ▼                                      ▼        ▼                       ▼
   Postgres: analystos (control) · analytics (staged, reader)  Redis   Neo4j (projection)   OpenRouter
   · superset · temporal                                       cache                        chat + JEV decisions
                          ▲
                          └── Superset (reads analytics as analystos_reader)      ServiceNow / PG / SQL Server / files
```

Process roles share one image (ADR-0001), built with the extras each role needs (P7-17): `api`,
`worker` (Temporal) and `scheduler` (claim-then-execute cron loop, ADR-0009). The API never runs agent
work in the request; it writes state and signals the workflow (Temporal) or the local orchestrator
(lite). Isolated pools run `analystos worker --queues compute-py|compute-ml` with no database or provider
credentials and reach only the artifact store (ADR-0022).

## Component view

```
agents ──uses──▶ skills (deterministic)      agents ──invoke──▶ tools.ToolRuntime ──policy──▶ governance
   │                 │                                               │
   │                 └── RunSQL ──▶ gateway (validator → executor → cache → audit) ──▶ sources
   ├── llm.router (purposes → profiles → allowlisted models)  ├── llm.jev (typed decisions)
   ├── artifacts (versions + lineage) ──▶ graph (Neo4j projection)
   └── publishing (BIPublisher: superset | preview)
runtime.engine ◀── workflows (Temporal | local) ; runtime.context re-resolves scope per task

Ask ──▶ verified queries ─▶ semantic.compiler (SemanticQuery → SQL, row filters, masks) ─▶ gateway
recipes (IR → SQL via gateway | DuckDB on snapshots) ─▶ gates ─▶ pipelines.writer (approval) ─▶ managed outputs
ml.jobs (prepare → baseline + bounded search → holdout once → sealed report) ◀── workers (compute-ml)
evidence.verification (fingerprints → ACTIVE | VOID) ◀── changes to SQL, data, semantics, methods, policy
definitions (draft → published → retired) ─▶ schedules pin versions ; steps / branches / notebooks = versioned steps
```

## Runtime flow: one analysis run

1. `POST /api/workspaces/{id}/analysis` → `services.runs.create_run`: role ≥ analyst, scope
   resolved (selected assets, denied columns), policy `run_analysis` decision, run row with scope
   snapshot + hash, workflow started.
2. Workflow `plan_run` → supervisor frames questions (LLM, optional) → base plan (+`plan_approval`
   when autonomy ≤ 2) → tasks materialised, `plan_hash` computed.
3. Loop: `get_state` → ready tasks → `execute_task` (activity, retries, non-retryable domain errors)
   → each task builds a fresh `RunContext` (re-resolved scope, policy, agent spec) → agent behaviour.
4. `hypotheses` adds `test:H-*` and `followups:1`; follow-ups add more tests while useful
   (policy max iterations, JEV stop check).
5. `insights` (BH correction, dedupe, numbers guard) → `verify` (REV) → `dataset` → `semantic`
   → `visualize` → `publish_request` (governance review, approval bound to bundle hash + plan hash).
6. Run is `WAITING_USER`; `POST /approvals/{id}/approve` signals the workflow; `publish`
   re-verifies and publishes idempotently; `finalize` writes the summary, episode memory, Neo4j projection.

## Runtime flow: redirect before publication

`POST …/feedback {"text": "Focus only on application incidents"}` → JEV classifies (redirect) and
checks consequentiality → LLM interprets into filters → filters validated against run scope →
`apply_replan(full=True)`: plan_version+1, dynamic tasks removed, hypotheses/insights superseded,
downstream tasks reset, profile/quality reused, approvals invalidated → workflow nudged → new
hypotheses include the filter (`origin: user_redirect`) → a new approval is required.

## Data model

Initial control-plane schema ([migration 0001](../../migrations/versions/0001_control_plane_schema_v1.py));
the following groups describe that baseline, not the complete current table inventory.
Groups: identity & workspace (`app_user`, `workspace`, `workspace_member`, `workspace_policy`);
sources (`source`, `source_asset`, `source_column`, `relationship`); memory (`context_entry` with
`vector(256)`); registries (`agent_definition`, `tool_definition`, `skill_definition`); runs
(`analysis_run`, `run_task`, `run_event`, `agent_message`); audit (`model_call`, `tool_execution`,
`query_execution`, `audit_event`); analysis (`hypothesis`, `experiment`, `insight`); outputs
(`artifact`, `artifact_version`, `lineage_edge`); control (`approval`, `publication`, `feedback`).

Later migrations extend narrative provenance (0002), add schedules/monitors/alerts/notifications
(0003), platform settings and token savings (0004), and crawler state/history (0005); increment 7 adds
verification records (0032), definitions and the outbox (0033), the semantic compiler (0034), Postgres
spend counters (0035), recipes (0036), ML (0037), worker tasks (0038), pipelines and the managed writer
(0039), and brief, steps, branches and notebooks (0040). The
[`migrations/versions`](../../migrations/versions) directory is the schema history; consult it
alongside the ORM models rather than treating the initial table count as current.

## Where each spec §8 module lives

| v1 module | Package |
|---|---|
| Workspace Service | `services/workspaces.py`, `api/routers/workspaces.py` |
| Agent Runtime / Orchestration | `runtime/`, `agents/`, `workflows/` |
| Context Service Adapter / Memory | `context/` |
| Metadata Service | `connectors/`, `agents/metadata.py` |
| Data Access Gateway / Query Engine | `gateway/` |
| Python Sandbox | `sandbox/` |
| Integration / Transformation (minimal) | `staging/`, `agents/sql_agent.py` (virtual dataset) |
| Semantic Model Service | `agents/semantic.py` (+ `artifact` type `semantic_model`/`metric`) |
| Insight Service | `agents/insight.py`, `agents/critic.py` |
| Artifact Registry / Lineage | `artifacts/`, `graph/` |
| BI Publishing Gateway | `publishing/` |
| Tool / Skill Registry | `tools/`, `skills/registry.py` |
| Model Router | `llm/` |
| Policy / Governance | `governance/` |
| Observability / Cost | `model_call`, `tool_execution`, `query_execution`, `/api/admin/usage`, `/api/health` |
| Scheduler | `services/schedules.py` (`analystos scheduler`) |
| Monitoring Engine | `services/monitors.py` |
| Notification Service | `services/notifications.py` (in-app) |
| Reporting | `services/reports.py`, `reports/` (md/html/pdf/xlsx renderers) |
| Semantic compiler (spec v4 §5) | `semantic/` |
| Verification records (spec v4 §6) | `evidence/verification.py`, `evidence/why.py` |
| Definitions and pins (spec v4 §8) | `services/definitions.py`, `services/pins.py` |
| Transformations and pipelines (spec v4 §9) | `recipes/`, `pipelines/`, `services/recipes.py`, `services/pipelines.py` |
| Governed ML (spec v4 §10) | `ml/`, `services/ml.py` |
| Compute workers (spec v4 §4) | `workers/`, `contracts/worker.py` |
| Workbench: brief, steps, branches, notebooks, work orders | `services/brief.py`, `services/steps.py`, `services/branches.py`, `services/notebooks.py`, `services/work_orders.py` |
