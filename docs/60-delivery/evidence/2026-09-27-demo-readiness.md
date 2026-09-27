# Demo readiness — 2026-09-27

Live proof that the demo path works end to end from a fresh database, what was fixed on the way, and what was
**not** proven. Raw reports, logs and screenshots: [`2026-09-27-demo-readiness/`](2026-09-27-demo-readiness/).
Walkthrough: [runbook 05](../../30-runbooks/05-demo-walkthrough.md).

## Environment

* Base commit `c288403`; the fixes below applied (worktree `agent-afcfcbf16d734bf45`).
* Linux container, no Docker daemon. Postgres 16 + pgvector, Redis and a Temporal dev server shared with other
  sessions; isolated by fresh databases (`analystos_live_e`, analytics `analytics_live_e`), Redis db 5 and Temporal
  queue prefix `e5`. API on :8015 (uvicorn), `analystos worker`, `analystos scheduler`, the ServiceNow mock on
  :8095, the web UI (Vite) on :5195 (5185 was taken by another session).
* **Superset 4.1.1, real:** `pip install apache-superset==4.1.1 psycopg2-binary` in a scratch venv (27 s; plus
  `setuptools<70` and `marshmallow<4`, which the unpinned PyPI install needs and the compose image already pins),
  SQLite metadata, gunicorn on :8098, the repository's `deploy/superset/superset_config.py` settings.
* **No model key** (`OPENROUTER_API_KEY` unset): every step took its deterministic rung.

## Results — standard profile, fresh database (final run, all fixes applied, 16:45 UTC)

| Command | Result | Report |
|---|---|---|
| `analystos migrate && analystos seed` | alembic head `0042`, seeded users/registries/packs | — |
| `scripts/e2e_demo.py --api http://127.0.0.1:8015 --servicenow http://127.0.0.1:8095` | **26/26**; published to Superset (dashboards 15, 16); run 28.4 s | [standard/e2e-20260927-164507.md](2026-09-27-demo-readiness/standard/e2e-20260927-164507.md) |
| `scripts/e2e_phase3.py` (the workspace above) | **13/13**; re-analysis 11.4 s, alert investigation 20.4 s | [standard/e2e-phase3-20260927-164543.md](2026-09-27-demo-readiness/standard/e2e-phase3-20260927-164543.md) |
| `scripts/e2e_increment3.py` | **13/13** | [standard/e2e-increment3-20260927-164636.md](2026-09-27-demo-readiness/standard/e2e-increment3-20260927-164636.md) |
| `analystos demo-seed` (first run) | workspace, ServiceNow source + 2 tables loaded, brief (6 assertions + reviewed grains), CSV + recipe preview, investigation (6 verified findings, approved, published to Superset 19/20), schedule, monitor evaluated | [standard/demo-seed-1.log](2026-09-27-demo-readiness/standard/demo-seed-1.log) |
| `analystos demo-seed` (second run) | every item `exists`; nothing created | [standard/demo-seed-2.log](2026-09-27-demo-readiness/standard/demo-seed-2.log) |
| Superset chart data API on the published dashboards | executive 7/7 and operations 5/5 charts return data | screenshot: [after fix](2026-09-27-demo-readiness/screens/superset-histogram-after-fix.png) |

Earlier fresh-database runs the same day (before the histogram and nudge fixes) also passed 26/26, 13/13 and
13/13; they found the failures below.

## Results — lite profile (no Redis, Temporal, worker or Superset)

Fresh databases, `ANALYSTOS_PROFILE=lite`, the API started through a launcher that first removes what Windows
lacks (`resource`, `fcntl`, `pwd`, `grp`, `termios`; `os.getuid/getgid/setsid/killpg/fork/...`;
`signal.SIGKILL/SIGUSR1/SIGALRM/SIGHUP/...`) — a simulation on Linux, **not a run on Windows**.

| Check | Result |
|---|---|
| Import every `analystos` module with those removed | 0 failures (so no guard was needed beyond `workers/isolation.py` and `sandbox/runner.py`) |
| API without the `temporal`, `ml`, `graph` extras (`pip install -e .[reports]`) | imports; `/api/health` all `up`/`off`, no problems |
| `analystos demo-seed` against API + web proxy (:5195 → :8025) | investigation to completion on the local orchestrator; publication to the in-platform preview ([lite/demo-seed-lite.log](2026-09-27-demo-readiness/lite/demo-seed-lite.log)) |
| `scripts/e2e_demo.py` through the web proxy | 25/26; only `16_published_to_superset` fails, by design (no Superset in lite) ([lite/e2e-20260927-155334.md](2026-09-27-demo-readiness/lite/e2e-20260927-155334.md)) |
| UI with the API stopped | banner "The AnalystOS API is not responding" ([screen](2026-09-27-demo-readiness/screens/ui-lite-api-down.png)) |

## Failure clarity (standard profile)

| Fault injected | `/api/health` | UI banner | Run behaviour |
|---|---|---|---|
| `analystos worker` stopped (SIGTERM) | `checks.worker.state = down` after ~100 s (Temporal lists a poller until it is 120 s stale) | "Worker: No worker is running …" ([screen](2026-09-27-demo-readiness/screens/ui-worker-down.png)) | runs stay queued |
| Superset stopped | `superset` down, `problems[0].severity = warning` | "Superset: Superset is not reachable …" ([screen](2026-09-27-demo-readiness/screens/ui-superset-down.png)) | `e2e_demo.py` 25/26: the run published to the preview and said "Superset not reachable (… Connection refused); publication will target the local preview destination." ([no-superset/e2e-20260927-161746.md](2026-09-27-demo-readiness/no-superset/e2e-20260927-161746.md)) |
| API stopped | — | "API: The AnalystOS API is not responding …" | — |

