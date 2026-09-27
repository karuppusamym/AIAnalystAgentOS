# Real-API browser journeys — P4-07, P7-05, P7-19 (2026-09-27)

Until today every Playwright journey in `web/e2e/` ran against the mocked API (`web/src/test/mockBackend.ts`).
This file records the first journeys driven through the real UI against a real API, worker, database and
ServiceNow mock, with nothing intercepted: `web/e2e-live/` (config `web/playwright.live.config.ts`, script
`npm run test:e2e:live`, skipped unless `LIVE_BASE_URL` is set).

## Stack

Branch base `c288403`, built from this worktree. Postgres 16 + pgvector (control DB `analystos_live_c`, analytics
DB `analytics_live_c` with the loader/reader/builder/writer identities of `deploy/postgres/01-init.sql`), Redis
db 3, Temporal dev server with queue prefix `c3`, ServiceNow mock on :8093, `analystos worker` (all queues),
API on :8013, `vite preview` of the production bundle on :5183 proxying `/api`. No Superset (publication falls
back to the in-platform `preview` destination, as designed); no model key (every agent on its deterministic
path; the runs billed $0.00).

```bash
# once: databases, schema, seed
psql -h /tmp -U analystos -d postgres -c "CREATE DATABASE analystos_live_c OWNER analystos"
psql -h /tmp -U analystos -d analystos_live_c -c "CREATE EXTENSION IF NOT EXISTS vector"
psql -h /tmp -U analystos -d postgres -c "CREATE DATABASE analytics_live_c OWNER analystos_loader"
export ANALYSTOS_DATABASE_URL=postgresql+psycopg://analystos:analystos@localhost:5432/analystos_live_c
# and ANALYSTOS_ANALYTICS_LOADER_URL / _READER_URL / _BUILDER_URL / _WRITER_URL, each its identity on analytics_live_c
export ANALYSTOS_ANALYTICS_READER_URL=postgresql+psycopg://analystos_reader:reader@localhost:5432/analytics_live_c
export ANALYSTOS_REDIS_URL=redis://localhost:6379/3 ANALYSTOS_TEMPORAL_QUEUE_PREFIX=c3
export ANALYSTOS_SERVICENOW_MOCK_URL=http://localhost:8093 SERVICENOW_PASSWORD=admin
analystos migrate && analystos seed
uvicorn analystos.connectors.servicenow_mock:app --port 8093 &
analystos worker &
uvicorn analystos.api.app:app --port 8013 &
cd web && npm run build && ANALYSTOS_API_URL=http://localhost:8013 npx vite preview --port 5183 &
# the suite
LIVE_BASE_URL=http://127.0.0.1:5183 PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers npm run test:e2e:live
```

Each execution creates a fresh workspace (`Live journeys <stamp>`), so the suite is repeatable on one stack.

## Results

Four full executions today. The first two exposed bugs (below); after the fixes **executions 3 and 4 were clean:
13/13 passed each** (3.8 min and 3.4 min).

