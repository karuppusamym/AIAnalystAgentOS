# Context2AI AnalystOS — Specification v4 (one platform for analyst, data-science, data-engineering and ML work)

**Status:** proposed, 2026-09-26. Rows move to Done only through the
[tracker](../60-delivery/01-tracker.md): increment 7 (P7-*), plus P5-04..06 and P6-04..08.

**Relationship to earlier specs.** Each layer keeps authority over its own subject; v4 changes
them only where a section says so.

| Document | Stays authoritative for |
|---|---|
| [v2](02-spec-v2.md) | Principles P1–P9, governance invariants, run lifecycle, verification semantics |
| [v3](03-spec-v3-platform.md) | Capability platform, deterministic-first ladder, open formats, engines, self-hosted scale, UI journeys |
| [Workspace data-team spec](03-workspace-data-team-spec.md) | `WorkspaceBrief`, readiness, `WorkOrderSpec`, evidence dimensions, DE and ML *rules* (leakage, splits, holdouts, reconciliation) |
| **v4 (this)** | How one codebase carries all four disciplines; the trust mechanisms that make every output reproducible and self-invalidating; the donor port program |

**Why v4 exists.** The owner supplied an external comparison review on 2026-09-26 and made three
decisions about it. The review is recorded, with a disposition per recommendation, in
[`70-reviews/2026-09-26-agent-os-comparison-review.md`](../70-reviews/2026-09-26-agent-os-comparison-review.md).
The decisions:

1. One platform, with Atlas and DataPilot as donor repositories.
2. Governed classical ML.
3. Documentation first.

**Decisions:** [ADR-0018](../10-architecture/adr/0018-one-platform-donor-repositories.md) to
[ADR-0024](../10-architecture/adr/0024-governed-classical-ml.md).

---

## 1. Positioning

> **AnalystOS is the governed data-work OS that runs on your stack.** Analysts, data scientists,
> data engineers and ML practitioners work from one workspace, on one knowledge base, through one
> gateway. Models propose; deterministic code compiles, tests, verifies and computes. Every number
> can say where it came from, and it stops claiming to be verified the moment anything it depends
> on changes.

**What changes from v3 §1.** v3 absorbed the four disciplines "in order" and kept data engineering
to "the minimum needed". v4 keeps that minimum, and still runs no ETL engine of its own
(ADR-0014). It widens the other disciplines in three ways:

- **Engineering:** ingestion of files and sources, typed transformation recipes with blocking
  quality gates, and executed lineage.
- **Data science and ML:** a real (bounded) classical ML lifecycle.
- **All four:** one step-based working model, so the same trust guarantees hold whatever the
  discipline.

**Non-goals (additions to v3 §1):**

- Online model serving.
- A feature store.
- Deep learning or GPU pools.
- Streaming pipelines.
- A general notebook/IDE product.
- A separate orchestrator per discipline.
- A cross-repository SDK.

## 2. Principles added to v2 §2 and v3 §2

| # | Rule | Enforced by |
|---|---|---|
| P17 | **One platform, one gateway, one evidence model.** Every discipline's work is a capability in this codebase, reads data only through `QueryGateway` or a gateway-produced snapshot, and produces evidence in the same bundle/verdict model. | ADR-0018, CLAUDE.md rules 3–5 |
| P18 | **Governed questions compile; they are not prompted.** A question answerable from approved semantics becomes a `SemanticQuery` compiled deterministically. Anything else is visibly `ad_hoc`. | ADR-0019 |
| P19 | **A verdict is only as current as its dependencies.** Every verdict stores a dependency fingerprint. Any change to query, data, semantics, method, context, model call or policy voids it. | ADR-0020 |
| P20 | **Triggers run published, pinned definitions.** Schedules, API calls, monitors and MCP run a published version with pinned semantics. They never run a draft, and they never run a silent upgrade. | ADR-0021 |
| P21 | **Heavy compute is isolated and credential-free.** Training, backtests and snapshot transforms run in isolated pools with scoped artifact tokens, and no source or provider secrets. | ADR-0022 |
| P22 | **Evaluation gates releases.** A change to a model route, prompt, method, compiler or capability merges only if its evaluation suites don't regress past the owner-set thresholds. | §12, P7-07 |

## 3. Four disciplines as capability packs, not personas

