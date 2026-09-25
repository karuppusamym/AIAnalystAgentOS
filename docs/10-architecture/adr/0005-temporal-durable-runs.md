# ADR-0005 — Temporal for durability; Postgres for state

**Status:** accepted

**Decision.** The workflow holds no business state: it loops `get_state → execute ready tasks`,
waits on a `nudge` signal (pause/resume/cancel/approval/feedback) with a periodic re-check.
Tasks have stable keys (idempotency) and a plan_version (stale results discarded). Domain errors
(`PolicyDenied`, `SQLRejected`, `ApprovalRequired`, `BudgetExceeded`, …) are non-retryable.
Publication reconciles by idempotency key before retrying.

**Consequences.** Worker crash → Temporal re-dispatches; the task sees its persisted state.
The UI reads Postgres, never Temporal.

**Amendment (increment 4, P4-S01).** Work is split into task queues per workload — `analysis`,
`compute`, `publish`, `crawl`, `elt` — each served by its own worker pool (`analystos worker
--queues …`). The workflow runs on `analysis` and routes each ready step by kind
(`workflows/queues.py`: profiling, quality and hypothesis tests → `compute`; publication →
`publish`). The compute pool runs activities in a bounded process pool, so CPU-bound statistics never
starve a worker's event loop or heartbeats. Every long activity heartbeats; `start_to_close` and
`heartbeat` timeouts are per queue (`config/task_queues.yaml`) and travel as workflow input, so a
config change never breaks the replay of a run in flight. The run workflow continues-as-new after
`max_activities_per_run` activities. A retry after a timed-out attempt releases that attempt's task
claim (the engine's claim TTL is the fallback for crashes outside Temporal's view).