| Journey | File | Result |
|---|---|---|
| Owner creates the workspace (all three work modes), adds the analyst (editor) and the approver, relaxes `require_approved_metrics` through the policy's Advanced JSON (as `scripts/e2e_demo.py`) | `01-analyst` | pass |
| (a) Analyst: no admin nav; connect ServiceNow, Discover (4 assets), select incident + change_request (staged), refresh and confirm every brief suggestion, Start work → Explain (brief prefilled, preflight, readiness) → the live board reaches verified findings (≥ 3) and waits for the publication approval | `01-analyst` | pass |
| (b) The analyst's own Approve is refused with the server's reason; the approver approves in a second browser context; the analyst sees COMPLETED and the approval `executed` with the reason | `01-analyst` | pass |
| (a) "Why this number?": verified, "every link still holds", all six links (fact, step, query + SQL, data, metric definition, verification); an Executive report generated with its formats and source link | `01-analyst` | pass |
| (c) Analyst: no admin screens in nav or the command palette; direct URLs say "…is for platform administrators" / "Workspace owners manage members and policy" | `02-restrictions` | pass |
| (c) Approver: Explain says "needs the analyst role here; you are approver" and has no start button; brief suggestions not offered; a Discover (forbidden write) shows the server's refusal | `02-restrictions` | pass |
| (d) Data Thread: record the investigation as steps; edit a method step → "is now version 2. 1 step that read it re-ran; 1 earlier verdict is now void"; the re-run finding is not flagged, v1 shows "Why void"; the finding's Pin is disabled with the reason; pin the re-verified method step through an approval decided by the approver; fork "what if", compare side by side, merge into a titled report and open it in Outputs | `03-data-thread` | pass |
| (e) Engineer: file source from an upload; Start work → Prepare data → load a CSV; recipe saved, previewed (nothing written), published; owner allows a writer destination; pipeline saved and published; dry run with the rows ledger (input, output, quarantined, rejected, late "not measured" with its reason); a pending approval is refused, then approve → materialize v1; append rows → second dry run → v2; roll back v2 → serves v1 | `04-engineer` | pass |
| (f) ML: Start work → Predict (no spec yet) → Write a new spec → proposed from the data (baseline locked) → Save, publish and train → experiment with baseline table, consumed holdout and model card → request promotion → approver approves → champion → score approved data through a second approval → "Scored n of m rows" | `05-ml` | pass |
| (g) Work modes: owner reviews and saves "ML off" → Start work says "ml work is not selected for this workspace" and offers no Predict; ML on again → Predict is back | `06-work-modes` | pass |
| (g) Workflow builder (P7-19): two steps from registered agents with a dependency → draft validated → published v1 → run at that version → COMPLETED | `06-work-modes` | pass |
| (h) Reconnect: reload mid-run → stream Live again, events continue; the stream's network cut (browser fault injection on the SSE URL, no mocked answer) → "Reconnecting (attempt n · retry in s …)" → Reconnect now → Live → the run finishes | `07-reconnect-stale` | pass |
| (h) Stale edit: another tab saves the same step first → this tab's save gets 412 → "Someone changed this … nothing was overwritten" → Compare with mine (lists `min_group_size`) → Reload | `07-reconnect-stale` | pass |

Screenshots of every key screen are written to `web/e2e-live/screenshots/` on each run (git-ignored); four small
copies are committed: `key-a4-why-this-number.png`, `key-d2-compare.png`, `key-e2-dry-run-ledger.png`,
`key-h2-stale-edit.png`.

## Bugs the journeys exposed, fixed (each with a regression test)

1. **Editing a step flagged the verified finding that reads it.** A recorded finding re-ran against the method's
   new result and its numbers did not bind: the segment label ("= 1") is text in the result and the rate ratio
   ("3.0x") lives in `stat.highlights`. `src/analystos/services/steps.py` `_number`/`numbers_of` (≈ l.453–478) now
   bind numeric text and the method's headline numbers. Test: `tests/unit/test_selfcheck.py::test_a_recorded_finding_rebinds_to_its_rerun_method_result`.
2. **Merge notice said `with  steps` and showed the generated artifact name.** The API returns `included` as a
   count and the title in `report.content.title`; the UI read `included.length` and `report.name`
   (`web/src/components/DataThread.tsx` ≈ l.556; type in `web/src/api.ts` `MergeResult`; the mock now answers like
   the server). Test: `web/src/test/dataThread.test.tsx` (fork → merge).
