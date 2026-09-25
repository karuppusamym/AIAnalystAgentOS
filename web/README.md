# AnalystOS web UI

React 18 + TypeScript + Vite single-page app for **Context2AI AnalystOS**. Every screen talks to
the real FastAPI backend (`src/analystos/api/routers/*.py`). There are no mocks in the app itself.

## Run

```bash
cd web
npm install
npm run dev          # http://localhost:5173, proxies /api -> http://localhost:8000
                     # (override with ANALYSTOS_API_URL=http://host:port npm run dev)
npm run typecheck    # tsc --noEmit
npm test             # vitest (jsdom): parser, chart builders, tree, components, schedules/monitoring/reports,
                     # sources/crawls/catalog/explain/admin settings (increment3.test.tsx)
npm run build        # typecheck + production bundle in dist/
```

Seeded users (development only): `admin@analystos.local`, `analyst@analystos.local`,
`approver@analystos.local`, all with password `ChangeMe123!`.

Optional live smoke test, which renders every screen against a running API:

```bash
ANALYSTOS_LIVE_API=http://localhost:8000 npx vitest run src/test/live.smoke.test.tsx
```

### Docker

`Dockerfile` does a multi-stage build (node:22 build, then nginx). `nginx.conf` serves `dist/` with
SPA fallback and proxies `/api/` to `http://api:8000` (the compose service). Proxying is
SSE-friendly: `proxy_buffering off`, no cache, and a 1 h read timeout. It also allows 210 MB request
bodies for uploads. `compose.yaml` publishes the app on port 5173 (`web` service).

## Screens → endpoints

