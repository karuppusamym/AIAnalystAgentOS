# Capability register (evidence)

Companion to the [tracker](01-tracker.md): the tracker holds status, this file holds dated
evidence. Each row gives the code path, the automated coverage, the live evidence and the
remaining limitation (spec v1 §70). Entries are appended, not rewritten; a later entry supersedes
an earlier one for the same capability.

## 2026-09-25 — Phase 0 + Phase 1 MVP

Environment: compose stack on one host (Postgres 16 + pgvector, Redis 7, Neo4j 5, Temporal 1.25,
Superset 4.1.1 + psycopg2), API + Temporal worker as processes, OpenRouter (chat + Decisions API).
Data: synthetic ServiceNow-shaped dataset (20,000 incidents, 3,000 changes, planted effects and
data-quality defects) served by the bundled Table-API mock.

### Live end-to-end (spec v1 §62 definition of done)

[`evidence/e2e-20260925-054942.md`](evidence/e2e-20260925-054942.md) (+ `.json`): **26/26
checks passed** through the HTTP API, with the Temporal orchestrator, real model calls and a real
Superset publication. Highlights:

* 5 verified findings, all reproducible (re-run result hash identical) and confirmed by an
  independent second method; narratives written by a low-cost model and accepted by the numbers
  guard. They match the planted ground truth: 3+ reassignments → 28.8% vs 9.7% missed SLA
  (3.0x); Network Operations 22.1% vs 10.2%; priority-1 concentration by CI (top 3 = 46.7%,
  Gini 0.57); Payments Gateway P1 concentration.
* Redirect before publication: JEV classified the instruction as `redirect`
  (P(consequential) = 0.09); the LLM interpreted it into the validated filter
  `category != 'inquiry'`; plan v2 reset 9 tasks and removed 9 dynamic tasks; findings were
  recomputed on 16,995 rows.
* Pause while waiting for approval → `PAUSED`; the analyst (editor) was refused approval (403);
  the approver approved; publication to Superset (dashboards 21, 22) completed in 9 s.
* 41 model calls, 19 of them JEV decisions across all six decision purposes; 1 failed call
  (malformed JSON, retried); total model cost **$0.32** for the run.
* Safety probes via the SQL console: restricted/PII column → `sql_rejected`; `DELETE` →
  rejected; `analystos.public.app_user` (control plane) → rejected; a workspace the analyst is not
  a member of → 404.

### Capabilities

| Capability | Code | Automated coverage | Live evidence | Limitation |
|---|---|---|---|---|
| Governed SQL gateway | `gateway/validator.py`, `gateway/service.py` | 109-case validator security suite; service unit tests; live gateway tests (reader cannot write, cannot reach control plane, timeout, audit, cache) | e2e safety probes | SQL Server read-only relies on the login |
| Scope + policy + approvals | `governance/*` | 13 integration tests (PII/restricted denial, clearance, roles, publish gating, payload/plan binding, SoD, policy-version change, revoked approver, expiry, pause persistence) | e2e approval + 403 | Local JWT identity |
| ServiceNow connector | `connectors/servicenow.py` | unit tests vs mock (types, keys, references, display values, pagination, 401) + staging integration | e2e (mock) | **Not certified**: no real instance |
| PostgreSQL connector | `connectors/postgres.py` | integration against compose Postgres | — | — |
| SQL Server connector | `connectors/sqlserver.py` | metadata mapping + T-SQL compile/transpile tests | — | Never run against SQL Server |
| Analytical skills | `skills/*` | 462 tests incl. benchmarks: planted effects found, 6 null controls not supported, every method×derivation×dialect compiled and gateway-validated | e2e findings | Sampled methods cap at 50k rows |
| Model router | `llm/router.py` | fake-transport tests (fallback, fail-closed allowlist, family exclusion, redaction, JSON parsing) | e2e 41 calls | Provider region metadata not available |
| JEV decisioning | `llm/jev.py` | fake-transport tests (score/choice/noul parsing, invalid choice ignored, unavailable → None) | e2e 19 calls, 6 purposes | Escalate-only by design |
| Agent runtime + Temporal | `runtime/*`, `workflows/*` | plan/dependency unit tests; deterministic full-flow integration test (no models, preview destination, 20 s) | e2e on Temporal | — |
| Replanning | `runtime/engine.apply_replan`, `services/runs.submit_feedback` | cycle/stale-plan unit tests | e2e redirect | — |
| Superset publishing | `publishing/superset.py` | 64 unit tests; live integration (15 charts render data, idempotent republish, rollback) | e2e dashboards 21/22 | Report scheduling is Phase 3 |
| Web UI | `web/` | 27 vitest tests, typecheck, build; live smoke across all screens | manual review against running API | Docker image build not verified in this sandbox |
| CI | `.github/workflows/ci.yml` | lint, unit, integration with Postgres/Redis services, web build | green on `51c4e03` | Superset/Neo4j/Temporal tests skip in CI |

