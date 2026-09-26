# AgentSwarms comparison and first implementation (2026-09-26)

## Product fit

AgentSwarms combines an analyst chat, agent canvas, ETL, lakehouse, dashboards and ML in one
TypeScript/Supabase application. AnalystOS already has a deeper investigation workflow: governed
queries, deterministic statistics, reproducible evidence, approval-bound publication and Temporal
execution. Its main product gap is the day-to-day analyst experience around individual steps and
clearer entry points, not another runtime or data store.

| Area | AgentSwarms observation | AnalystOS state | Decision / product effect |
|---|---|---|---|
| Analyst trace | Each question has visible SQL steps, charts and a review mark. | Run tasks, query receipts and facts exist, but step edit and re-run is pending. | Build step objects on current receipts (P7-04), then show a number's full evidence path (P7-08). The analyst can inspect and correct work without losing provenance. |
| Semantic answers | Named metrics compile into queries and show their governed status. | Approved definitions exist; Ask still generates SQL for metric questions. | Compile a validated semantic query and visibly label ad hoc answers (P7-02). Business definitions stay stable across users and schedules. |
| Scheduled analysis | Re-executes pinned SQL and computes changes. | Hypothesis replay exists; definition-version pinning is pending. | Publish immutable definitions and pin schedule inputs (P7-03). A scheduled KPI cannot silently change meaning after an edit. |
| Access and safety | One policy layer is intended across surfaces. | AnalystOS has an AST-based gateway and hash-bound approvals, but PostgreSQL/T-SQL function and join gaps were measured. | Harden the gateway in this branch (P7-15). Keep every query path through it. |
| First use | A checklist and status band help users start. | Nineteen screens, with no guided first-run path. | Implement a state-based Overview and Start work entry from the registry (P7-18), without adding top-level screens. |
| Platform breadth | Built-in lakehouse, BI and ML enlarge deployment and navigation. | Source pushdown and Superset already serve the core use case. | Keep the lite deployment and workbench plan (P7-16/18); add governed ML and pipelines as job types when their contracts are ready. |

## Reuse boundary

The downloaded AgentSwarms source is Elastic License 2.0. This branch copies no source, tests,
prompts or assets from it. It implements the safety checks in AnalystOS's Python gateway from the
already recorded gap analysis. Bringing its services or Supabase schema into AnalystOS would create
a second authorization and execution path. The detailed eight-pattern review is in
[the build-right study](2026-09-26-build-right-study.md); dispositions and dependencies are in the
[tracker](../60-delivery/01-tracker.md).

## This branch

The PostgreSQL and SQL Server profiles now reject unrecognized function calls and bind/session
variables. The allowlist covers only known functions needed by AnalystOS's own SQL compiler. The
validator rejects table hints and joins without a column-bearing predicate, including comma joins,
`CROSS JOIN`, `ON TRUE` and `ON 1 = 1`. It continues to accept keyed `ON` and `USING` joins. The
gateway still parses and regenerates SQL before execution.

This is a first slice of P7-15. The 106-case adversarial corpus and its expectations file are not
yet checked into CI, and live SQL Server and PostgreSQL source certification is still needed. The
stricter rule can reject previously accepted custom functions and deliberate Cartesian analysis;
those should be expressed as reviewed capabilities rather than silently exempted at the gateway.
