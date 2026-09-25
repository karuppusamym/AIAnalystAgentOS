# Context2AI AnalystOS — Specification v2 (engineering rewrite)

**Status:** authoritative engineering specification for this repository, 2026-09-25.
**Supersedes for implementation purposes:** [`Context2AI_AnalystOS_Complete_Spec.md`](../../Context2AI_AnalystOS_Complete_Spec.md) (v1, kept unchanged as the product vision).
**Relationship to v1:** v1 describes *what* the product must become. v2 decides *how* to build it
safely, and draws a hard line around what Phase 1 must prove. Where v2 narrows or reorders v1, the
reason is stated. Nothing in v1's vision is dropped; items outside Phase 1 remain in the
[tracker](../60-delivery/01-tracker.md) with their v1 IDs.
**Amended by:** [spec v3 — platform](03-spec-v3-platform.md) (proposed 2026-09-25). v3 changes how
agents, plans, methods, model calls, knowledge, engines and the UI are built: §5, §6, §7, §10, §11,
§13 and §15 here. It builds on the increment-3 additions in §10.1 and §11. Every principle,
invariant and verification rule in this document still holds.

---

## 1. The problem, restated from first principles

An analyst's job is not "write SQL". It is a loop:

1. **Understand** what the business is asking, in its own vocabulary.
2. **Look** at the data before believing anything about it (shape, holes, joins).
3. **Guess** — form falsifiable hypotheses about what drives the outcome.
4. **Test** each guess with a method that fits the data type and could have said *no*.
5. **Doubt** the results: would this survive a re-run, a second method, a sceptical colleague,
   and the fact that we tested ten things at once?
6. **Package** what survived into reusable assets: a dataset, KPI definitions, charts, dashboards.
7. **Ask permission** before anything leaves the building.
8. **Remember** what was learned so the next investigation starts smarter.

A language model is very good at steps 1, 3 and the prose around 6. It is unreliable at 4 and 5
(it will produce a plausible number that nobody computed) and it must never be the thing that
decides 7. So the architecture is organised around one idea:

> **Models propose. Deterministic code computes, checks and enforces. People approve side effects.**

Every design decision below is an application of that sentence.

## 2. Principles (v1 §6, sharpened into testable rules)

| # | Rule | How it is enforced in code |
|---|---|---|
| P1 | No number in a finding that a deterministic tool did not compute | Insight narratives pass a *numbers guard*: every numeral must match a computed fact, else the template text is used (`agents/insight.py`) |
| P2 | Hypotheses are executable, not prose | The LLM must emit an `AnalysisSpec` in a closed vocabulary (6 methods × 9 derivations); invalid specs are dropped with a recorded reason (`agents/investigator.py`) |
| P3 | Authorization lives outside the model | Scope is resolved server-side per step; the SQL gateway re-validates every statement against it (`governance/policy.py`, `gateway/validator.py`) |
| P4 | Context is data, never instructions | Catalog text and retrieved documents are wrapped as `<untrusted_context>`; they cannot grant tools or widen scope |
| P5 | Verified means reproducible, not "the models agreed" | REV verification = re-run hash match + independent second method + adjusted significance + effect size; model opinions only move confidence |
| P6 | Side effects are proposals bound to a hash | Approval binds payload hash + plan hash + policy version + expiry; execution re-verifies all of them and current authorization |
| P7 | Minimum data movement | Pushdown for SQL sources; bounded snapshots only for non-SQL sources; datasets are virtual (a governed `SELECT`) |
| P8 | Fail closed and visibly | Missing model route, provider, destination or permission → explicit error and a deterministic fallback that is *labelled* as such |
| P9 | Everything is persisted with lineage | Queries, experiments, artifacts, approvals, model/tool calls, and a lineage edge list mirrored to Neo4j |

## 3. Scope

### 3.1 Phase 1 (this release) must prove, end to end

The v1 §62 "definition of done" scenario, on a governed ServiceNow-shaped source:
workspace → source → select Incident + Change Request → context → profile → data quality →
hypotheses → iterative testing → ≥3 verified findings → reusable dataset → KPIs → ≥5 charts →
executive + operational dashboards → approval → Superset → lineage; with pause/redirect before
publication and evidence inspectable for every KPI and finding.

### 3.2 Deliberate Phase 1 limits (v1 §59 "Phase 1" + safety)

