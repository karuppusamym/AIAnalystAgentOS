# Workspace-adaptive data team — target specification

Design revision: 2026-09-25. This extends [spec v2](02-spec-v2.md) for future increments;
it does not claim implementation. The [tracker](../60-delivery/01-tracker.md) is the status authority.
Current read-only and approval invariants continue to apply. Where this document defines a new
contract or evidence state, rollout requires migrations, compatibility and evaluation first.

## 1. Product outcome and scope

Enable one user to complete analyst, data engineering, and data science workflows from a
business objective to a reproducible, maintained output. “Better” means independently accepted
work with less elapsed time, human effort and total cost, measured within a declared capability
envelope. Do not claim replacement of every specialist on every dataset.

| Work | Existing foundation | Target deliverable |
|---|---|---|
| Analyst | Profile, hypotheses, statistics, SQL, KPIs, dashboards, reports, monitors | Governed business definitions, validated joins, cohorts/funnels and comparisons, evidence-linked decision brief |
| Data scientist | Statistical testing, logistic driver analysis, forecasting skill | Explicit estimand/target, valid experiments, reproducible comparisons, uncertainty and limitations |
| ML practitioner | Libraries and isolated analytical skills | Leakage-safe training, experiment registry, model card, approved batch scoring, delayed-label evaluation |
| Data engineer | Discovery, staged snapshots, virtual datasets, refresh | Versioned transformations, contracts, reconciliation, incremental processing, backfill and recovery |
| Business owner | Objectives, feedback and approval | Accepted definition of success, documented decision, accountable follow-up and measured outcome |

First ML scope: supervised tabular classification/regression and batch forecasts. Defer online
serving, deep learning, streaming infrastructure and unbounded custom code until a measured use
case requires them. Causal effects require an experiment or an approved identification design;
observational feature importance is not evidence that changing a feature will change an outcome.

## 2. The workspace is the unit of adaptation

Add a versioned `WorkspaceBrief`, tied to existing source assets, context and artifact versions:

| Field group | Required meaning |
|---|---|
| Decision | Business question, decision owner, intended action, audience, acceptance rubric |
| Domain | Reviewed vocabulary, entities, aliases and prohibited interpretations |
| Data semantics | Row grain, entity keys, join cardinalities, deduplication and exclusion rules |
| Time and measures | Event/availability time, timezone, fiscal calendar, units/currency, numerator/denominator and aggregation |
| ML objective, when applicable | Target, prediction moment, horizon, label availability, error costs, evaluation metric |
| Constraints | Allowed source/destination scope, freshness tolerance, residency/retention, compute and query budgets |
| Knowledge | Reviewed context IDs, metric versions, accepted corrections and their owners |

Each assertion records origin (`source`, `rule`, `model`, `user`), evidence references, review
state and version. An inferred grain or business term remains a suggestion until reviewed or
validated by an applicable deterministic check. Do not infer sensitive permissions from data.
Metric validation detects equivalent duplicate definitions and conflicting definitions with the
same business name; the owner resolves them before either becomes a shared canonical metric.

At each request:

1. Resolve authorized assets and current versions, then retrieve only workspace-authorized memory.
2. Check freshness, schema drift, grain, key uniqueness, join fanout, coverage and missingness.
3. Classify the job: describe, compare, diagnose, forecast, predict, experiment, prepare or monitor.
4. Match it to a versioned capability with explicit data prerequisites and limits.
5. Build a plan showing assumptions, expected artifacts, budgets, validation and stop conditions.
6. Ask only questions that change correctness or the intended decision; use visible defaults for
   reversible presentation choices. Work that does not depend on an answer may continue.

A `ReadinessAssessment` returns `ready`, `needs_input`, `blocked`, or `unsupported`, with per-check
reasons and remediation. No single averaged score can conceal a failed leakage, scope or grain check.
An unavailable label must not silently turn prediction into a misleading explanation task.

Examples: retail returns requires order-versus-line grain and a returns denominator; incident SLA
analysis requires the owner's breach definition and timezone; churn prediction requires a
prediction cutoff and labels mature enough to evaluate. These share execution infrastructure but
need different inputs, tests and outputs. Renaming columns alone must not change the result once
semantic mappings are fixed.

## 3. One work order, typed execution specifications

Introduce `WorkOrderSpec` as a routing envelope; retain the current `AnalysisSpec` within it.
The envelope includes objective, job kind, brief revision, dataset/semantic versions, authorized
scope hash, acceptance rubric, budget, output expectations and typed validation policy.

Specialized payloads are `AnalysisSpec`, `PipelineSpec`, `MLSpec`, or `ExperimentSpec`.
Register each capability with input types, prerequisites, executable compiler, verifier,
resource estimates, supported connectors and benchmark evidence. Models select or propose
registered operations; deterministic code validates and executes them. New methods need a
contract and evaluation, not merely a prompt or an agent configuration.

The same durable run/task engine executes all types. Planning has a draft → assessed → executable
transition; policy decides when plan approval is required. External effects retain separate
hash-bound approval. Never request approval twice for the same unchanged valid proposal.

## 4. Evidence that matches the claim

Replace one overloaded trust badge with dimensions on a versioned `EvidenceBundle`:

- **Data:** snapshot/version or source observation interval, query IDs, filters, sampling,
  missing/excluded rows, schema and semantic revisions.
- **Claim:** subject/group, metric, value, unit, numerator/denominator, time window, comparison,
  effect direction and fact IDs. Numerical clauses render from bound facts, including titles.
- **Method:** assumptions, fit diagnostics, sample sizes by group, practical effect threshold,
  uncertainty method, selection procedure and multiple-testing family.
