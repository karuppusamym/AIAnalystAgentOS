# Architecture

See [spec v2](../00-intent/02-spec-v2.md) for the rules; this document shows the structure and the
runtime flows. Decisions are recorded as ADRs in [`adr/`](adr/).

## Deployment view (compose / Kubernetes)

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

Process roles share one image (ADR-0001). The API never runs agent work in the request; it writes
state and signals the workflow.

## Component view

```
agents ──uses──▶ skills (deterministic)      agents ──invoke──▶ tools.ToolRuntime ──policy──▶ governance
   │                 │                                               │
   │                 └── RunSQL ──▶ gateway (validator → executor → cache → audit) ──▶ sources
   ├── llm.router (purposes → profiles → allowlisted models)  ├── llm.jev (typed decisions)
   ├── artifacts (versions + lineage) ──▶ graph (Neo4j projection)
   └── publishing (BIPublisher: superset | preview)
runtime.engine ◀── workflows (Temporal | local) ; runtime.context re-resolves scope per task
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

29 control-plane tables ([migration 0001](../../migrations/versions/0001_control_plane_schema_v1.py)).
Groups: identity & workspace (`app_user`, `workspace`, `workspace_member`, `workspace_policy`);
sources (`source`, `source_asset`, `source_column`, `relationship`); memory (`context_entry` with
`vector(256)`); registries (`agent_definition`, `tool_definition`, `skill_definition`); runs
(`analysis_run`, `run_task`, `run_event`, `agent_message`); audit (`model_call`, `tool_execution`,
`query_execution`, `audit_event`); analysis (`hypothesis`, `experiment`, `insight`); outputs
(`artifact`, `artifact_version`, `lineage_edge`); control (`approval`, `publication`, `feedback`).

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
| Scheduler / Monitoring / Notification | Phase 3 (not built; endpoints return an explicit error) |