* **One source per run** (cross-source federation is Phase 2).
* **Read-only against sources.** `source_mutation` is a hard deny, not a policy option.
* **Publication, scheduling, export and external notification always need an approval**, at every
  autonomy level including 4. Level 4 is recorded but cannot remove this gate in this release.
* **Superset only** as a live BI destination (`preview` destination when unreachable). Power BI is Phase 2.
* **Local identity (JWT)** with an OIDC-shaped token; SSO is Phase 4. Workspace roles are enforced now.
* **Scheduling, monitoring and reports (Phase 3)** were added in increment 2 (ADR-0009): scheduled
  dataset refresh and re-analysis with a claim-based "what changed" diff, narrative/PDF/Excel
  reports, metric threshold/drift/change-point and data-quality monitors, alerts with JEV triage,
  policy-gated automatic investigations, in-app notifications. External delivery (email/chat/
  webhook) remains out of scope until it can go through the approval model.

### 3.3 What v2 changes relative to v1, and why

| v1 | v2 | Why |
|---|---|---|
| ~15 deployable services (§57) | **Modular monolith, three process roles** (api, worker, web) + infrastructure | Service boundaries are package boundaries with contracts; splitting processes before load demands it multiplies failure modes without adding safety. ADR-0001 |
| Free-form SQL agent on the critical path | **Closed analysis vocabulary** compiled to SQL; free-form SQL only for ad-hoc "Ask", still gateway-validated | Reproducibility and injection resistance. ADR-0002 |
| "REV agent verifies" | **Verified = deterministic checks pass**; independent model family and JEV only adjust confidence | v1 §28 itself says agreement ≠ truth; v2 makes it mechanical |
| Model router by task | Router by **purpose → profile → allowlisted models**, plus **JEV decision purposes** | Typed decisions with probabilities are auditable; chat completions are not. ADR-0006 |
| Semantic model / Superset dataset built from tables | **Virtual dataset** (governed SELECT + derived columns) re-validated against current scope at publish time | No writes to sources; restricted columns cannot leak into BI |
| Temporal "or" LangGraph | **Temporal** for durability; engine is orchestrator-agnostic (local runner for tests) | Same code path in tests and production. ADR-0005 |

## 4. System model

### 4.1 Planes and trust boundaries

```
┌──────────── Control plane (Postgres db `analystos`) ────────────┐
│ users, workspaces, members, versioned policies, sources/assets, │
│ runs, tasks, hypotheses, experiments, insights, artifacts,      │
│ lineage, approvals, publications, model/tool/query audit        │
└──────── never reachable from user or model SQL ─────────────────┘
┌──────────── Analytics plane (Postgres db `analytics`) ──────────┐
│ src_<source> schemas: bounded snapshots of non-SQL sources      │
│ loader identity writes; reader identity (read-only) queries     │
└─────────────────────────────────────────────────────────────────┘
   Pushdown sources (PostgreSQL, SQL Server): queried in place with
   their own least-privilege read-only identity, secret resolved JIT.
```

Identities: `analystos` (control plane), `analystos_loader` (staging writes), `analystos_reader`
(the only identity the gateway and Superset use for data, `default_transaction_read_only=on`,
no CONNECT on the control-plane database).

### 4.2 Components (package = boundary)

| Package | Responsibility | v1 module (§8) |
|---|---|---|
| `api` | HTTP surface, auth, SSE | API gateway |
| `services` | workspace, source, run/feedback use-cases | Workspace Service |
| `governance` | scope resolution, policy decisions, approvals, audit | Policy / Governance Engine |
| `gateway` | SQL validation, read-only execution, cache, query audit | Data Access Gateway, Query Engine |
| `connectors`, `staging` | discovery, extraction, snapshot load | Metadata Service, Integration Engine (minimal) |
| `skills` | profiling, quality, relationships, statistics, viz rules, layouts | Analysis Skills |
| `sandbox` | resource-limited Python for custom numeric code | Python Sandbox |
| `llm` | model router, JEV decisions, redaction | Model Router |
| `agents`, `runtime`, `workflows` | agent behaviours, plan, engine, Temporal workflow | Agent Runtime, Orchestration |
| `tools` | tool registry and gated invocation | Tool Registry |
| `artifacts`, `graph` | artifact versions, lineage, Neo4j projection | Artifact Registry, Lineage |
| `context` | layered retrieval, Context2AI adapter, local store | Context Service Adapter, Memory |
| `publishing` | BI publisher interface, Superset adapter, preview | BI Publishing Gateway |