- **Validation:** reproducible, exploratory, confirmed-on-holdout, predictive-evaluated,
  inconclusive, or invalid, with check-level pass/fail/not-applicable and reason.
- **Limits:** applicability population, stale inputs, unidentified confounding, unsupported
  questions and untested slices. A review score is not a calibrated probability of truth.

These are dimensions, not a universal ladder: a correct descriptive count does not need a
p-value; a predictive model needs unseen-data evaluation; a causal estimate needs its design
assumptions and diagnostics. There is no universal `n >= 100` proof of validity. Record method-
specific sample adequacy/power rules, and permit “insufficient evidence” as a successful outcome.

Adaptive discovery must record every tested hypothesis, including negative results. A second
method on the same selected data is a robustness check. For confirmatory labels, lock the claim
and method before access to an untouched confirmation partition, or use a reviewed sequential
testing procedure. Without that, label the result exploratory even when adjusted p-values pass.

Exact hash replay requires the same immutable data version and canonical result order/types.
When a source cannot supply that version, record the observation interval and best-effort replay;
changed source data is not automatically a failed statistical method. Historical artifacts keep
their evidence, but current UI views mark superseded/stale versions and require revalidation
before promotion. Missing query records or empty evidence never pass verification.

## 5. Data engineering workflow

`sources → contracts → join/transform plan → dry run → tests/reconciliation → versioned output`

`PipelineSpec` defines typed read-only transformations, input versions, output grain/schema,
keys, joins with expected cardinality, freshness, null/uniqueness/referential checks and budgets.
Compile SQL through the existing gateway; use an approved bounded snapshot for local transforms.
Report input/output counts, fanout, unmatched keys, rejected rows and aggregate reconciliation.
Never silently accept multiplication of a business measure after a join.

The first slice emits a governed virtual dataset and downloadable reproducibility recipe via
the existing export approval path. Managed materialization is a later, separately gated step:
allowlisted destination, separate writer identity, hash-bound approval, staging then atomic
promotion, retry-safe checkpoint, idempotent keys and rollback pointer. No writes to input sources.

Incremental specs must declare watermark, late-arrival window, deduplication keys, updates/deletes
policy and backfill range. Failed validation quarantines the candidate, retains the previous
good output, and records a failed operation. A retry or overlapping schedule must not double count.
Cross-source analysis requires per-source scope and snapshot manifests plus validated staging
joins; do not weaken the existing single-source gateway to enable it.

## 6. Data science and ML workflow

`objective → target/availability checks → split → baseline → bounded experiments → evaluation
→ model card → approval → batch scoring → performance monitoring`

`MLSpec` pins dataset version, entity/group keys, target, feature availability, prediction cutoff,
label horizon, split strategy, preprocessing, allowed estimators, search budget, objective metric,
slice guardrails, seed and runtime environment digest.

- Split before learning transformations. Fit preprocessing/selection only on training folds;
  use the fitted pipeline unchanged for validation and scoring. See the primary guidance on
  [preventing data leakage](https://scikit-learn.org/stable/common_pitfalls.html#data-leakage).
- Choose chronological validation for forecasting and group-aware validation for repeated entities;
  combine constraints where required, embargo overlapping label windows, and record split membership.
  Random splits need justified independence. See [cross-validation strategies](https://scikit-learn.org/stable/modules/cross_validation.html).
- Tune on training/validation only; freeze the selection before final holdout evaluation. Retuning
  after reading that holdout requires a new evaluation partition and a new version.
- Compare with a simple baseline on the same split. Classification records threshold and error
  costs, precision/recall and calibration when probabilities matter; regression records error in
  business units; forecasts record rolling-origin error by horizon and interval coverage.
  Metric choice follows the objective; see [scoring definitions](https://scikit-learn.org/stable/modules/model_evaluation.html).
- Report uncertainty and weak slices. Feature importance explains model behavior, not causality.
  No improvement is a valid result; do not promote a model that misses its predeclared guardrails.

Use artifact versions for model packages, experiment records and model cards before adopting a
separate registry service. Packages contain preprocessing, feature schema/order, environment,
seed, code hash and evidence references. Load only trusted platform-produced, integrity-checked
packages in isolated workers; do not execute arbitrary uploaded serialized models.

Initial deployment is approved batch scoring to a managed output. Pin model/input versions,
enforce training/scoring feature parity, record rejected rows and maintain a rollback version.
Track freshness, missingness, feature drift and cost separately from measured predictive error.
Drift alone does not prove performance loss. Evaluate error when labels mature; retraining
creates a challenger, never silently promotes it. Online serving remains deferred.

## 7. Learning and operation

Corrections become reviewed workspace memory with provenance, owner, expiry and semantic version.
Schema or definition changes invalidate dependent plans/evidence; withdrawn permission removes
retrieval access immediately. Episodes are evidence of past work, not privileged instructions.
Do not automatically turn a positive user reaction into a training label or change shared models.

Every output can carry a decision owner, proposed next action, success metric, evaluation date
and outcome record. Showing an association does not establish business impact. Distinguish
accepted analysis, action taken and independently measured outcome in reporting.

## 8. Acceptance and scope control

Deliver vertical slices through UI, API, execution, evidence, recovery and monitoring together.
Every capability lists supported task/data types, limits and last evaluation version. Budgeting
includes warehouse work, CPU/memory, storage and wall time as well as model tokens. Hard limits
are enforced by schedulers/workers; estimates and unavailable source-cost measurements are labeled.

The [evaluation plan](../60-delivery/05-evaluation-plan.md) gates claims of improvement and
the [workbench design](../10-architecture/02-workbench-ux.md) defines the visible experience.
