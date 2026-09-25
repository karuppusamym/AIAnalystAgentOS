# Runbook — operations

## Health
`GET /api/health` reports Postgres, Redis, Neo4j, Temporal, Superset reachability and whether chat
models and JEV are routable. It reports observations, not SLOs.

## Where to look
| Question | Source |
|---|---|
| What did an agent do? | `agent_message`, `tool_execution` (Agent Console) |
| What did it cost? | `model_call` (per call cost/tokens/latency), `/api/admin/usage` |
| Which data did it read? | `query_execution` (SQL, referenced assets, fingerprint, rows, cache hit) |
| Why was something allowed/denied? | `audit_event` (decision + reasons), `/api/workspaces/{id}/audit` |
| What was published, from what? | `publication`, `artifact.external_id`, `lineage_edge` / Neo4j |

## Recovery
* **Worker crash mid-task** — Temporal re-dispatches the activity; the task key is idempotent and
  stale plan versions are discarded.
* **Partial publication** — `publication.status = partial` with created ids; re-approving/retrying
  the same bundle reconciles by name before creating. To undo: `POST /api/publications/{id}/rollback`.
* **Neo4j lost** — it is a projection; the next `finalize` (or `graph.projection.project_workspace`)
  rebuilds it from Postgres.
* **Rotate OPENROUTER_API_KEY** — update the secret in the environment and restart api/worker; no
  key is stored in the database.

## Budgets
Per workspace policy: `run_token_budget`, `run_cost_budget_usd`, `workspace_monthly_cost_budget_usd`,
`max_queries_per_run`. When exceeded, model calls fail with `budget_exceeded` and agents continue on
deterministic paths; queries beyond the cap fail the task that issued them.