## 5. Run lifecycle

A run is a **versioned plan** (DAG of tasks with stable keys) whose state lives in Postgres.
The orchestrator (Temporal in deployments, a local loop in tests) repeatedly asks the engine
*"what is ready?"* and executes those tasks as idempotent activities.

```
context ─┐
metadata ─┼─ relationships ─┐
          └─ profile ───────┴─ quality ─┐
                                        ├─ hypotheses ─▶ test:H-1..n ─▶ followups:1 ─▶ test:H-n+1.. ─▶ followups:2 …
                                        │                                 (bounded by policy.max_iterations, JEV stop check)
                                        └────────────▶ insights ─▶ verify ─▶ dataset ─▶ semantic ─▶ visualize
                                                                              ─▶ publish_request ─▶ [approval] ─▶ publish ─▶ finalize
```

* `insights` depends on `test:*` and `followups:*` (wildcards include tasks added later).
* Autonomy ≤ 2 inserts `plan_approval` before any data is touched.
* **Redirect** ("focus only on application incidents") → the instruction is persisted, interpreted
  into validated filters, the plan version increments, hypotheses/insights of the old version are
  `superseded`, context/metadata/profile/quality are **reused**, approvals are invalidated, the
  workflow is nudged. A task that finishes under an old plan version has its result discarded.
* **Reject finding** → downstream-only replan (dataset onward).
* **Pause** takes effect between tasks and between queries; **cancel** is checked before every
  query and before the publish side effect; the run records whether it stopped before or after
  a side effect.

Run states: `NEW → PLANNING → READY → RUNNING ⇄ WAITING_USER / PAUSED → COMPLETED | FAILED | CANCELLED`.

## 6. Agents, skills, tools (v1 §12–§15)

Fourteen persistent roles are registered from YAML (`config/agents/*.yaml`); two Phase-2 roles are
registered disabled. An agent is *configuration + a behaviour*; capabilities live in **skills**
(deterministic functions) and **tools** (gated, audited invocations). An agent can only invoke tools
bound in its definition, and every invocation is policy-checked and recorded.

| Agent | Phase-1 behaviour |
|---|---|
| Supervisor | frames the objective into questions (LLM), owns plan, finalizes (summary, episode memory, graph projection) |
| Context | layered retrieval: exact metadata → graph → vector → prior episodes → user notes; term resolution; ambiguity list |
| Metadata | row counts, keys, relationship discovery + validation (containment, cardinality) |
| Profiler | pushdown profiling (nulls, distinct, quantiles, histograms, categories, coverage, duplicates, outliers) |
| Data Quality | missing, duplicates, orphans, temporal order, future timestamps, constant/case-variant categories |
| Investigator | LLM hypotheses in the closed vocabulary; validation; profile-driven fallback; JEV priority; follow-ups |
| Data Scientist | runs the method (chi-square, Mann-Whitney/Kruskal, Spearman, logistic regression + importance, trend + change point, Pareto) |
| Insight Analyst | Benjamini–Hochberg across the run, findings with impact and caveats, numbers-guarded narrative |
| REV Critic | reason/evaluate/verify, re-run hash match, second method, independent model family, JEV second opinion |
| SQL Engineer | virtual analytical dataset with derived columns; ad-hoc NL→SQL with gateway-feedback repair loop |
| Semantic Model | KPIs from dataset columns (+LLM proposals), each validated by execution, duplicates detected |
| Visualization | chart rules (§34) + JEV override, previews through the gateway, executive/operational layouts |
| Governance | re-validates published datasets against current scope, destination policy |
| BI Publisher | immutable bundle → approval → re-verify → idempotent Superset publish (reconcile-before-create) |

## 7. The closed analysis vocabulary

`AnalysisSpec = {method, asset, outcome, segment, drivers, time, filters}` with
methods `rate_by_segment | numeric_by_segment | trend | pareto | correlation | driver_model` and
derivations `column | duration_hours | after_hours | bucket | equals | is_true | date_trunc |
hour_of_day | day_of_week`. The compiler (`skills/sqlbuild.py`) emits dialect-correct SQL with
aggregation pushed to the source; row-level data leaves the source only as bounded, deterministic
samples for tests that need it. Method/type compatibility is validated before anything runs.