## 2026-09-25 — Increment 2: Phase 3 (scheduled & continuous analytics)

Same environment as above plus the `analystos scheduler` process.

### Live evidence

[`evidence/e2e-phase3-20260925-064436.md`](evidence/e2e-phase3-20260925-064436.md): **13/13**.
Earlier pass [`e2e-phase3-20260925-061710.md`](evidence/e2e-phase3-20260925-061710.md) (12/12)
is kept for history: its diff was noisy, which led to carrying claims and KPI definitions forward.

* Scheduled re-analysis (Europe/London cron) re-tested all 4 previously verified claims with
  identical specs: 4 persisting, 0 resolved, 0 not re-tested; 10 KPIs with 0.0 change on unchanged
  data; publication tasks skipped; weekly report generated (PDF 212 KB, XLSX 36 KB, HTML 17 KB)
  and downloaded through the audited, hash-checked endpoint.
* Monitor schedule evaluated 4 monitors (drift, change point, threshold, data quality) through
  the gateway; the threshold alert was triaged by JEV (`typesafe/jev-1.13`) and escalated; the
  monitor's automatic investigation run completed with `origin: alert` and no publication.
* Notifications for the report, the alert and the investigation.

| Capability | Code | Automated coverage | Live | Limitation |
|---|---|---|---|---|
| Scheduler | `services/schedules.py` | validation, timezone, idempotent claim, run-now path | ✅ | no back-fill of missed slots |
| Re-analysis diff | `services/changes.py`, investigator carry-forward, semantic carry-forward | claim identity unit tests; same-data integration assertion (0 resolved / 0 not re-tested / equal KPIs) | ✅ | diff granularity is per claim, not per group value |
| Reports | `reports/*`, `services/reports.py` | 63 renderer tests (escaping, formula injection, determinism) + integration download | ✅ | core PDF font |
| Monitors + alerts | `services/monitors.py` | drift/threshold/change-point unit tests; integration (alert, de-dup, DQ baseline, auto-investigation) | ✅ | DQ monitor re-profiles each run (cost grows with columns) |
| Notifications | `services/notifications.py` | integration | ✅ | in-app only |
| Phase-3 UI | `web/src/pages/{Schedules,Monitoring,Reports}.tsx` | 23 new vitest tests | smoke | monitor config not editable after creation |

## 2026-09-25 — Increment 3: universal sources, metadata crawler, token economy, admin control plane

Same environment as above. The source for the live run is a seeded **SQLite** retail database, not
ServiceNow: 24,000 orders, 2,500 customers, 60 products, 4 regions. It contains three planted
effects and null controls:
- marketplace orders are returned about 3× as often;
- Enterprise order value is about 2.1× higher;
- APAC shipping takes about 3 days longer;
- payment method, discount and coupon have no effect.

### Live evidence

[`evidence/e2e-increment3-20260925-150550.md`](evidence/e2e-increment3-20260925-150550.md): **13/13**.

The run went through the HTTP API, with the Temporal orchestrator, real model calls under the
`token_saver` preset, and a real Superset publication. Earlier passes are kept for history:
- [`…145158`](evidence/e2e-increment3-20260925-145158.md) (12/13): three duplicate driver-model
  findings, and only the boolean outcome was tested.
- [`…145827`](evidence/e2e-increment3-20260925-145827.md) (12/13): measures were tested against one
  dimension only.