The review's advice to keep durable roles few is already the architecture. Agents are
declarative manifests (`config/agents/*.yaml`, P4-X03). Behaviour lives in methods and skills, and
playbooks sequence them. v4 adds capabilities, a small number of agents, and two isolated pools.

| Discipline | Job kinds (workspace spec §2) | Typed spec | Existing capabilities | Added by v4 | Pool |
|---|---|---|---|---|---|
| Analyst | describe, compare, diagnose, monitor | `AnalysisSpec`, `SemanticQuery` | `investigate.v1`, Ask, 8 methods, monitors, reports, Superset publishing | Semantic compiler (P7-02), step model (P7-04), branching (P7-05), "why this number" (P7-08) | inline / `compute` |
| Data scientist | compare, diagnose, experiment | `AnalysisSpec`, `ExperimentSpec` | Hypothesis registry, BH correction, second-method REV, driver model, forecast skill, confirmation rules | Experiment design checks and power (P5-01), notebooks over the gateway (P7-12) | `compute-py` |
| Data engineer | prepare, monitor | `PipelineSpec` → recipe IR | Staged loader, virtual datasets, `elt_build.v1` + BuildGateway (dbt on customer engine), crawler, drift | File ingestion (P6-06), recipe IR and compilers (P6-04), DQ gates and quarantine (P6-05), executed lineage (P6-07), incremental (P6-02) | inline / `compute-py` |
| ML practitioner | forecast, predict | `MLSpec` | `skills/forecast.py`, logistic driver model, scikit-learn/statsmodels deps | `ml.forecast / classify / regress / cluster / anomaly` methods, experiment records, model cards, batch scoring, label-aware monitors (P5-01..06) | `compute-ml` |
| Steward (all) | review | semantic model, knowledge pack | Approvals, semantic metric versions, OKF/Ossie/ODCS, crawler curation | Relationship cardinality review (P7-09), semantic diff (P7-02), injection screening at ingest (P7-10) | inline |

**Agents added:**

- `engineer`: proposes recipe nodes and `PipelineSpec`s.
- `ml_practitioner`: proposes `MLSpec` targets, features and estimator choices.

Both are declarative (`entry: builtin:generic`). They can only *propose typed specs*; validators
and compilers decide (CLAUDE.md rule 3). The existing `transformation`, `data_quality` and
`data_scientist` agents are extended rather than duplicated where their manifests already cover the job.

## 4. Contracts

| Contract | Module | Decided in | Row |
|---|---|---|---|
| `WorkOrderSpec` envelope + `AnalysisSpec \| PipelineSpec \| MLSpec \| ExperimentSpec` | `contracts/work.py` | Workspace spec §3 | P4-06 |
| `SemanticQuery` IR | `contracts/semantic.py` | ADR-0019 | P7-02 |
| `SemanticRelationship.cardinality` (+ validation provenance) | `contracts/semantic.py` (Ossie extension) | ADR-0019 | P7-09 |
| `VerificationRecord` + dependency rows | `contracts/evidence.py` | ADR-0020 | P7-01 |
| `Definition` versions (draft/published/retired) | `contracts/definition.py` | ADR-0021 | P7-03 |
| `TaskEnvelope`, `ArtifactRef`, scoped token claims | `contracts/worker.py` | ADR-0022 | P7-06 |
| `Recipe` IR (nodes, checks, incremental) | `contracts/recipe.py` | ADR-0023 | P6-04 |
| `MLSpec`, split manifest, experiment record, model card | `contracts/ml.py` | ADR-0024, workspace spec §6 | P5-01..02 |
| `Step` (analysis step object) and `Branch` | `contracts/step.py` | §7 | P7-04..05 |

All of these are exported to `contracts/*.json` by `analystos export-contracts`, like the existing
ones. Canonical JSON and hashing follow `contracts/approval_hash.md` (ADR-0017).

## 5. Semantic compilation (ADR-0019)

```
question ─▶ rules rung: glossary/metric/synonym match ─▶ SemanticQuery? ──yes──▶ validate vs approved model vN
                     │ no match                                                   │
                     ▼                                                            ▼
          sql_generation purpose (model SQL)                 compiler (policy filters, masks, fan-out refusal)
                     │                                                            │
                     └──────────────▶ QueryGateway.execute ◀──────────────────────┘
                                      │
                         answer labelled ad_hoc | governed (model vN, compiler vM)
```

