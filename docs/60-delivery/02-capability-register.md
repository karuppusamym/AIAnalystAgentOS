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