| Screen | Route | Endpoints |
|---|---|---|
| Login | `/login` | `POST /api/auth/login`, `GET /api/auth/me` (validates a stored token) |
| Workspaces | `/` | `GET/POST /api/workspaces` (name, description, objective, autonomy 0–4 with explanations) |
| Workspace home | `/w/:ws` | `GET /api/workspaces/{id}` (counts, role, policy, members), `…/sources`, `…/analysis`, `…/insights`, `…/artifacts`; **Start analysis** → `POST …/analysis {objective, source_ids?, autonomy_level?}` |
| Sources & data explorer | `/w/:ws/sources` | `GET /api/source-kinds` (Add-source form generated per kind: grouped by category, required/optional fields, `secret_ref` only, driver/install hint, pushdown vs staged, admin-disabled kinds greyed out), `POST …/sources` (file kinds may upload via `POST …/uploads` then `config.path`), `POST …/sources/{id}/discover`, `PUT …/sources/{id}/selection`, `GET …/assets`, `PUT /api/assets/{asset}/columns/{col}/tags`, `GET …/relationships`; per source **Crawl** `POST …/sources/{id}/crawl {mode, include, exclude, profile?, enrich?}`, history `GET …/crawls?source_id=`, running crawls polled every 2 s via `GET /api/crawls/{id}`; detail shows schema drift (new / changed with added, removed, retyped columns / missing / deprecated / rename candidates) and the stage log |
| Catalog | `/w/:ws/catalog` | `GET …/catalog?q=&domain=&role=&include_deprecated=` (role, domain, grain, confidence, description with origin badge source/rule/model/user, reviewed, lifecycle; expandable columns with role, unit, tags + `tags_origin`, PII category, glossary term), `PATCH /api/assets/{id}/metadata` (editors/owners: business name, description, reviewed) |
| Analysis runs | `/w/:ws/runs` | `GET …/analysis` |
| Run view | `/w/:ws/runs/:run` | `GET …/analysis/{run}` (incl. `origin` badge: scheduled / alert investigation, and a **What changed since the previous run** panel from `summary.changes` with KPI deltas and a link to `summary.report_artifact_id`); live **SSE** `GET …/analysis/{run}/events` (fetch + ReadableStream with a bearer header; reconnects with `?after_id=`); `POST …/analysis/{run}/pause|resume|cancel`; `POST …/feedback`; `POST /api/approvals/{id}/approve|reject`; `GET /api/agent-runs/{task}` (task details); `POST /api/publications/{id}/rollback` |
| Agent console | `/w/:ws/runs/:run/console` | `GET …/analysis/{run}/console` (messages, tool calls, model calls with **JEV decision** highlight for `provider == "typesafe"`, queries, cost) |
| Insights | `/w/:ws/insights/:id` | `GET …/insights`, `GET /api/insights/{id}` (REV record, queries with SQL + result preview, experiments, lineage) |
| Studio | `/w/:ws/studio?artifact=` | `GET …/artifacts`, `GET /api/artifacts/{id}` (versions + lineage); dashboard preview uses `GET …/artifacts?type=chart&run_id=` for chart previews |
| Schedules | `/w/:ws/schedules?schedule=` | `GET/POST …/schedules` (list includes `recent_runs`), `PATCH/DELETE /api/schedules/{id}` (edit, enable toggle, delete), `POST /api/schedules/{id}/run` (run now). Form: kind-specific config (reanalysis / dataset_refresh / report / monitor / crawl: sources, mode, include/exclude), cron presets (weekly Mon 07:00, daily 06:00, monthly 1st 07:00) plus raw cron, IANA time zone; next run shown in the schedule's zone and local time; server validation errors (e.g. < 15 min interval) shown verbatim |
| Monitoring | `/w/:ws/monitoring?tab=monitors\|alerts&alert=` | `GET/POST …/monitors` (kinds: threshold, drift, change point, forecast deviation {z, history, seasonal_periods?}, data quality), `PATCH /api/monitors/{id}` (enabled, auto_investigate), `POST /api/monitors/{id}/evaluate`, `GET /api/monitors/{id}/series` (line chart, latest point marked, drift baseline median / threshold line), `GET …/artifacts?type=metric` (metric picker), `GET …/assets` (data-quality assets); `GET …/alerts?status=`, `POST /api/alerts/{id}/acknowledge\|resolve\|investigate` (JEV triage `data.triage.p_material` shown) |
| Reports | `/w/:ws/reports?artifact=` | `GET …/artifacts?type=report`, `POST …/reports {run_id?, kind, formats}`, `GET /api/artifacts/{id}/download?format=` (fetch with bearer → blob → object URL; HTML **Preview** renders in `<iframe sandbox="" srcdoc>`, never in the page DOM) |
| Notifications (top bar) | all pages | `GET /api/notifications?unread=true` polled every 30 s for the badge, `GET /api/notifications` when the dropdown opens, `POST /api/notifications/read {ids}`; a click follows `link {type, id}` (alert → Monitoring, report artifact → Reports, other artifact → Studio, run → Run view, schedule → Schedules) |
| Ask | `/w/:ws/ask` | `POST …/ask` (SQL, explanation, repair attempts, result, chart hint), `POST …/query` (SQL console; `sql_rejected` shown as a gateway rejection), **Explain** `POST …/query/explain` (deterministic explanation + gateway verdict, nothing executed) |
| Policy & members | `/w/:ws/governance` | `PUT …/policy` (JSON editor), `PATCH /api/workspaces/{id}` (objective/autonomy), `POST/DELETE …/members`, `GET …/audit` (owners) |
| Admin & registry | `/admin` | `GET/PATCH /api/agents`, `GET/PATCH /api/tools`, `GET /api/skills`, `GET /api/admin/models` (incl. effective mode per purpose), `GET /api/admin/usage`, `GET /api/admin/audit`; admin-only **Settings** `GET/PUT /api/admin/settings` (per-purpose off/auto/always with the rule-path flag, feature flags, numeric limits with schema ranges, enabled source kinds, disabled models, cacheable purposes; a note is required; `purpose_modes` is always sent as the complete map because the server replaces it wholesale), `POST /api/admin/settings/preset` (confirmed), `GET /api/admin/settings/history` + `POST …/rollback`; **Token savings** `GET /api/admin/token-savings?days=`; **Prompts** `GET /api/admin/prompts` |