## Root-cause fixes (each with a regression test)

| Failure (how it showed) | Root cause | Fix | Test |
|---|---|---|---|
| `e2e_demo` 25/26: `decisions_recorded_with_rung` failed without a model key | rule-decided decisions were entered in the run's ledger only when JEV could have answered, so a keyless run's console showed no `hypothesis_priority` / `chart_selection` | `decisions/service.py::_record_avoided_calls` records the rule's decision with 0 tokens saved when no model route exists | `tests/unit/test_decision_service.py::test_keyless_rule_decision_is_still_in_the_run_ledger_without_claimed_savings` |
| `e2e_phase3` crashed: `'NoneType' object has no attribute 'startswith'` | the script read `triage.model` (None without a key) | script: JEV triage is required when `/api/health` says JEV is available, otherwise the rules triage must be recorded with its decision id (`alert_triaged_by_rules_jev_unavailable`) | live run above |
| `e2e_phase3` re-run: `threshold_alert_raised` failed | one open alert per condition and period, whichever monitor observed it (by design); the script looked the alert up by the new monitor's id | script: look the alert up by the id the evaluation returned | live run above |
| Superset operations dashboard: "Column 'opened_at_to_resolved_at_hours' contains non-numeric values" | Superset 4.1's histogram operator refuses any NULL; open incidents have no resolution time | `publishing/superset_charts.py`: histogram charts filter `IS NOT NULL` in the query and the saved explore filters | `tests/unit/test_publishing_charts.py::test_histogram_counts_only_rows_with_a_value`; screenshots before/after |
| A publication started 5 minutes after its approval (`publish_request` 16:18:09, approval 16:18:14, `publish` 16:23:15) | `AnalysisWorkflow._wait` cleared the nudge flag when the wait began, so a nudge that arrived while `get_state` ran (approval right after a resume) was lost and the 300 s re-check ran | `workflows/analysis_workflow.py`: clear the flag before the state read, behind `workflow.patched("nudge-cleared-before-state")` so in-flight histories replay | `tests/unit/test_workflow_nudge.py` (4 tests); every later run: approval → publish gap 0 s |
| `kill` of `analystos worker` left six process-pool children running, holding DB connections | no SIGTERM handler: the process died without unwinding the pools | `workflows/worker.py::_serve_until_terminated` shuts the Temporal workers down on SIGTERM so the ExitStack closes the pool | `tests/unit/test_worker_sigterm.py`; live: SIGTERM → "shutting down 5 workers", no orphans |
| Superset unreachable → silent fallback | `choose_destination` announced only raised errors; `test_connection` reports outages by returning `ok: False` | `agents/publisher.py::choose_destination` says "Superset not reachable (…)" in both cases | `tests/unit/test_health_states.py::test_unreachable_superset_is_announced_when_publication_falls_back_to_preview` |
| No way to see a missing worker; UI hung when a dependency was down | `/api/health` had no worker check and no plain state; the UI never read it | `/api/health`: `checks.<dep>.state` (`up`/`down`/`off`), a `worker` check (fresh Temporal pollers on the run queues, `workflows/orchestrator.py::worker_status`), `degraded` and `problems` with severity and plain impact; web `StatusBanner` polls it (8 s timeout) | `tests/unit/test_health_states.py`, `web/src/test/statusBanner.test.tsx` |

## Added for the demo

* `analystos demo-seed` (`src/analystos/demo/seed.py`, domain data in `packs/itsm/demo.yaml`): idempotent, through
  the API, tests in `tests/unit/test_demo_seed.py`.
* `scripts/demo.ps1` and `scripts/demo.sh`: compose up (full or `-Lite`), wait for health, migrate, seed,
  demo-seed, print URLs and logins; `-Reset`, `-Down`. Dry-run with stubbed `docker`/`curl` under bash and
  **PowerShell 7.4** (pwsh on Linux): each mode issues the expected compose commands and prints the summary. Not run
  against a real Docker daemon, and not run under Windows PowerShell 5.1.
* `compose.yaml` static check (`tests/unit/test_compose_static.py`): parses; every build context, Dockerfile, mount
  and entrypoint exists; host ports unique; `service_healthy` dependencies have healthchecks; every `ANALYSTOS_*`
  variable in compose and `deploy/compose/*.env` is a real setting; the default services are exactly
  postgres/api/web; every profile in runbook 04 exists; `.sh` files stay LF (`.gitattributes`). Superset's entrypoint
  now runs through `bash` so a Windows bind mount without the executable bit still starts it.

## Not proven

* Nothing ran under Docker (no daemon here): the compose images were not built, and the compose stack, the
  Superset image and `scripts/demo.ps1` against Docker Desktop are unproven. The Superset used here is the same
  version (4.1.1) installed with pip.
* Nothing ran on Windows: the lite path's Windows compatibility is by simulation and the import audit only.
* No model key: JEV triage, model-written hypotheses and narratives were not exercised (the deterministic paths were).
* The live-journey suite (`web/e2e-live/`, added on main after this base) was not run against this stack; the UI
  steps of runbook 05 were driven with ad-hoc Playwright scripts instead (start Explain with a chosen source, the
  analyst's approval refused with "role 'editor' cannot approve", the approver's approval, Prepare data, Ask,
  Evaluate now).
* The seeded workspace turns *require approved metrics* off (as `e2e_demo.py` does); a workspace created live keeps
  the default and stops at `publish_request` until its KPIs are approved (runbook 05 §1).
* The web suite has one failing test on the base commit as well (`knowledge.test.tsx` "lists governed and inferred
  edges where no canvas is available"); not touched here.