- **Fan-out refusal.** An additive measure never crosses a `one_to_many` or `many_to_many` edge
  without a declared pre-aggregation.
- **Cardinality comes from data.** It is established by data checks (ported from Atlas), never by a model.
- **Promotion.** An `ad_hoc` answer can be promoted to a metric proposal; approval stays human
  (separation of duties, P4-K03).

## 6. Verification that voids itself (ADR-0020)

A `VerificationRecord` binds a verdict to typed dependencies (`query`, `data`, `semantic`,
`method`, `context`, `model_call`, `policy`) and their version hashes.

- **Events void it.** The events that change a dependency void every dependent record in the same
  transaction.
- **A nightly sweep catches misses.** It voids anything the events missed, and counts late voids
  as a defect.
- **Consumers check the record's state.** The publish gate, reports, badges, baselines and exports
  read the record's `ACTIVE | VOID | SUPERSEDED` state, not a stored boolean.
- **P4-03 folds in.** Its `stale` state becomes the `data` case of `VOID`.

## 7. The step model and the Data Thread

Every run, Ask thread and notebook is a sequence of **steps**:

`Step {id, version, kind: plan|query|method|recipe|train|chart|claim, spec, receipts[], result_snapshot: ArtifactRef, chart_spec?, checks[], verification_record?}`

- **Self-check before REV.** A step runs deterministic checks for empty results, magnitude against
  history, truncation (the P4-C12 population record), grouping mistakes, fan-out and DQ gate failures.
  A failed check that is safe to correct is corrected and re-run. Otherwise it is flagged.
  (`skills/selfcheck.py`, P7-04)
- **Edit and re-run.** Editing a step creates `version+1`, re-runs it and the steps that depend on
  it, and voids their verdicts (P19). The old version stays readable.
- **Branch.** A branch forks from any step and keeps its parent pointer. Two branches can be
  compared side by side, and either can be merged into a report. The Data Thread (P7-05) is this
  structure rendered as a thread.
- **Pin.** A step can be pinned to a dashboard tile, a schedule (as a frozen `SemanticQuery` or
  `AnalysisSpec`, P20) or a later workflow's input.
- **"Why this number?"** (P7-08) resolves any displayed number through five links:
  1. the fact binding;
  2. the step;
  3. the query receipt (SQL or `SemanticQuery`, result hash);
  4. the data version;
  5. the semantic metric version and the verdict.

  Each link comes with its current state. This is the review's "Answer Passport", built from
  records that already exist (P4-03 facts and receipts, ADR-0015 decisions) plus ADR-0020.

## 8. Draft, published, pinned (ADR-0021)

Playbooks, recipes, ML specs and saved analyses have `draft → published → retired` versions.

- **Triggers run published versions only.** API calls, schedules, monitors and MCP refuse drafts
  outside `dev` workspaces.
- **Schedules pin a frozen set.** A schedule stores the published definition version plus the
  metric and method versions of its baseline run, and replays that frozen set on new data.
- **Upgrades are explicit.** A newer version shows *upgrade available* with a diff; the owner
  accepts it into a new schedule revision.
- **Promotion is by content hash.** Moving dev → test → prod copies the definition by content hash
  and re-binds connections per environment.

## 9. Data engineering

```
files / sources ──▶ stage (loader; watermark incremental, ADR-0016) ──▶ recipe IR (ADR-0023)
   ──▶ compile: in-source SQL (pushdown) | snapshot + compute-py (DuckDB/Polars) | dbt on customer engine
   ──▶ join cardinality pre-flight ──▶ DQ gates (blocking → quarantine, keep last good; warning → evidence)
   ──▶ output: virtual dataset | reproducible recipe export | approved managed materialization (writer identity)
   ──▶ executed column lineage (OpenLineage) ──▶ published definition ──▶ schedule / monitor
```

