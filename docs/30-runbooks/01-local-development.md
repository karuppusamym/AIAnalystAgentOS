# Runbook — local development

## Prerequisites
Docker (with compose), Python 3.11 + [uv](https://github.com/astral-sh/uv), Node 22.

## Boot
```bash
cp .env.example .env                       # set OPENROUTER_API_KEY (never commit .env)
uv venv -p 3.11 .venv && uv pip install -e ".[dev]"
docker compose up -d postgres redis neo4j temporal superset
.venv/bin/analystos migrate && .venv/bin/analystos seed
set -a; . ./.env; set +a
export SERVICENOW_PASSWORD=admin           # the mock's password, referenced as env:SERVICENOW_PASSWORD
.venv/bin/uvicorn analystos.connectors.servicenow_mock:app --port 8090 &
.venv/bin/analystos worker &               # Temporal worker (or ANALYSTOS_ORCHESTRATOR=local for the API)
.venv/bin/uvicorn analystos.api.app:app --port 8000 &
(cd web && npm install && npm run dev)     # http://localhost:5173
```
Full containerised stack: `docker compose up -d --build` (API :8000, UI :5173, Superset :8088 admin/admin).

## Tests
| Command | Needs |
|---|---|
| `.venv/bin/pytest -q -m "not integration"` | nothing |
| `.venv/bin/pytest -q -m integration` | compose Postgres/Redis/Superset (tests skip if absent) |
| `cd web && npm run typecheck && npm test && npm run build` | Node |
| `.venv/bin/python scripts/e2e_demo.py` | full stack + OPENROUTER_API_KEY |

## Troubleshooting
* **Superset exits with `No module named psycopg2`** — you are running the upstream image; use
  the compose `build:` (`deploy/superset/Dockerfile`).
* **`/api/health` shows `models.chat=false`** — `OPENROUTER_API_KEY` is not in the API/worker
  environment. Runs still complete using labelled deterministic fallbacks.
* **Run stuck in `WAITING_USER`** — a publication approval is pending; approve as a user with
  role `approver` or `owner` (`/api/approvals/{id}/approve`).
* **`sql_rejected` in the console** — the gateway message says exactly which rule fired
  (out-of-scope table, restricted column, write statement, denylisted function...).
