# ADR-0005 — Temporal for durability; Postgres for state

**Status:** accepted

**Decision.** The workflow holds no business state: it loops `get_state → execute ready tasks`,
waits on a `nudge` signal (pause/resume/cancel/approval/feedback) with a periodic re-check.
Tasks have stable keys (idempotency) and a plan_version (stale results discarded). Domain errors
(`PolicyDenied`, `SQLRejected`, `ApprovalRequired`, `BudgetExceeded`, …) are non-retryable.
Publication reconciles by idempotency key before retrying.

**Consequences.** Worker crash → Temporal re-dispatches; the task sees its persisted state.
The UI reads Postgres, never Temporal.