| Piece | Exists | v4 work | Donor |
|---|---|---|---|
| Source staging, per-workspace grants, snapshot fingerprints | Yes (increment 3, P4-03) | Watermark incremental + delete reconcile (P6-02) | — |
| File upload (CSV/JSON/Excel/Parquet), schema mapping, load modes | Partial (file inspection via DuckDB) | Mapped ingestion with replace/append/merge (P6-06) | DataPilot `staging.py`, `file_profiles.py` |
| Typed transformations | Virtual datasets; dbt build (`build/`) | Recipe IR + SQL/DuckDB/Polars/dbt compilers (P6-04) | DataPilot `pipeline_codegen/` |
| DQ | Crawler drift; data_quality agent | Blocking/warning gates, quarantine, seasonal baselines (P6-05) | DataPilot `quality.py`; Atlas `data_quality.py` |
| Lineage | OpenLineage export, run lineage | Executed column lineage from IR; SQL/dbt/procedure lineage parsing (P6-07) | Atlas `sql_lineage_parser.py`, `dbt_column_lineage.py` |
| Managed output | Design only (ADR-0011 workspace) | Writer identity, atomic promotion/rollback (P6-03) | — |

## 10. Data science and governed classical ML (ADR-0024)

`objective → brief (target, cutoff, horizon, label maturity) → readiness (leakage, grain, label availability) → split manifest → baseline → bounded search (compute-ml) → holdout evaluation → model card → approval → batch scoring (published, pinned) → drift + delayed-label monitors → challenger`

| Capability | Method | Evidence it must produce |
|---|---|---|
| `ml.forecast` | statsmodels ETS/ARIMA vs seasonal-naive baseline | Rolling-origin error by horizon, interval coverage |
| `ml.classify` | Allowlisted sklearn pipelines vs dummy baseline | Threshold with error costs, precision/recall, calibration, slice guardrails |
| `ml.regress` | Same family | Error in business units, residual diagnostics, slices |
| `ml.cluster` | k-means / GMM | Stability across seeds and bootstraps; no causal wording |
| `ml.anomaly` | Seasonal residual, isolation forest | Threshold calibrated on history; false-alarm rate on backtest |

- **Model role.** Models suggest targets, features and estimators, and write model-card prose from
  bound facts.
- **Code role.** Code does leakage checks, splitting, fitting, search within budget, evaluation and
  guardrails.
- **Registry.** The record lives in the artifact store, with an MLflow file-store export.
- **Loading.** Packages load only when their hash matches a platform-produced record.

## 11. Donor port program (ADR-0018)

Rules (summarised; ADR-0018 is authoritative):

- **What moves.** Port behaviour and tests, not schemas or routers.
- **Governance first.** Every port lands behind the gateway, approvals and router.
- **Traceable origin.** Record `repo@commit:path` in the module and in the capability register.
- **No inherited status.** Done status is earned by AnalystOS tests.
- **No bad dependencies.** No proprietary or non-permissive dependencies come across.

| Wave | Ports | Rows |
|---|---|---|
| 1 (with trust core) | Atlas SQL guard corpus and redaction, prompt-risk and injection defence, relationship inference, semantic diff and formula signatures | P7-09, P7-10, P7-02 |
| 2 (with DE) | DataPilot file ingestion and load modes, quality compiler and quarantine, pipeline codegen emitters; Atlas DQ baselines, SQL/dbt lineage parsers | P6-04..07 |
| 3 (with tools/UX) | DataPilot HTTP tool runtime with SSRF block, query-tool lifecycle, MCP JSON-RPC handler, notebook runtime | P7-11, P7-12 |
| 4 | Parity checklist per donor; owner freezes the donor repository | P7-14 |

## 12. Architecture view (additions to [01-architecture](../10-architecture/01-architecture.md))

```
            web (React) ── /api ──▶ api ──▶ Temporal ──▶ worker pools
                                                          ├─ analysis   (inline capabilities, gateway, router)
                                                          ├─ compute    (process executor, same trust)
                                                          ├─ publish    (BI, exports; approvals)
                                                          ├─ compute-py (isolated: DuckDB/Polars recipes, notebooks)   ◀ new, ADR-0022
                                                          └─ compute-ml (isolated: training, backtests, scoring)       ◀ new, ADR-0022
   isolated pools: no DB/provider credentials · egress only to artifact store · scoped ArtifactRef tokens
   control plane:  definitions (draft/published) · verification records + dependency index · semantic compiler
```

## 13. Evaluation as release gates (P22)