## 8. Verification (v1 §26, §28)

For each finding the critic records:

* **Reason** — claim, question, required evidence.
* **Evaluate** — method fit (assumption warnings), sample size (n ≥ 100), significance after
  Benjamini–Hochberg (q < α), effect size above threshold, overreach (causal wording is rewritten).
* **Verify** — re-execute each evidence query with the cache bypassed and compare result hashes;
  run an independent second method; ask an independent model family (excluding the narrative
  model's family) and JEV for P(evidence supports claim); detect contradictions.

`verified = all deterministic checks pass`. Confidence is a documented function of q-value,
second-method agreement, reproducibility, model opinions and data-quality caveats.

## 9. Governance and security invariants (v1 §12.2, §45, §46)

1. Scope = selected assets of ready sources in the workspace, minus denied columns (restricted
   tags/policy + PII unless `pii_access=allowed` or the user has `pii_clearance`), re-resolved at
   **every task**; a run can never widen beyond its start scope.
2. Every statement from any path (agent, repair, "Ask", console, verification re-run, dataset,
   chart preview, metric validation) passes the same validator: single read-only statement, every
   table resolved to an in-scope asset, one source, columns qualified via the scope catalog, no
   denied column anywhere (including `*` expansion, aliases, CTEs, subqueries), function denylist,
   row cap, statement timeout, read-only transaction, audit row.
3. Policy decision = `allow | deny | approval_required` + machine-readable reasons + risk tier +
   effective autonomy, persisted as an audit event; re-evaluated at execution time.
4. Approvals are immutable proposals bound to payload hash, plan hash, policy version and expiry;
   decide() enforces approver role and separation of duties; verify_for_execution() re-checks
   hashes, expiry, policy version, requester still `editor+`, approver still `approver|owner`.
5. Secrets are references (`env:` / `file:`), resolved just in time, never stored in config,
   prompts, artifacts or logs; outbound prompts are redacted.
6. Model routes are policy inputs: workspace allowlist ∩ platform allowlist; empty → fail.

## 10. Model routing and JEV decisioning (v1 §27)

Purposes map to profiles (`config/models.yaml`). Chat profiles: `reasoning_strong`, `coding`,
`analytical_reasoning`, `low_cost`, `independent_model_family` (excludes the primary family).
The `decision` profile routes to **TypeSafe Jev** (`typesafe/jev-1.13`, pinned) via the OpenRouter
Decisions API, which returns typed answers with probabilities:

| JEV purpose | Question type | Used for | Authority |
|---|---|---|---|
| `hypothesis_priority` | `score` low/medium/high | rank hypotheses for the objective | ranking only |
| `risk_check` | `noul` | is a feedback/publication request consequential? | **escalate only** |
| `feedback_classification` | `choice` | redirect / add context / reject finding / deeper / question | routing of feedback |
| `chart_selection` | `choice` among rule-valid types | override chart default when p ≥ 0.6 | presentation only |
| `rev_second_opinion` | `noul` | P(evidence supports claim as worded) | confidence only |
| `stop_check` | `noul` | P(objective answered) ≥ 0.85 stops follow-up rounds | early stop only |
| `alert_triage` | `noul` | P(monitored change is material) ≥ 0.8 raises alert severity | **escalate only** |

Only trusted text enters JEV `state` (objective, statements, computed statistics, registry
descriptions) — never raw result rows. Every call is logged with latency and cost; when JEV is
unavailable each decision falls back to its deterministic rule and records `by: rules`.

### 10.1 Token economy and admin control (increment 3, ADR-0010)

Every purpose has an administrator-set mode:
- `off`: deterministic path only; the router refuses the call.
- `auto`: rules first; the model runs only if the rules are insufficient.
- `always`: the default.

Around every call:
- responses are cached by purpose, candidate models and payload;
- estimated-oversize prompts are refused;
- runs below 25% of their budget downgrade to `low_cost`;
- catalog prompts are ranked and capped.

Every avoided call is recorded in `model_call` as `skipped`, `cache_hit` or `refused`, with its
`tokens_saved`. Platform settings are versioned, admin-only, audited, validated against
`config/models.yaml` and can be rolled back. Presets: `balanced`, `token_saver`, `max_quality`,
`offline`.

## 11. Data plane (v1 §16–§18, §48)

* **Source-kind catalog** (`config/source_kinds.yaml`). Kinds: PostgreSQL, SQL Server, MySQL,
  MariaDB, Oracle, Snowflake, BigQuery, Databricks, Trino, Redshift, ClickHouse, DuckDB, SQLite,
  ServiceNow and CSV/Parquet/Excel. Every SQL kind uses one generic connector (SQLAlchemy
  inspector, read-only session, Arrow batches).
* **Pushdown** only where the gateway validates the dialect and the session can be made read-only
  (PostgreSQL, SQL Server). Everything else is a **staged** snapshot (atomic swap) in
  `analytics.src_<id>`.
* **Metadata crawler** (`services/crawler.py`): full or incremental; include/exclude patterns;
  structural fingerprints and a drift diff (new / changed / missing / deprecated / rename
  candidates); deterministic semantics (business names, table role/domain/grain, column
  role/unit); PII from names plus gateway-sampled values; declared relationships; glossary links;
  context-store and graph refresh; optional batched model descriptions. Crawls can be scheduled
  (`crawl` schedule kind). Owner curation always wins; tags only tighten.
* **Query cache** key = fingerprint + scope hash + source version + row cap (Redis).

## 12. Publishing (v1 §32–§35)

`PublishBundle` (datasets, metrics, charts, dashboards) is the unit of approval. The Superset
adapter reconciles by deterministic names before creating (retries converge; no duplicates),
stores external ids per artifact, supports update-in-place, rollback, and dashboard inspection
(foundation for v1 §35 existing-dashboard mode).

## 13. Persistence, lineage, memory (v1 §29–§31, §44)

Postgres is the system of record (29 tables, Alembic-managed). Neo4j is a rebuildable projection
of `lineage_edge` + `relationship`. Semantic memory = `context_entry` with pgvector embeddings
(deterministic local hashing embeddings; no data leaves for embedding). Episodic memory = run
summaries written as `episode` entries and retrieved by later runs.

## 14. Observability and cost (v1 §50–§51)

Per call: `model_call` (purpose, profile, provider, model, prompt version, tokens, cost, latency,
attempt, error), `tool_execution` (decision, latency), `query_execution` (SQL, fingerprint,
status, rows, cache hit, duration, rejection). Budgets: per-run tokens and USD, per-workspace
monthly USD, per-run query count; exceeded budgets fail the model call and agents degrade to
deterministic paths with a visible message.

## 15. Non-functional targets (v1 §55) — targets, not claims

Workspace creation < 3 s; context search < 2 s; status updates streamed (SSE from persisted
events); publish orchestration < 30 s excluding BI processing. Measured values belong in the
[capability register](../60-delivery/02-capability-register.md), never in this document.

## 16. Testing and evaluation (v1 §56)

* Unit: validator security suite, statistics vs SciPy/statsmodels, compiler for all
  method×derivation×dialect, router/JEV fakes, plan/guard logic.
* Integration (compose stack): governance/approvals, staging + gateway as reader, Superset
  publish/idempotency/rollback, full run on the local orchestrator.
* Analytical benchmarks: planted effects must be found; null-effect controls must not.
* Live e2e (`scripts/e2e_demo.py`): the v1 §62 scenario through the HTTP API with real
  OpenRouter + JEV + Temporal + Superset, writing a dated evidence report.

## 17. Readiness

This release targets **Prototype** readiness (v1 §70): synthetic, non-sensitive data; isolated
environment; read-only; visible limits. Controlled-pilot and production gates are listed in
[`03-release-readiness.md`](../60-delivery/03-release-readiness.md) with what is missing.

## 18. Open questions

1. Real Context2AI endpoint contract (v1 §11.1 lists paths but not payloads) — the adapter
   assumes `{results: [...]}`; confirm with the Context2AI owners. *Proposed answer (spec v3
   §6.6):* consume Context2AI (Atlas) through OKF v0.2 bundle import and its MCP knowledge tools
   instead of a bespoke REST contract.
2. ServiceNow instance for connector certification (mock-only today; v1 §62 forbids certifying on mocks).
3. Model allowlist and residency requirements per tenant.
4. Whether Level-4 autonomy should ever permit unattended publication (requires the adversarial
   false-approval evaluation in v1 §56.4 first).
