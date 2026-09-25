# Workbench API evolution — target contract

Design only, 2026-09-25. Routes below are proposed additions unless explicitly called existing.
They are not callable promises or generated schemas. Implement Pydantic models first, export
schemas and OpenAPI, then generate the client. See [tracker](../60-delivery/01-tracker.md),
P4-01/P4-06, P5 and P6. Keep current `/api/.../analysis` clients compatible during rollout.

## 1. Shared semantics

- Every nested resource is resolved with both workspace and object ID, then current permission.
  An object outside the path workspace returns 404, including for a member of both workspaces.
- Preflight and execution independently resolve scope. A readiness token is not authorization.
  Downloads, cached previews, worker tasks and SSE batches recheck access too.
- Mutating creates/start commands require `Idempotency-Key`. Scope keys by principal, workspace
  and operation; atomically store canonical payload hash and result. Same key/body returns the
  original resource, changed body returns 409. Retain records at least 7 days and through active
  execution; document the retention bound. Stable operation IDs prevent repeated worker effects.
- Editable resources return an ETag revision; PATCH requires `If-Match` (428 if missing, 412 if
  stale). Semantic revisions and executed specs are immutable; editing creates a new revision.
- New lists use `limit` (default 50, max 200), opaque cursor, stable order `(created_at, id)` and
  `{items, next_cursor}`. Bind cursors to workspace/query; authorize every request. Preserve old
  array endpoints until callers migrate, rather than silently changing their response shape.
- Long execution returns 202 with a persisted operation and `Location`; committed short resource
  creation returns 201. Execution occurs in durable workers, not in request handlers.
- Preserve the current error envelope `{error: {code, message, retryable, details}}`; add a
  correlation `request_id`. Do not include secrets or denied data in errors. Add stable codes
  `unsupported_capability`, `readiness_blocked`, `stale_input`, `idempotency_conflict`, and
  `precondition_failed`. Validation errors remain 422; limits use 429 with retry timing when known.

## 2. Resources and routes

All paths start `/api/workspaces/{workspace_id}`. New resources carry `schema_version`, ID,
workspace, revision, timestamps and provenance where applicable.

| Route | Payload / result | Authority and behavior |
|---|---|---|
| `GET /capabilities` | Job kinds, methods, prerequisites, limits, certification/evaluation references, policy availability and reason codes | Viewer; report implemented and allowed capabilities, never infer availability from catalog presence |
| `GET /brief` / `PATCH /brief` | `WorkspaceBrief`, ETag / new revision and dependency impact | Viewer / editor; use reviewed context and semantic references |
| `GET /semantic-models` / `POST /semantic-models` | Versioned entities, grains, metrics, units and joins | Viewer / editor; creates draft, never self-approves |
| `POST /semantic-models/{id}/validate` | Operation ID with uniqueness, fanout, reconciliation and definition results | Analyst; bounded gateway reads |
| `POST /semantic-models/{id}/approval-requests` | Immutable version plus validation evidence | Editor proposes; existing approval service decides and rechecks |
| `POST /work-orders` / `GET /work-orders/{id}` | Typed `WorkOrderSpec` draft / detail and revision | Analyst create; authorized viewer read |
| `PATCH /work-orders/{id}` | Objective, expected outputs, typed spec or budget; returns new draft revision | Analyst; If-Match; changes invalidate previous assessments |
| `POST /work-orders/{id}/assessments` | Revision → readiness checks, missing input, assumptions, estimates and assessment ID | Analyst; bounded reads, async operation when needed |
| `POST /work-orders/{id}/runs` | Expected revision, assessment ID and input-version manifest → run operation | Analyst; assessment must match inputs and be within validity window; recheck policy and limits |
| `GET /operations/{id}` | State, stage, result reference, safe error, retry/cancel links | Authorized viewer; no guessed progress percentage |
| `GET /evidence-bundles/{id}` | Typed facts, checks, provenance, limits, evidence schema version | Viewer under current underlying data policy |
| `GET /datasets/{id}/versions` | Immutable manifests, schema/semantics, checks and retention | Viewer; scope applies to each version |
| `POST /datasets/{id}/previews` | Version, typed filters and grouping → bounded operation/result with evidence | Analyst; gateway validation, limits and scope-partitioned cache |
| `GET /experiments` / `GET /models/{id}/versions` | Experiment comparisons / trusted package metadata and model card | Viewer; P5 only |
| `POST /models/{id}/score-requests` | Model/input versions, destination, validation and approval reference | Editor; P5 only; verify approval before effects |
| `GET /pipelines/{id}/versions` / `POST /pipelines` | Versioned `PipelineSpec` | Viewer / editor; P6 only |
| `POST /pipelines/{id}/validation-runs` | Input manifest and dry-run limits → operation | Analyst; read-only execution |
| `POST /pipelines/{id}/materialization-requests` | Version, destination, bounded range, approval reference | Editor; P6 only; dedicated writer and revalidation |