- [`…150310`](evidence/e2e-increment3-20260925-150310.md) (12/13): KPI × dimension follow-ups ran,
  but were ordered naively.

Each of those passes changed the investigator (see tracker P3 findings).

**Catalog and crawl**
- **Any database.** `/api/source-kinds` lists 15 kinds. The SQLite file was uploaded and registered
  as a `sqlite` source, which is staged.
- **Discovery.** Discovery is a full crawl with **0 model calls**. It derived role/domain/grain for all
  4 tables (orders = fact, sales, "one row per order", confidence 0.82). It tagged `customer.email`
  and `customer.customer_name` as PII by name rules, recorded 3 declared relationships and wrote 4
  context entries plus the graph.
- **Incremental crawl** after staging: 4 unchanged, 0 touched, 4 profiled and value-sampled through
  the gateway.
- **Drift.** A column `coupon_code` was added and a full crawl run. It reported exactly one changed
  table with `added: [coupon_code]` and nothing deprecated. It **re-staged** the changed selected
  table and profiled with 0 errors. The owner's reviewed description was kept, and a
  `schema_change` notification was raised.

**SQL and admin**
- **Explain.** Deterministic, with no execution. The PII column query was rejected by the gateway
  with its reason, and the rejection was audited.
- **Admin.** The `token_saver` preset was applied at runtime (`/api/admin/models` shows
  `hypothesis_generation: auto`, `verification: always`). The previous settings version was
  restored at the end.

**Analysis**
- **Run:** 14 hypotheses over 3 rounds, all deterministic: rule playbook and KPI × dimension
  follow-ups.
- **Found:** 4 verified findings, all 3 planted effects:
  - returns driven mainly by channel (marketplace odds ratio ≈ 3.25);
  - returns concentrate in channel = marketplace;
  - APAC has the longest shipping;
  - Enterprise has the highest order value.
- **Rejected:** all 10 non-planted combinations.
- **Model use:** 10 model calls (verification and JEV where required), **15 deterministic skips**.
  Cost **$0.023**, 8,048 tokens. This is not directly comparable with the MVP run's $0.32, which
  used another dataset and the default `always` modes.
- **Publication:** after approver approval, to Superset (dashboards 39/40, 7 KPIs, 9 charts).

**Monitoring and scheduling**
- A forecast-deviation monitor was evaluated on weekly orders.
- A `crawl` schedule run-now succeeded.

### Capabilities

| Capability | Code | Automated coverage | Live | Limitation |
|---|---|---|---|---|
| Source-kind catalog + generic SQL connector | `config/source_kinds.yaml`, `connectors/{kinds,generic_sql}.py` | 120 unit tests; integration against MySQL 8.4 (docker), SQLite, DuckDB and Postgres: discovery, read-only session, staging, gateway query with a denied column | SQLite (this run) | Not certified: SQL Server, Oracle, Snowflake, BigQuery, Databricks, Trino, Redshift, ClickHouse, MariaDB |
| Metadata crawler | `services/crawler.py`, `skills/catalog.py` | 72 catalog-skill tests; crawler rule tests; integration lifecycle (drift, curation kept, deprecation only on full uncapped crawls, re-staging) | ✅ | English keyword heuristics; renames are candidates only |
| Token economy | `llm/router.py`, `llm/cache.py`, `agents/common.model_gate` | router tests: off/auto, cache, oversize refusal, overrides, downgrade within allowlists | ✅ 15 skips in one run | `tokens_saved` for skips is an estimate (prompt-size heuristic) |
| Admin control plane | `contracts/platform.py`, `services/platform_settings.py`, `api/routers/admin.py` | integration: admin-only, reference validation, preset → router, rollback, stale-merge guard | ✅ preset + rollback | 5 s propagation between processes |
| SQL explain | `skills/sqlexplain.py` | unit tests | ✅ | Explains structure, not intent |
| Forecasting + forecast-deviation monitor | `skills/forecast.py`, `services/monitors.py` | 18 forecast tests (coverage over 30 seeds) + monitor test | ✅ | Holt-Winters ~0.1–0.3 s per series |
| Investigator breadth | `agents/investigator.py` | role playbook, outcome-diverse selection, driver-model deduplication, KPI × dimension continuation tests | ✅ 3/3 planted, 0 false | Continuation is one dimension per outcome per round |
| Increment-3 UI | `web/src/pages/{Catalog,AdminSettings}.tsx`, `components/CrawlPanel.tsx` | 26 new vitest tests (76 total) | smoke incl. Catalog | Column-level curation not in the UI |

