# Workspace brief, readiness, steps, branches and notebooks — API shapes

Implemented 2026-09-26 for P4-04 (brief, readiness, Start-work job kinds, scoped memory), P7-04 (steps,
self-checks, pins), P7-05 (branches, backend) and P7-12 (notebooks, backend). The
[tracker](../60-delivery/01-tracker.md) is the status authority. Models are Pydantic in
`src/analystos/contracts/brief.py` and `contracts/step.py` (exported to `contracts/workspace_brief`,
`readiness_assessment`, `job_kind`, `step` and `branch` `.schema.json`). All paths start
`/api/workspaces/{workspace_id}`; every child id is bound to that workspace (another workspace's id is 404),
and a step or branch is also bound to its container (a run, a **private** Ask thread, a notebook).

Errors use the existing envelope `{error: {code, message, retryable, details}}`. New code:
`readiness_blocked` (409). `unsupported_capability` (422) is reused for job kinds without an executor.

## 1. Workspace brief (P4-04)

| Route | Role | Result |
|---|---|---|
| `GET /brief[?version=N]` | viewer | `WorkspaceBriefDoc`; `ETag: "<version>"` (0 = no brief yet) |
| `GET /brief/versions` | viewer | `[{version, content_hash, reason, created_by, created_at, assertions}]`, newest first |
| `POST /brief/suggestions` | analyst | merges deterministic suggestions and checks; `{...brief, added[], updated[], new_version}` |
| `PATCH /brief` + `If-Match` (required) | editor | `BriefPatch {ops: [{op: set\|review\|reject\|remove, key?, assertion?, note?}], reason?}` → new version `{...brief, changed[], impact: {readiness_assessments_outdated}}`; 428 without If-Match, 412 when stale |
| `POST /memory` `{query, limit?}` | viewer | `{items[], withheld, brief[], scope_hash}` — only memory the caller's scope may see |

`Assertion {key, group, field, subject?, value, origin: source|rule|model|user, evidence: [{kind, ref, detail?}],
review_state: suggested|reviewed|validated|rejected, version, confidence?, note?, updated_by, updated_at}`.
Groups and their fields: `decision` (question, owner, intended_action, audience, acceptance_rubric),
`domain` (term, entity, alias, prohibited_interpretation), `data_semantics` (grain, entity_key,
join_cardinality, dedup_rule, exclusion_rule), `time_measures` (event_time, availability_time, timezone,
fiscal_calendar, unit, currency, numerator, denominator, aggregation), `ml_objective` (target,
prediction_moment, horizon, label_availability, error_costs, evaluation_metric), `constraints`
(source_scope, destination_scope, freshness_tolerance_hours, residency, retention, compute_budget,
query_budget), `knowledge` (context_ids, metric_versions, accepted_corrections). Keys are
`<group>.<field>[:<subject>]`, e.g. `data_semantics.grain:sn.incident`.

UI rules: only `reviewed`/`validated` assertions are facts; show `suggested` ones as questions with their
origin and evidence; a person's assertion (`origin: user`) or review is never overwritten by a refresh.

## 2. Start work: job kinds (P4-04)

`GET /capabilities` → `{workspace_id, digest, job_kinds: [JobKindAvailability]}` with
`{key: explain|compare|forecast|predict|prepare|monitor, label, work_order_kind, available,
reasons: [{code, message, remediation}], capabilities: [{id, ref, kind, usable, reason, certification}],
entry: {type: work_order|recipe|monitor, payload_type?, route}, readiness_checks[], min_role}`.
Reason codes: `no_executor`, `not_registered`, `capability_unusable`, `role`, `no_data`. A choice with
`available: false` must be shown disabled with its reasons, never started.

## 3. Readiness (P4-04)

| Route | Role | Result |
|---|---|---|
| `POST /readiness` `ReadinessIn {job_kind, assets[], target?, time_column?, horizon?, measures[]}` | viewer | 201 `ReadinessAssessmentDoc` |
| `GET /readiness/{assessment_id}` | viewer | the stored assessment |
| `POST /work-orders/{work_order_id}/assessments` | analyst | 201, assessed from the work order's current revision |

`ReadinessAssessmentDoc {id, job_kind, status: ready|needs_input|blocked|unsupported, brief_version,
work_order_id?, work_order_revision?, checks: [{check, status: pass|fail|needs_input|warn|not_applicable|unsupported,
required, reason, remediation?, subject?, evidence[]}], inputs, inputs_hash, alternatives: [{job_kind,
requires_explicit_choice: true, note}]}`. Checks: capability, scope, freshness, schema_drift, grain,
key_uniqueness, join_fanout, coverage, missingness, label_availability. Only `required` checks decide the
status; advisory ones are shown as is. `POST /work-orders/{id}/runs` assesses first: `unsupported` → 422
`unsupported_capability`, `blocked` → 409 `readiness_blocked` (details: `assessment_id`, failing `checks`,
`alternatives`); `needs_input` may start.

## 4. Steps and the Data Thread (P7-04)

`Step {schema_version, id, version, current_version, kind: plan|query|method|recipe|train|chart|claim, title,
status: pending|ok|flagged|failed|unsupported|recorded, spec, spec_hash, receipts[], result_snapshot:
ArtifactRef {kind: artifact, id, version, content_hash, media_type}?, chart_spec?, checks: [{check, passed,
severity, detail, evidence, corrected}], corrections: [{check, reason, from_sql, to_sql, round}],
verification_record: {state: ACTIVE|VOID|SUPERSEDED|PENDING|LEGACY, badge, verdict, void: {kind, reason}?...}?,
depends_on[], inputs {step_id: version_id}, branch_id, container {type, id}, seq, origin, forked_from?,
reason: created|edited|rerun|upstream_changed|forked|ingested, error?, inherited}`.

| Route | Role | Result |
|---|---|---|
| `GET /threads/{container_type}/{container_id}` (`run`, `ask_thread`, `notebook`) | viewer (Ask: author) | `{branch, steps[], branches[]}` (main branch) |
| `POST /threads/{run\|ask_thread}/{id}/ingest` | viewer (Ask: author) | records the run (plan, hypotheses, findings) or answered turns as steps; idempotent |
| `GET /branches/{branch_id}` | viewer | `{branch, steps[]}`: inherited steps (fork-time versions) then its own |
| `POST /branches/{branch_id}/steps` `StepIn {kind, title?, spec, depends_on[], chart_spec?}` | analyst | 201 the executed step |
| `GET /steps/{step_id}[?version=N]` | viewer | `Step` + `result` (snapshot content); `ETag` = current version |
| `GET /steps/{step_id}/versions` | viewer | `{step_id, current_version, versions: [Step]}` newest first; old versions keep their (VOID) verdicts |
| `PATCH /steps/{step_id}` `StepEdit {spec?, title?, chart_spec?}` + `If-Match` | analyst | `{step, rerun: [Step], voided_records[]}` |
| `POST /steps/{step_id}/runs` | analyst | same shape: explicit re-verification on today's data |

Specs: `query` `{sql, source_id?}` or `{semantic_query}`; `method` `{analysis_spec}` or `{cell: python, code}`;
`chart` `{chart: {type, x, y}}` over one upstream step; `claim` `{text}` (numbers must bind to upstream
results when it depends on steps); `plan`, `recipe`, `train` are recorded. Self-checks: `empty_result`,
`magnitude`, `truncation`, `grouping`, `fanout`, `dq_gate` (and `numbers` for claims, `chart_fields` for
charts). A safely correctable failure is shown with `corrected: true` and a `corrections[]` entry;
otherwise the step is `flagged` and its verdict is `failed_verification`.

## 5. Pins (P7-04)

| Route | Role | Result |
|---|---|---|
| `POST /steps/{step_id}/pins` `PinIn {target: tile\|schedule, version?, dashboard?, destination?, name?, cron?, timezone, approval_id?}` | editor | 202 `{status: approval_required, approval_id, payload_hash, frozen}` then (approved id) 201 `{status: pinned, pin}` |
| `GET /pins/{pin_id}` | viewer | `{...pin, state: {state: current\|upgrade_available\|blocked, step_current_version, pinned_version, items[], blocking[]}}` |
| `POST /pins/{pin_id}/refresh` | analyst | replays the frozen query: `{sql\|query_ids, columns, rows (≤50), result_hash, pin_state, changed_since_pin, ...}` |

Only an `ok` version with an ACTIVE verified record can be pinned. A schedule pin creates a `step` schedule
(`config {pin_id, frozen_hash, approval_id}`, re-verified at each fire, pinned via ADR-0021).

## 6. Branches (P7-05)

| Route | Role | Result |
|---|---|---|
| `POST /branches/{branch_id}/forks` `ForkIn {from_step_id, name?, include_downstream, spec?}` | analyst | 201 `{branch, steps[], edited?}` |
| `GET /branches/{branch_id}/compare?with={other}` | viewer | `{a, b, steps: [{match: shared\|equivalent\|diverged\|only_a\|only_b, root, a, b, spec_diff, numbers: {same_result, headline {a, b, delta}, row_count, cells: [{key, column, a, b, delta}], stat?}, verdicts {a, b}}], summary}` |
| `POST /branches/{branch_id}/merge` `MergeIn {report_id?, title?}` | analyst | `{report: {id, version, name, content: {kind: data_thread, title, sections[], branches[]}}, branch, included, excluded[]}`; 403 `policy_denied` when a step's verdict is VOID |

## 7. Notebooks (P7-12)

| Route | Role | Result |
|---|---|---|
| `POST /notebooks` `{title}` / `GET /notebooks` | analyst / viewer | notebook / list |
| `GET /notebooks/{notebook_id}` | viewer | `{id, title, revision, branch_id, cells: [Step + {cell: markdown\|sql\|python, source}]}`; ETag = revision |
| `POST /notebooks/{notebook_id}/cells` `CellIn {cell, source, title?, depends_on?, source_id?}` | analyst | 201 the executed cell (a Step) |
| `PATCH /notebooks/{notebook_id}/cells/{step_id}` `CellEdit {source, title?}` + `If-Match` (cell version) | analyst | like a step edit |
| `POST /notebooks/{notebook_id}/runs` | analyst | every cell once more, in order: `{...notebook, executed[]}` |

Python cells see `inputs[<step id>]` and `inputs["cell<N>"]` (rows as records) and set `result`; imports are
limited to math, statistics, json, numpy, pandas; they run in `services/steps.execute_python` (the sandbox
now, the P7-06 compute pool later).

## 8. Events

`brief.updated`, `readiness.assessed`, `step.created`, `step.edited`, `step.executed`, `step.flagged`,
`step.pinned`, `step.pin_refreshed`, `branch.forked`, `branch.merged`, `notebook.created`, `notebook.updated`,
plus `verification.recorded` / `verification.voided` for step verdicts (subject_type `step`).