Internal semantic approval and external publication approval are distinct purposes with separate
payloads. Reuse existing approval records/authorization, but do not let one approve the other.
Managed scoring/materialization requests do not authorize writes without matching approval.

## 3. Example: forecasting work order

Illustrative request to `POST .../work-orders` (identifiers refer to existing authorized versions):

```json
{
  "schema_version": 1,
  "kind": "forecast",
  "objective": "Forecast weekly order volume for the next four complete weeks",
  "brief_revision": 3,
  "inputs": [{"dataset_id": "ds_orders", "version": 7}],
  "semantic_version": "sem_orders:2",
  "spec": {
    "type": "ml",
    "task": "forecast",
    "target_metric": "orders_count",
    "time_column": "week_start",
    "horizon": 4,
    "frequency": "week",
    "split": {"strategy": "rolling_origin", "gap_periods": 0},
    "baseline": "seasonal_naive",
    "selection_metric": "mae",
    "seed": 42
  },
  "budget": {"max_wall_seconds": 900, "max_trials": 10, "max_queries": 40},
  "outputs": ["forecast", "evaluation", "model_card"]
}
```

This is a draft: assessment must resolve required seasonal period, minimum training history,
cutoffs, final holdout and label completeness from the brief or explicit input before starting.
The server clamps budgets to policy, validates method prerequisites and records effective limits.
It never accepts client scope hashes or evidence verdicts as proof of authorization/correctness.

## 4. Execution, events and recovery

Creating an executable run atomically writes the run, immutable input manifest, idempotency
record and dispatch outbox in Postgres. A dispatcher starts/signals Temporal using a stable run
ID; duplicate delivery converges. A reconciler repairs unsent/orphaned dispatches. Persist final
results and the completing event together. This provides retry-safe effects, not a blanket
exactly-once guarantee across external systems.

Operation states: `QUEUED`, `RUNNING`, `WAITING_INPUT`, `WAITING_APPROVAL`, `PAUSE_REQUESTED`,
`PAUSED`, `CANCEL_REQUESTED`, `SUCCEEDED`, `FAILED`, `CANCELLED`. Expose a documented mapping to
existing run states; do not rename stored states in place. Resume uses the same immutable spec
and current policy. A changed spec creates a new plan/run version with approval invalidation.

Reuse persisted SSE with monotonic event IDs and `Last-Event-ID`/`after_id`. Add a versioned
envelope with workspace, operation, run/plan revision, event type and timestamp. Consumers dedupe
by ID and never apply an older revision over a newer snapshot. On an expired replay cursor,
return a reset instruction with the current authorized snapshot/cursor. Authenticate before
each emitted batch and stop after token expiry or membership revocation. Reconnect is not a way
to recover denied historical payloads.

Transient worker failures retry only within budget. Bad input, denied policy, invalid evidence
and exhausted budgets need correction, not an automatic retry loop. Cancellation propagates
to source query cancellation where supported; otherwise stop downstream work and label the
query as draining under its timeout. Artifact orphans expire through retention cleanup.

## 5. Contract acceptance

Cover dual-workspace path mismatch, revocation during streams, cross-tenant cache/download denial,
stale revisions/approvals, duplicate requests, crash-before/after-dispatch, event replay,
pagination during inserts, cancellation, source-version changes and malformed task variants.
Compatibility tests run existing analysis clients alongside the new generated client. OpenAPI
diff checks must flag breaking changes before release. Never hand-edit generated JSON schemas.
