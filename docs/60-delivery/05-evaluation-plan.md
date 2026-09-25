# Evaluation plan — prove useful work on unfamiliar data

Proposed gates, 2026-09-25; no measurements in this document are claimed results. The tracker
owns implementation status (P4-08, P5-02 and P6-02). Evidence is appended to the capability
register with commit, environment, fixture/snapshot versions, seeds, policy and cost settings.

## 1. Unit of evaluation

Evaluate a complete business task: input data + brief + allowed actions → accepted output or
correct abstention. A passing SQL query, attractive dashboard or matching second model does not
establish task success. Each case has a sealed rubric prepared independently of the run under
test. Record all cases, retries, failures, unsupported requests and interventions.

Freeze development and held-out sets before tuning. Evaluation answers/labels are unavailable
to the planner and model prompts. Rotate a consumed holdout before using it as a fresh gate.
Compare versions on identical data, permissions, budget and provider settings; report provider
variability using repeated runs rather than cherry-picking the best seed.

## 2. Initial benchmark suite

Start with at least 60 held-out tasks spanning retail, SaaS subscriptions, IT operations,
logistics and finance operations, with at least 10 tasks per domain. Target 20 analyst tasks,
15 engineering tasks, 15 ML/forecast tasks and 10 unsupported/insufficient-data tasks. Early
releases run their declared supported subset and publish the remaining coverage explicitly;
they cannot claim the full suite's capability from that subset.

| Suite | Required challenges |
|---|---|
| Analyst | Null effects, practical versus statistical significance, Simpson reversals, ambiguous denominators, duplicate joins, timezone/partial-period errors, missing data, incorrect units, changing sources |
| ML | Target/post-outcome leakage, repeated entities, time drift, imbalanced labels, unseen categories, sparse history, delayed labels, baseline wins, weak slices and invalid prediction-time features |
| Engineer | Schema evolution, fanout, updates/deletes, late arrivals, duplicate batches, overlapping schedules, corrupt records, crash/retry and backfill reconciliation |
| Transfer | Unfamiliar table/column names, supplied semantic mappings, new domains and irrelevant context; preserve correct behavior under equivalent renaming/reordering |
| Governance | Cross-workspace IDs, revoked access mid-stream/run, denied downloads/caches, expired approvals, injected catalog instructions and denied destinations |
| UX/recovery | First-time onboarding, ambiguous input correction, reconnect, stale edit, cancellation, failed dependency and safe rerun |

Synthetic planted effects remain useful for known truth and null controls. Add licensed public
or owner-approved representative data and actual pilot tasks before extrapolating to production.
Do not infer a population false-positive rate from a handful of null controls.

## 3. Proposed acceptance gates

These are initial product targets. Freeze task-specific tolerances and thresholds before a
release evaluation; revise them with versioned rationale rather than after seeing failures.

| Measure | Definition | Initial gate |
|---|---|---|
| Governance | Unauthorized read/effect or approval bypass in the regression corpus | Zero observed violations; every failure blocks release; this is not a guarantee of zero future risk |
| Facts | Numerical clauses whose metric/group/unit/window match authorized computed evidence | 100% of emitted numerical claims; uncertain claims are omitted or explicitly qualified |
| Task success | Independently accepted deliverables / supported held-out tasks | At least 90%, with per-domain denominators and uncertainty intervals; correct abstention only counts when rubric requires it |
| Inappropriate confidence | Confident answers on the unsupported/insufficient cases | Zero in the release corpus; report refusal accuracy and coverage together |
| Scientific validity | False discovery, effect coverage and predictive error under each declared method | Predeclared method-specific simulations and bounds; no universal p-value or sample-size gate |
| ML promotion | Improvement over simple baseline on untouched final data | Meet predeclared business-relevant improvement and uncertainty/slice guardrails, or retain baseline and abstain from promotion |
| Engineering | Reconciliation and retry equivalence | Exact for counts/keys; predeclared tolerances for numeric measures; failed candidate never replaces last good output |
| Reproducibility | Same immutable inputs/spec/runtime → same accepted result | All deterministic fixtures; seeded stochastic runs within declared tolerance; non-replayable source cases labeled |
| Efficiency | Human effort, elapsed time and total measured cost per accepted task | Report p50/p95; target at least 50% less human effort with no lower accepted quality versus paired baseline |

Report component failures and intervention counts, not just an aggregate score. Record warehouse
scan/cost when available, CPU/memory/wall time, model spend and estimated human-review cost
separately. Unknown infrastructure cost cannot be presented as zero.

## 4. Human baseline and pilot

Use at least 20 paired representative tasks for the first time/quality comparison. A qualified
practitioner and AnalystOS receive the same brief, data and permitted tools. Independently review
outputs with authorship concealed where practical; count all review/correction time. Report
disagreements, confidence intervals and per-task outcomes. This is a pilot estimate, not proof of
universal superiority. Expansion requires enough observations to resolve the claimed improvement.

A named business owner accepts the decision and documents whether it was acted on. Follow-up
records compare the agreed outcome metric with its baseline and acknowledge attribution limits.
Track recurring usage and maintenance burden as well as first-result speed. No externally
scheduled notification or action occurs without the existing approval mechanism.

## 5. Release evidence

Each release attaches a machine-readable case report and a readable summary: corpus version,
supported envelope, commit, inputs, policy, provider/seed settings, raw outcome references,
rubric verdicts, latency/cost, interventions, failures and reviewer/date. Run a fixed regression
subset on each change and the sealed suite before capability promotion. Keep development
fixtures separate from claims about generalization.