| Suite | Exists | Gate metric (thresholds set by the owner in `config/eval_gates.yaml`) |
|---|---|---|
| Analytical benchmark | P4-V01 | Planted-effect recall, false-discovery rate, confident-wrong = 0 |
| Ask accuracy | P4-V02 (162 questions) | Execution accuracy, abstention, 0 leaks; new governed slice = SQL equivalence with the compiler |
| Semantic compiler | New (P7-02) | Fan-out refusal cases, policy-filter injection, dialect equivalence |
| Grounding | New (P7-07) | % numeric clauses bound to facts; fabricated values = 0; void-on-change cases |
| Pipelines | New (P6-01..05) | Incremental = full rebuild within tolerance; blocking gate keeps last good |
| ML | New (P5-02) | Leakage fixtures refused; baseline parity on identical splits; no-improvement abstains |
| Security | Partly (gateway, P4-01) | Injection corpus (Atlas), cross-workspace, SSRF, token scope |
| Cost/latency | Savings ledger, P4-06 caps | Tokens and $ per accepted output, p95 latency |

CI runs the deterministic tiers on every change to the paths they cover. Live-model tiers run
nightly and before a release. A regression past threshold blocks the merge or release, even when
answers subjectively improve.

## 14. Security additions

- Scoped, short-lived artifact tokens for isolated pools, and no secrets in workers (ADR-0022).
- The injection screen runs at knowledge ingest and before prompts (Atlas port, P7-10).
- SSRF blocking applies to HTTP tools (DataPilot port, P7-11).
- The container sandbox remains P4-02.

## 15. UI (additions to v3 §9)

- **Investigate:** step cards with edit/re-run, a verification state per step, branches and
  compare (P7-04/05).
- **Knowledge:** a relationship cardinality review queue and a semantic diff before approval.
- **Build:** a Pipelines tab (recipes, gates, quarantine, lineage) and a Models tab (experiments,
  model cards, champion/challenger, scoring runs).
- **Operate:** schedule pins and *upgrade available*; void verdicts grouped by cause with
  one-click re-verification.
- **Everywhere:** a *Why this number?* drawer on every number.

## 16. Sequencing

Increment 7 runs alongside the remaining P4 rows. The shared prerequisites are:

- P4-02: the container sandbox, needed by the isolated pools.
- P4-04: the brief and readiness assessment.
- P4-06: work orders and the outbox.

| Wave | Rows | Exit |
|---|---|---|
| A — trust core | P7-01, P7-03, P7-07, P7-10 | Change the SQL or the metric and the badge voids; a schedule survives a pack upgrade unchanged; CI gates on the deterministic suites |
| B — semantic depth | P7-02, P7-09 | Governed Ask questions compile to identical SQL per model version; fan-out cases refused; answers labelled |
| C — compute seam | P7-06 (needs P4-02) | Conformance suite green for `compute-py` and `compute-ml` |
| D — engineering | P6-04..08, then P6-01..03 | Upload → recipe → gates → virtual dataset → dbt export with executed lineage; incremental equals rebuild |
| E — ML | P5-01..06 (needs C) | Train → evaluate → approve → score → monitor on a pilot dataset, with a no-improvement case abstaining |
| F — experience | P7-04, P7-05, P7-08, P7-11, P7-12 | Investigate, branch, verify, publish and reproduce without losing provenance |
| G — donor freeze | P7-14 | Owner signs the parity checklist for each donor |

Wave D may move ahead of E when the pilot's job is engineering-first (as the tracker already says for P6 vs P5).

## 17. Open questions

1. **Governed coverage for the pilot.** Should Ask *refuse* an ad-hoc answer when the question
   names an approved metric the compiler can't serve? Or should it answer `ad_hoc` with a warning?
   The proposal is to answer with the warning and log the gap as a metric-coverage signal.
2. **Estimator allowlist.** Should gradient boosting be scikit-learn only, or also LightGBM/XGBoost?
   Adding them brings native dependencies into the `compute-ml` image.
3. **MLflow.** Is an export enough for the pilot customers, or do they need to import too?
4. **Donor freeze.** Should Atlas's live users (if any) migrate before the freeze, or stay on a
   frozen Atlas release?
5. **LangGraph.** It was declined in favour of playbooks on Temporal. Revisit only if a
   discipline's flow can't be expressed as a playbook plus `expands`.
