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
npm test             # vitest (jsdom): parser, chart builders, tree, components
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
| Sources & data explorer | `/w/:ws/sources` | `POST …/sources` (servicenow / postgres with `secret_ref`; csv via `POST …/uploads` then `config.path`), `POST …/sources/{id}/discover`, `PUT …/sources/{id}/selection`, `GET …/assets`, `PUT /api/assets/{asset}/columns/{col}/tags`, `GET …/relationships` |
| Analysis runs | `/w/:ws/runs` | `GET …/analysis` |
| Run view | `/w/:ws/runs/:run` | `GET …/analysis/{run}`; live **SSE** `GET …/analysis/{run}/events` (fetch + ReadableStream with a bearer header; reconnects with `?after_id=`); `POST …/analysis/{run}/pause|resume|cancel`; `POST …/feedback`; `POST /api/approvals/{id}/approve|reject`; `GET /api/agent-runs/{task}` (task details); `POST /api/publications/{id}/rollback` |
| Agent console | `/w/:ws/runs/:run/console` | `GET …/analysis/{run}/console` (messages, tool calls, model calls with **JEV decision** highlight for `provider == "typesafe"`, queries, cost) |
| Insights | `/w/:ws/insights/:id` | `GET …/insights`, `GET /api/insights/{id}` (REV record, queries with SQL + result preview, experiments, lineage) |
| Studio | `/w/:ws/studio?artifact=` | `GET …/artifacts`, `GET /api/artifacts/{id}` (versions + lineage); dashboard preview uses `GET …/artifacts?type=chart&run_id=` for chart previews |
| Ask | `/w/:ws/ask` | `POST …/ask` (SQL, explanation, repair attempts, result, chart hint), `POST …/query` (SQL console; `sql_rejected` shown as a gateway rejection) |
| Policy & members | `/w/:ws/governance` | `PUT …/policy` (JSON editor), `PATCH /api/workspaces/{id}` (objective/autonomy), `POST/DELETE …/members`, `GET …/audit` (owners) |
| Admin & registry | `/admin` | `GET/PATCH /api/agents`, `GET/PATCH /api/tools`, `GET /api/skills`, `GET /api/admin/models`, `GET /api/admin/usage`, `GET /api/admin/audit` |

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
  lib/status.ts         one status -> tone map used by every badge; autonomy level texts
  components/           Layout, ui primitives, Chart, DashboardPreview, InvestigationTree,
                        ApprovalsPanel, LineageGraph, Markdown (safe subset), ErrorBoundary
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

- There is no user-management screen. `POST /api/users` exists, but the spec does not require one here.
- Hypothesis editing (`PATCH /api/hypotheses/{id}`) is in the client but has no UI yet.
  Feedback and redirect cover the steering flow.
- Context/glossary endpoints (`/api/workspaces/{id}/context`, `/api/context/search`) are not surfaced.
- The dashboard preview renders chart previews as the agents computed them. Native filters are shown
  but do not re-query.
- Publication rollback is available from the run summary, using `summary.publication.publication_id`.
  There is no separate publications list endpoint.
