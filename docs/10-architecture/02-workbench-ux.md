# Workspace workbench — UI design

Target design, 2026-09-25; implementation is tracked under P4-07, P5-03 and P6-03 in the
[tracker](../60-delivery/01-tracker.md). This is an interaction specification, not a screenshot
or claim that these screens exist. Existing routes continue to work during migration.

## 1. Organize around the user's work

Keep the workspace selector, then group navigation into Overview, Data, Work, Outputs and
Operate. Put policy/members and administration in Settings. Data contains sources, catalog,
definitions and quality; Work contains investigations and, when available, experiments and
pipelines; Outputs contains datasets, insights, dashboards, reports and models; Operate contains
schedules, monitors and approval inbox. SQL and the agent console remain specialist tools.

The primary action is **Start work**, with task choices Explain, Compare, Forecast, Predict,
Prepare data and Monitor. Availability comes from the API capability registry. Unsupported
choices show the missing requirement and cannot start a pretend run. Do not expose a numeric
autonomy level as the first decision a business user must understand; keep it in advanced controls
with the actual policy limits described in plain language.

Business users see objective, answer, evidence, limitations and next action. Specialists can
expand SQL, statistical diagnostics, experiment comparisons and pipeline steps. Both views use
the same artifacts and authorization; switching presentation never changes permissions.

## 2. Workspace overview and onboarding

The overview answers: What data is available? What does it mean? What is current? What needs me?
Show the business objective, data freshness, unresolved semantic questions, work status, latest
accepted outputs and pending decisions. Counts of agents, tokens and artifacts are secondary.

Onboarding is resumable:

1. Connect/upload; show connector support evidence, source scope and staged versus live access.
2. Discover/select; show actual selected assets, size, freshness and denied fields.
3. Review a short data brief: row grain, entities, joins, time, units and key business definitions.
4. Choose an outcome, metric and acceptance criteria; ask for target/horizon only for ML tasks.
5. Preflight; show blockers, assumptions, approximate cost/time and allowed next actions.

Do not require every optional business field before a simple descriptive query. Require the
fields used by the chosen method. If a source fails to load, preserve the entered brief and show
an actionable retry, not “no sources” as if the request succeeded.

## 3. Workbench layout

```text
Workspace / Objective                         Data as of ...   Budget ...
---------------------------------------------------------------------------
Question and brief        Plan / Results / Evidence / Activity     Details
                          -------------------------------         -------
Selected data             Answer with scope and limitations       Fact
Definition revisions      Chart + accessible table                SQL
Open questions            Checks and excluded data                Method
                          Next action and owner                   Lineage
---------------------------------------------------------------------------
Follow-up / Correct definition / Compare run / Pause / Cancel
```

On narrow screens, these become sequential panels with a persistent work status and one primary
action. The agent/task DAG is available under Activity; it is not the default answer screen.

Before starting, a plan preview lists what will be read, tested and produced; assumptions;
budget bounds; external effects; validation; and likely reasons for abstention. Readiness errors
link directly to the definition/source that needs attention. Only policy-required plans ask for
approval. Starting disabled work explains why instead of sending a predictably failing request.

During execution, show meaningful stages and current work, not invented percentage completion.
Pausing says “Pause requested” until the current safe boundary is reached; cancelling explains
whether a query is still stopping and preserves completed evidence. A disconnected event stream
shows “Reconnecting” with last-update time and recovers from the persisted cursor.

Results separate **What was observed**, **How it was checked**, **What remains uncertain** and
**What action is proposed**. A valid “insufficient evidence” outcome includes the missing data or
power needed. Completion never requires a quota of interesting findings on arbitrary data.

## 4. Evidence and semantic editing

Every numerical statement and chart links to a fact drawer: metric definition, unit, population,
window, denominator, source version, query, method, uncertainty and excluded rows. Show evidence
dimensions independently; never present an uncalibrated review score as “92% chance this is true”.
Old `verified` findings retain their historical verification version and are labeled accordingly.

The definitions editor supports column curation, glossary aliases, metric ownership, grain,
joins and approvals. An edit previews affected runs, datasets, metrics, charts and models;
saving makes a new revision. Concurrent edits show a diff instead of overwriting. Recompute is
explicit and produces new outputs; historical approved results remain inspectable.

Chart filters either re-query through the governed preview API, displaying applied filters and
new evidence, or are visibly noninteractive. A UI filter must never merely change its selected
value while leaving numbers unchanged. Expose keyboard access, table alternatives, clear units,
empty versus zero values, and uncertainty where applicable.

## 5. Specialist journeys

| Journey | Required UI | Completion condition |
|---|---|---|
| Analyst | Brief, definitions, plan, results, evidence drawer, compare runs, approval preview | An owner can reproduce and accept the answer; unanswered questions are visible |
| ML | Target/time checks, split diagram, baseline/experiment table, holdout results, model card, scoring preview | An eligible version passes its declared evaluation before approval for scoring |
| Engineer | Source-to-output DAG, transformation diff, join diagnostics, contract checks, reconciliation, backfill preview | Candidate output passes checks; failure retains last good version |
| Operator | Failures, freshness, approvals, budget, scheduled work, recovery and rollback | A failed run has a reason and a safe action; no blind repeated retries |

ML comparisons show evaluation split/version and metric direction together. Never rank scores
from different holdouts as if comparable. Mark the final holdout as consumed once inspected.
Pipelines show late data and quarantined rows; those rows must not disappear inside a green count.

Approval preview names exact destination, audience, artifact/model version, data scope, changes,
cost envelope and rollback limits. A stale proposal becomes non-executable with a reason and a
link to its replacement. Role-based UI affordances reflect server authority, including revocation.

## 6. Acceptance scenarios

- A first-time user completes connect → definitions → first useful result without consulting
  the agent console. Record time, misunderstandings and human interventions in the pilot.
- Duplicate clicks/reconnects create one operation; refresh restores the same work and draft.
- Ambiguous order versus order-line grain blocks an affected revenue metric and links to a fix.
- Missing labels blocks prediction; the user may explicitly choose a supported descriptive task.
- Swapped groups, incorrect units, stale data and failed checks cannot receive a confirmed badge.
- Scope changes/revocation stop affected reads and invalidate publication previews.
- Keyboard-only users can complete the primary journey; focus, errors and state updates are
  announced. Charts have equivalent labeled tables and no meaning conveyed by color alone.
- Browser tests exercise narrow and desktop layouts, loading, denied, empty, stale, disconnected,
  partial failure, cancellation and successful states against the real API contract.

Keep `web/README.md` as the implemented screen map. Update it only as each target journey ships.
