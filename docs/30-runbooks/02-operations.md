# Runbook — operations

## Health
`GET /api/health` reports Postgres, Redis, Neo4j, Temporal, Superset reachability and whether chat
models and JEV are routable. It reports observations, not SLOs. With the graph projection off (the
default) the `neo4j` check reads `{"ok": true, "status": "disabled"}` and Neo4j is never contacted.

## Where to look
| Question | Source |
|---|---|
| What did an agent do? | `agent_message`, `tool_execution` (Agent Console) |
| What did it cost? | `model_call` (per call cost/tokens/latency), `/api/admin/usage` |
| Which data did it read? | `query_execution` (SQL, referenced assets, fingerprint, rows, cache hit) |
| Why was something allowed/denied? | `audit_event` (decision + reasons), `/api/workspaces/{id}/audit` |
| What was published, from what? | `publication`, `artifact.external_id`, `lineage_edge` (Neo4j only if enabled) |

## Recovery
* **Worker crash mid-task** — Temporal re-dispatches the activity; the task key is idempotent and
  stale plan versions are discarded.
* **Hung task** — activities heartbeat every third of their queue's `heartbeat_seconds`
  (`config/task_queues.yaml`); an attempt that goes silent (a statistic stuck in native code, a
  frozen pool process) is timed out after that window, and the retry releases the dead attempt's
  task claim and runs the step again. A hung compute-pool process keeps its pool slot until it is
  killed; killing it breaks the pool and the compute worker exits, so run it under a restart policy
  (compose `restart`, a Kubernetes Deployment).
* **Backlog on one workload** — scale that pool only (`--scale worker-compute=N`, or the Helm
  `workers.<pool>.replicas`); Temporal's task-queue backlog per `<prefix>-<workload>` shows which.
* **Partial publication** — `publication.status = partial` with created ids; re-approving/retrying
  the same bundle reconciles by name before creating. To undo: `POST /api/publications/{id}/rollback`.
* **Neo4j lost** — it is an optional projection; the neighbourhood falls back to Postgres at once,
  and the next `finalize` (or `graph.projection.project_workspace`) rebuilds it from Postgres.
* **Rotate OPENROUTER_API_KEY** — update the secret in the environment and restart api/worker; no
  key is stored in the database.

## Budgets
Per workspace policy: `run_token_budget`, `run_cost_budget_usd`, `workspace_monthly_cost_budget_usd`,
`max_queries_per_run`. When exceeded, model calls fail with `budget_exceeded` and agents continue on
deterministic paths; queries beyond the cap fail the task that issued them.