3. **Pin was offered on findings the server refuses** ("only a query step or an AnalysisSpec method step can be
   pinned"). `DataThread.tsx` ≈ l.268 now matches `services/step_pins.py` and says to pin the step it reads.
   Test: `dataThread.test.tsx` "a verified claim is not offered a pin the server refuses".
4. **Predict/Forecast were a dead end before the first spec.** The server marks them unavailable until an
   `ml_spec` is published, and the only way to write one was inside the kind's own form. The reason now has its
   own code `no_ml_spec` (`src/analystos/capabilities/job_kinds.py` l.103); Start work opens the ML form (training
   stays disabled, "Write a new spec" available) when that is the only reason (`web/src/lib/jobKinds.ts` l.65,
   `StartWork.tsx` l.69–106). Tests: `web/src/test/lightIa.test.tsx` "Predict without a published spec…",
   `tests/unit/test_ml_work_orders.py`, `tests/unit/test_brief_readiness.py` (code updated).
5. **An improved experiment showed "No model version was registered" and no promotion** once the page re-read
   it: only the training call returned `model_version`. `GET …/ml/experiments[/{id}]` now include the registered
   version (`src/analystos/services/ml.py` `registered_versions` l.409; `api/routers/ml.py` l.86, l.94). Test:
   `tests/unit/test_ml_work_orders.py::test_a_read_of_an_improved_experiment_shows_its_registered_version`.
6. **Scoring defaulted to a fixed demo table** (`stg_sn.incident`), refused as out of scope. The form now
   defaults to the champion's training table (`web/src/components/Ml.tsx` ≈ l.590). Test: `web/src/test/ml.test.tsx`
   "the scoring form defaults to the table the champion was trained on".
7. **No way to create a pipeline in the UI.** The panel said "Publish the pipeline first" with no Publish button,
   and offered no way to save a PipelineSpec or allow a writer destination. Added (existing API calls, no new
   routes): Publish on a draft, "Advanced: new pipeline from a specification", and an owner-only "Destinations
   the writer may use" (`web/src/components/Pipelines.tsx` l.207, l.283, l.314; `api.designateDestination`).
   Test: `web/src/test/pipelines.test.tsx` "creating a pipeline in the UI".
8. **Appending a CSV with whole-number amounts to a `double precision` column was refused.** A lossless widening
   (int → larger int, int → numeric/double, real → double) is now accepted; a narrowing or a change of kind is
   still refused with the column named (`src/analystos/staging/loader.py` `_WIDENS` l.501). Test:
   `tests/unit/test_file_ingest.py::test_append_accepts_a_lossless_widening_and_refuses_a_narrowing`.
9. **Model card names and parameters were garbled by Markdown** (`incident_made_sla` → "incident*made*sla",
   `` `dummy_prior` `` broken): intraword underscores no longer emphasise (`web/src/components/Markdown.tsx` l.11).
   Test: `web/src/test/components.test.tsx` "keeps underscores inside words".

## Observed, not changed

* With `require_approved_metrics` on (the default), an Explain run ends FAILED at `publish_request` after it has
  verified its findings, and the page shows only "required task(s) failed: publish_request"; the worker log has
  the full remedy (approve each proposed KPI, or turn the policy off). The journey sets the policy as the demo
  script does. Surfacing the task's own error on the run page would help a live demo.
* KPI approvals requested before a policy change stay pending but cannot be approved (the card says the policy
  changed); they need a new request.
* On this base, three mocked Playwright journeys and one vitest fail before and after these changes (Definitions
  now opens on "Data model", and the approval inbox's diff is collapsed); both are already fixed on the main
  branch (`744f8df`, design pass `fc39244`).

## Test counts (this worktree, 2026-09-27)

* Live suite: 13 passed × 2 consecutive clean executions (plus 2 earlier executions that found the bugs above);
  13 skipped without `LIVE_BASE_URL`.
* Mocked Playwright (`npx playwright test`): 93 passed, 3 failed (pre-existing, see above).
* Vitest: 293 passed, 1 failed (pre-existing `knowledge.test.tsx`, see above), 1 skipped.
* `npm run typecheck`, `npm run build`: clean. `scripts/export_openapi.py --check`: current (no route changes).
* Backend unit (`pytest -m "not integration"`): 3221 passed, 11 skipped. `ruff check src tests`: clean.
