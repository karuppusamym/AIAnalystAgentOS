# Product, architecture, UI and API review — 2026-09-25

## Verdict

Keep the governed gateway, modular monolith, durable runs, deterministic skills, artifact lineage,
and hash-bound approvals. They are a credible analytics foundation. The repository demonstrates
bounded analytical workflows; it does not yet demonstrate a general replacement for a data
analyst, data engineer, or data scientist. The next investment should be correctness, workspace
understanding, and complete workflows with independent evaluation, rather than more agent names.

This review updates the design and delivery plan only. No runtime fixes, new endpoints, browser
validation, or new live certifications are claimed. Existing live evidence is historical and
was read, not rerun. Implementation status remains solely in the [tracker](01-tracker.md).

## Findings, ordered by impact

Priority here means delivery urgency: P0 blocks a shared pilot or a stronger trust claim; P1 is
needed for the next complete workflow; P2 follows those foundations.

| Priority | Finding and inspected evidence | Required change | Tracker |
|---|---|---|---|
| P0 | Run detail checks URL workspace ownership, but pause/resume/cancel, feedback, console and events in `api/routers/analysis.py` resolve only `run_id`. `services/runs.get_run_for` checks access to the run's actual workspace, so this is a URL/object consistency defect, not evidence of access by a user with no membership. | One workspace-bound resource resolver for all nested routes; reject mismatches with 404 before reads or writes. Test users with membership in one and both workspaces. | P4-01 |
| P0 | `analysis.events` authorizes before opening SSE and does not recheck membership or token expiry in the stream loop. | Recheck current authorization before each fetched batch; terminate on revocation/expiry. Cover reconnect and concurrent revocation. | P4-01 |
| P0 | Readiness already records a shared Superset reader with workspace permissions not configured, and a Python sandbox that is not a security boundary. | Gate shared publication on destination isolation; gate arbitrary numeric code on container isolation. Prove denial directly in the BI destination and worker. | P4-02 |
| P0 | `agents/insight._guard` flattens values, permits numerals 1/2/3, and does not bind a value to its metric, group, unit, or time window. A real number can support an incorrect sentence. | Typed claim facts with subject/metric/value/unit/window bindings; render numerical clauses from those facts. Add adversarial swapped-group, reversed-direction, and percent-versus-ratio cases. | P4-03 |
| P0 | `agents/critic.py` uses a common sample floor, adjusted significance, same-data second method, and re-run hash for every finding; the confidence value is a hand-built score. These checks do not establish causality, out-of-sample performance, or calibrated probability of truth. | Method-specific evidence states, discovery versus confirmation, data-version binding, and an explicitly uncalibrated review score. Missing evidence must fail closed. | P4-03 |
| P1 | `AnalysisSpec` exposes six methods over one asset. Statistical driver models and a forecasting skill exist, but there is no typed end-to-end training, registry, scoring, and model monitoring lifecycle. | Extend through a separate `MLSpec` and bounded first ML workflow; retain `AnalysisSpec` compatibility. | P5-01..03 |
| P1 | `MetricDef` has owner/status fields, but the tracker already records that metrics lack a complete ownership/approval workflow. Catalog grain/domain are heuristics, and product intent explicitly excludes training models. | Versioned workspace brief, reviewed semantic definitions, entity/time/grain/joins, and task suitability assessment; broaden the target scope explicitly. | P4-04..05 |
| P1 | `create_run` persists a run and then calls `start_run`; its inspected path has no transactional dispatch record. An orchestration outage can leave work without a workflow. Request retries also lack a declared API idempotency contract. | Atomic run + dispatch outbox, stable workflow IDs, reconciliation, and request replay handling. Inject crashes on both sides of dispatch. | P4-06 |
| P1 | One-source analysis and virtual datasets do not provide incremental engineering pipelines, data-contract enforcement, reconciliation, or backfill recovery. | Start with tested read-only transformations and certified joins, then separately approved managed outputs. Keep source mutation denied. | P6-01..03 |
| P1 | `WorkspaceHome.tsx` starts from objective/source/autonomy; `Layout.tsx` has eleven peer destinations. `web/README.md` records missing glossary/column curation and static dashboard filters. | Guided brief → readiness → plan → results/evidence → operation; retain specialist tools behind secondary navigation. | P4-07 |
| P1 | List runs is capped at 50; the UI maintains a handwritten API client. There is no documented common concurrency, cursor, or capability contract for the expanded workflows. | Additive typed APIs, pagination, revision checks, capability discovery, and generated client verification. | P4-06 |
| P1 | Existing evidence covers planted ServiceNow and retail effects and selected engines. It cannot establish superiority on unfamiliar schemas, ambiguous business definitions, engineering failures, or ML leakage. | Sealed multi-domain benchmark plus paired human baseline, failure cases, costs, abstention, and real pilot outcomes. | P4-08 |

Additional observations: the original architecture counted only migration 0001 even though
migrations 0002–0005 exist; this documentation drift is corrected. A completed feature row does
not imply pilot or production readiness. Connector catalog entries remain distinct from tested
and certified connectors.

## Refined design documents

- [Workspace data-team specification](../00-intent/03-workspace-data-team-spec.md): scope,
  adaptation, analytical validity, engineering, ML and outcome ownership.
- [Workbench UX](../10-architecture/02-workbench-ux.md): user journeys, screens and UI acceptance.
- [API evolution](../20-contracts/02-workbench-api.md): resources, payloads, concurrency and failure semantics.
- [ADR-0011](../10-architecture/adr/0011-workspace-workflows-and-evidence.md): shared execution
  architecture and staged extension of the current boundaries.
- [Evaluation gates](05-evaluation-plan.md): evidence required to say the product performs better.

## Recommended sequence

1. Close P0 trust and isolation gaps, and establish the evaluation baseline.
2. Ship one complete workspace-adaptive analyst workflow, including semantics, preflight and UX.
3. Add supervised tabular ML and forecast backtesting, with held-out evaluation and batch scoring.
4. Add tested engineering pipelines and managed outputs; expand connectors only against demand
   and certification evidence. External delivery and existing-dashboard editing remain follow-ons.

The first release slice should answer an unfamiliar business question correctly, disclose when
it cannot, and make the result reusable. Success on that slice is the gate to expanding autonomy.