## Layout

```
src/
  api.ts                typed client, ApiError, token store, SSE subscription (subscribeRunEvents)
  auth.tsx              auth context (401 anywhere -> logout)
  App.tsx, main.tsx     routes
  lib/sse.ts            text/event-stream parser + fetch reader
  lib/charts.ts         ECharts option builders per chart_type (kpi, line, bar, histogram, heatmap,
                        table, pie, treemap, stacked_bar, scatter); light/dark palettes
  lib/tree.ts           Question -> Hypotheses (nested follow-ups) -> Findings
  lib/lineage.ts        layered lineage (upstream -> downstream)
  lib/status.ts         one status -> tone map used by every badge (+ alert severity tones); autonomy level texts
  lib/schedules.ts      cron presets/description/validation, schedule form -> kind-specific config, zone formatting
  lib/monitors.ts       monitor form -> config (incl. forecast_deviation), chart overlays (drift baseline, threshold)
  lib/sourceKinds.ts    source-kind catalog -> add-source form (grouping, typed config, validation)
  lib/crawls.ts         crawl form -> body, stats/drift summaries, stage progress
  lib/settings.ts       admin settings diff -> PUT patch (replaced maps sent whole), limits validation, preset effect
  lib/notifications.ts  notification link -> route
  components/           Layout, ui primitives, Chart, DashboardPreview, InvestigationTree,
                        ApprovalsPanel, LineageGraph, Markdown (safe subset), ErrorBoundary,
                        NotificationBell, ChangesPanel, CrawlPanel
  pages/                one file per screen
  test/                 vitest suites
```

Design notes: CSS variables with separately tuned light and dark themes (`prefers-color-scheme`).
Every status value uses the same colour everywhere: green = done/verified, blue = running,
amber = waiting/pending, red = failed/rejected, grey = inert/superseded. JEV decision calls are
violet. The layout is responsive: the nav becomes a drawer under 860 px and the dashboard grid
stacks under 760 px. Charts fall back to a table view when canvas is unavailable, and each chart
has a "Table view" disclosure.

## Known gaps

The next interaction design is [Workspace workbench](../docs/10-architecture/02-workbench-ux.md),
with implementation tracked under P4-07, P5-03 and P6-03. It is a target design; the screen map
above continues to describe the shipped UI.

- There is no user-management screen. `POST /api/users` exists, but the spec does not require one here.
- Hypothesis editing (`PATCH /api/hypotheses/{id}`) is in the client but has no UI yet.
  Feedback and redirect cover the steering flow.
- Context/glossary endpoints (`/api/workspaces/{id}/context`, `/api/context/search`) are not surfaced.
- The dashboard preview renders chart previews as the agents computed them. Native filters are shown
  but do not re-query.
- Schedules: `dataset_refresh`/`reanalysis` `source_ids` can only be chosen for dataset refresh; re-analysis
  uses the workspace sources. `baseline_run_id` (set by the backend) is shown only indirectly via "previous run".
  Schedule ownership is not displayed (the list returns `owner_id` only).
- Catalog: the glossary term is shown (with its match reason) but not linked, because there is no glossary screen yet.
  Column descriptions and business names are read-only in the UI (the curation endpoint covers tables only).
- Admin settings: `routing_overrides` and `profile_models` are shown but edited only through the API.
- Monitors: name/config cannot be edited after creation in the UI (only enable / auto-investigate);
  `sql_expression` monitors and `dataset_artifact_id` are API-only. Data-quality monitors have no chart.
- Notifications are polled (30 s), not pushed; there is no "load more" beyond the API's 100 most recent.
- Publication rollback is available from the run summary, using `summary.publication.publication_id`.
  There is no separate publications list endpoint.