## 2026-09-25 — Design review and next delivery scope

The [design review](04-design-review.md) inspected spec v2, architecture/ADRs, the existing
tracker/readiness/evidence, the UI screen map and relevant API, service, contract and verification
code. The resulting target design covers workspace semantics, UI/API evolution, ML, engineering,
evidence and evaluation. P4–P6 in the tracker contain the implementation acceptance criteria.

This is documentary evidence only. No runtime capability, test count, connector certification
or release-readiness promotion is added. Existing live runs were not repeated. Validation for
this change checks changed Markdown links, code fences, tracker IDs/status consistency and diff
whitespace; application tests and browser execution are not part of this documentation change.
## 2026-09-25 — Increment 4, wave 0 (plus X05–X08 and U01)

Same environment. Built in parallel streams, merged with re-chained migrations
(0005 → 0006 → 0009 → 0007 → 0008 → 0010, verified up, down to 0005 and up again).
Suites at merge: 1,197 unit, 102 integration, 114 vitest, 21 Playwright.

### Live evidence (P4-C08)

| Evidence | Result | Notes |
|---|---|---|
| [`e2e-20260925-171115.md`](evidence/e2e-20260925-171115.md) | 26/26 | Full model use; 382 s; $0.29; 7 distinct KPIs, all percent KPIs fractions; new cross-workspace isolation probe passes |
| [`e2e-phase3-20260925-171755.md`](evidence/e2e-phase3-20260925-171755.md) | 13/13 | One alert (de-duplicated), but it read 0.3827 — see findings |
| [`e2e-20260925-173738.md`](evidence/e2e-20260925-173738.md) | 26/26 | After the fixes; OpenRouter returned HTTP 402 (credits) for large `max_tokens` requests, so planning, hypotheses, follow-ups and semantic modelling ran deterministically; 81 smaller calls succeeded |
| [`e2e-phase3-20260925-173848.md`](evidence/e2e-phase3-20260925-173848.md) | 13/13 | One alert at 0.04192 (matches the data); KPI definition stable across the scheduled run |
| [`sse-load-20260925-165049.md`](evidence/sse-load-20260925-165049.md) | pass | 200 streams, p95 38.9 ms, 0 slow-callback warnings |

**Defects found by the live re-runs, fixed and tested:**
- A low-cost model (`deepseek/deepseek-v4-flash`) wrote a finding in Chinese that passed the numbers
  guard. Model narratives must now be English; words quoted from the evidence may be in any script.
- A scheduled run's model re-proposed `critical_incident_rate` as `priority = 1 OR impact = 1 OR
  urgency = 1`, and the artifact upsert silently replaced the carried-forward definition. A KPI name
  now keeps its first definition; runs without a scheduled predecessor inherit the workspace's latest
  completed run's definitions.
- The threshold monitor resolved its KPI by name and followed the redefinition, alerting on 0.38
  instead of 0.04. Monitors now pin the definition they measure (`config.pinned`, ignored by the
  condition key).
- On HTTP 402 the router tried every fallback model per call (32 failed calls). A credit refusal now
  cools the provider down for 60 s, and callers take their deterministic path without a request.

### Capabilities

| Capability | Code | Automated coverage | Live | Limitation |
|---|---|---|---|---|
| Workspace model policy (samples, providers, cost approval, residency) | `llm/router.py` `CallContext.for_policy` | `test_model_policy.py` | — | No provider regions configured yet: residency blocks all models until set; prices are list-price estimates |
| Structured prompt trimming, prompt version hashes | `agents/common.fit_payload`, `agents/prompts.py` | `test_prompt_payload.py` | ✅ | — |
| Replayable model calls | `llm/replay.py`, `model_payload` | `test_model_replay*.py` | — | Replays calls, not a whole run end to end |
| Query budgets (critic re-runs, Ask) | `governance/budgets.py`, `agents/critic.py` | `test_tool_gate_and_budgets.py` | ✅ | — |
| Workspace isolation in depth | `staging/roles.py`, gateway `SET LOCAL ROLE`, lineage key, Neo4j keys | `test_workspace_isolation.py` (validator bypass) | ✅ probe | The reader login can `SET ROLE` to any workspace role; isolation relies on the gateway choosing the role |
| Tool-gate coverage | `ToolRuntime.authorize`, `gate_agent_write` | `test_tool_gate_and_budgets.py` | ✅ | Exempt paths named in spec v2 §6 |
| Non-blocking SSE | `events/stream.py` | `test_sse_stream.py`, load script | ✅ 200 streams | Measured on the standard asyncio loop |
| Replan supersedes artifacts | `artifact.plan_version` | `test_e2e_local_run.py` redirect test | ✅ | — |
| Population of staged snapshots | `connectors/sampling.py`, `staging/snapshots.py`, critic `representative_population` | `test_snapshot_sampling.py`, `test_snapshot_population.py` | — | ServiceNow/files: `full` and `first_n` only |
| Capability manifests and registry | `contracts/capability.py`, `capabilities/registry.py` | `test_capability_registry.py` | — | Enablement, certification gating and plan-hash binding pending (X01) |
| MCP client | `mcp/client.py` | `test_mcp_client.py` (SDK-built test double) | — | Not registered against a real Superset/dbt MCP server; no host allowlist |
| MCP server | `mcp/server.py`, `mcp/grants.py` | `test_mcp_server.py` | — | No per-client OpenAPI, no MCP prompts, no OAuth yet |
| Domain packs | `packs/{itsm,sales}`, `skills/hypothesis_templates.py` | grep test, pack benchmarks (3/3 planted each) | ✅ via demo | Pack KPIs not yet read by the semantic agent |
| Connector certification from evidence | `connectors/certification.py`, `scripts/certify_connectors.py` | `test_connector_certification.py` | postgres, mysql, sqlite, duckdb | Warehouses remain `tested` |
| Five-journey UI shell | `web/src/routes.ts`, `components/CommandPalette.tsx` | 114 vitest, 21 Playwright (axe AA) | — | Journey content (U02–U07) not built |


## 2026-09-25 — Increment 4, wave 2 (token and decision economy) plus S02, U03, U06, U07

Merged from parallel streams onto `claude/gracious-knuth-ievauj`. Migrations chain
0011 → 0015 (registries) → 0016 (task claims) → 0012 (ladder) → 0013 (decisions) → 0014 (prompt cache).
Up/down/up was verified on a scratch database after each re-chain.

### Measured (fake transport; live model runs are blocked by exhausted OpenRouter credits)

| Measure | Before | After | Evidence |
|---|---:|---:|---|
| Chat calls per standard run | 20 | 0 | `evidence/token-default-20260925-183030.md` |
| Decision calls per standard run | 15 | 7 (`rev_second_opinion`) | same |
| Tokens per standard run | 25,075 | 1,435 | same; guarded by `scripts/cost_gate.py` in CI |
| Prompt tokens, compiled purposes | 18,942 | 15,342 (−19 %) | `evidence/context-compiler-20260925-182819.md` |
| Ask registry hit latency | — | p50 < 1 s asserted (no model call) | `test_registries.py` |
| Queries per engine task | measured | — | `evidence/engine-queries-20260925-182754.md` |

### Capabilities

| Capability | Code | Automated coverage | Live | Limitation |
|---|---|---|---|---|
| Execution ladder, `answered_by` per call | `llm/router.py` (`ladder`, `mode_of`), `config/models.yaml` `ladders:` | `test_ladder_and_budgets.py` | — | The JEV rung counts as a model rung, so `off` removes it |
| Deterministic-first default | `contracts/platform.py` presets, `agents/{publisher,visualization,critic}.py` | `test_token_default.py`, `test_cost_gate.py` | — | Planning is not yet merged into hypothesis generation |
| Context compiler with receipts | `context/compiler.py`, `agents/common.compile_for` | `test_context_compiler*.py`, `test_context_tokens.py` | — | Lexical relevance only (hybrid ranking is P4-K05) |
| Prompt-cache layout and accounting | `llm/router.py` `wire_messages`, `cached_prompt_tokens` | `test_prompt_cache.py` | ❌ not measured | Stable prefix ≈ 18 % of sent text; the ≥ 60 % target is unlikely without a larger header |
| Verified-query and hypothesis registries | `registries/`, `agents/sql_agent.py`, `agents/investigator.py` | `test_registries.py`, `test_registry_logic.py` | — | — |
| Knowledge version in cache keys | `context/version.py` | `test_context_compiler_db.py` | — | — |
| Redis budget counters, price table | `runtime/budget_counters.py`, `runtime/usage.py` | `test_budget_counters.py` | — | Ask's rolling-hour budget still counts in the database; JEV has no list price (`missing_price`) |
| DecisionService, authority classes | `decisions/` | `test_decision_service.py`, `test_decisions_db.py` | — | Local-classifier weights are hand-set (v1) |
| Decision calibration and downgrade | `decisions/calibration.py`, `analystos calibrate` | `test_decision_calibration.py`, `test_decisions_db.py` | — | No Operate screen for the report yet |
| CI cost gate | `scripts/cost_gate.py`, `tests/fixtures/cost_baseline/` | `test_cost_gate.py` | — | New agent call sites are only caught once the fixture is re-recorded, or by `test_token_default.py` |
| Engine loop without locks or N+1 | `runtime/engine.py`, `artifacts/registry.py`, `governance/policy.py` | `test_engine_claims.py`, `test_engine_queries.py` | ✅ | In-flight Temporal workflows must be drained before deploying |
| Investigation board, Operate, capability-driven UI | `web/src/` | vitest, Playwright journeys | — | Capability invoke endpoint pending (P4-U02 stream) |

### Merge decision recorded

When the ladder and the DecisionService were merged, their meanings of `auto` differed. The
ladder used `auto` to mean "rules first, then the model". The DecisionService used it to mean
"the rule decides".

The resolution: under `auto`, the service moves `rules` to the front of the purpose's backend
chain, so JEV answers only what the rule leaves open (a tie, an abstention, an escalation). `off`
removes the model backends. Direct `JevDecisions` callers keep "auto = the rule decides". The
cost gate is unchanged (7 and 3 calls).

## 2026-09-25 — Increment 4, wave 3: semantic layer (P4-K03)

| Capability | Code | Automated coverage | Live | Limitation |
|---|---|---|---|---|
| Ossie 0.1.1 semantic model, pinned schema | `semantic/ossie.py`, `semantic/schema/` (+ `PROVENANCE.yaml`) | `test_semantic_ossie.py` (upstream examples) | — | Upstream's Salesforce fixture fails its own SQL rule; recorded, not hidden |
| Metric approval workflow | `semantic/service.py`, `governance/approvals.py` (`ALWAYS_SEPARATE_DUTIES`) | `test_semantic_layer.py` | ✅ deterministic run: run 1 refused at publish, approved, run 2 publishes (37 s) | Semantic-model structure versions have no approve endpoint |
| dbt 1.12 import/export | `semantic/dbt.py` | `test_semantic_ossie.py` fixtures from real `dbt parse` | ✅ by hand (dbt-core 1.12.0 + metricflow 0.213.0) | dbt is not in the project environment; metricflow mangles percent KPIs (export warns) |
| Publish gate on approved metrics | `semantic/service.gate_bundle`, `build_bundle` | `test_semantic_ossie.py`, `test_semantic_layer.py` | ✅ | Existing workspaces default to off; new ones on |

## 2026-09-25 — Increment 4, waves 4–6: build, Ask, self-hosting, evaluation (E04–E06, U02, S04, V01)

Migrations 0017 → 0020 (build gateway) → 0021 (Ask threads) → 0023 (user identity); up/down/up verified.

| Capability | Code | Automated coverage | Live | Limitation |
|---|---|---|---|---|
| BuildGateway write identity | `build/gateway.py`, `build/targets.py`, `deploy/postgres/01-init.sql` | `test_elt_build.py` (Postgres refuses non-target, source and `public` writes; approval tamper/expiry) | ✅ | Postgres analytics engine only |
| dbt builder playbook | `build/project.py`, `build/runner.py`, `build/lineage.py`, `playbooks/elt_build.v1.yaml` | `test_build_project.py`, `test_elt_build.py` | ✅ dbt Core 1.12.5, 20,000 rows, 3/3 tests (`evidence/elt-build-dbt-20260925.md`) | dbt lives in a separate venv (`ANALYSTOS_DBT_EXECUTABLE`), not in the worker image; tables only (no incremental) |
| dlt vs staged loader | `scripts/spike_dlt_vs_staged.py`, ADR-0016 | — | ✅ measured | Static mock data |
| Ask threads, stages, decisions, promote | `services/ask.py`, `api/routers/ask.py`, `agents/sql_agent.py` | `test_ask_threads.py`, `test_ask_threads_api.py`, vitest, Playwright | — | Tool rung unused; dashboard promote creates an approved chart, not a Superset publish |
| Capability invoke | `capabilities/invoke.py` | `test_ask_threads*.py` | — | Methods run only inside investigations |
| Paste-SQL explain | `QueryGateway.explain` | `test_ask_threads_api.py` | — | Postgres plans only |
| Model providers + air-gapped egress guard | `llm/providers.py`, `config/models.airgapped.yaml` | `test_model_providers.py` | ❌ no local model run | Bedrock against a double |
| OIDC SSO + ABAC | `security/oidc.py`, `governance/policy.py` | `test_oidc_abac.py`, `test_oidc_sso.py` (fake IdP) | ❌ | No real IdP login yet |
| Sandbox network isolation | `sandbox/` | sandbox tests | ✅ | Falls back to not isolated under the chart's default seccomp; NetworkPolicy is the boundary |
| Helm chart, offline bundle | `deploy/helm/analystos`, `scripts/bundle_images.sh` | `test_helm_chart.py` (skips without helm) | — | helm not in CI |
| Analytical benchmark | `evaluation/`, `scripts/benchmark_analytical.py` | CI `--check` (precision/recall ≥ 0.90, FDR ≤ α) | ✅ deterministic + platform tiers (`evidence/2026-09-25-analytical-benchmark-*.md`) | No live-model report; effects well above materiality |

## 2026-09-25 — Increment 4, wave 3 knowledge and wave 4 engines (K01, K02, K09, K10, E01, E03)

Migration 0018 (knowledge packs) now follows 0023. The chain is 0017 → 0020 → 0021 → 0023 → 0018, and it has been checked up, down and up.

| Capability | Code | Automated coverage | Live | Limitation |
|---|---|---|---|---|
| OKF knowledge packs, revisions, index | `knowledge/{okf,store,index,platform}.py` | `test_knowledge_okf.py`, `test_knowledge_pack.py` (reindex identity, HNSW, isolation) | ✅ seeded platform pack (27 documents) | `ts_rank_cd`, not BM25 |
| OKF / Atlas bundle import and export | `knowledge/bundle.py` | real Atlas-exported sample, byte-identical round trip | — | No export API route |
| Context providers | `knowledge/providers.py`, `mcp/client.py` | MCP SDK server + MOCK fixture | ❌ no live Atlas | External results not in prompts yet |
| Embedding providers, re-embed | `knowledge/embeddings.py`, `scripts/benchmark_embeddings.py` | benchmark evidence | — | Default model chosen on the benchmark set |
| Engine protocol, multi-dialect validator | `engines/`, `gateway/dialects.py` | `test_gateway_dialects.py` (286 cases) | ❌ no warehouse instance | Snowflake/BigQuery/Databricks/Trino stay staged |
| Federated cross-source runs | `QueryGateway._execute_federated`, `skills/federation.py` | `test_federation.py`, `test_cross_source_runs.py` | ✅ Postgres + DuckDB file | Not yet a playbook step; no filter pushdown per leg |

Evidence: `evidence/2026-09-25-knowledge-k01-k10.md`, `evidence/engines-federation-20260925.md`.
