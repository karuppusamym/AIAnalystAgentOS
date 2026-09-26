# Context2AI AnalystOS

**An autonomous, governed data & analytics agent operating system.** Point it at governed
enterprise data, state a business problem, and a team of agents profiles the data, forms and
tests hypotheses, verifies what it finds, defines KPIs, designs executive and operational
dashboards, and — after a person approves — publishes them to Superset, with lineage from every
tile back to the query and table it came from.

> Models propose. Deterministic code computes, checks and enforces. People approve side effects.

| | |
|---|---|
| Product vision (v1) | [`Context2AI_AnalystOS_Complete_Spec.md`](Context2AI_AnalystOS_Complete_Spec.md) |
| Engineering spec (v2) | [`docs/00-intent/02-spec-v2.md`](docs/00-intent/02-spec-v2.md) |
| Workspace / engineering / ML target | [`docs/00-intent/03-workspace-data-team-spec.md`](docs/00-intent/03-workspace-data-team-spec.md) (design, not shipped) |
| Design review and priorities | [`docs/60-delivery/04-design-review.md`](docs/60-delivery/04-design-review.md) |
| Workbench UI / API design | [UX](docs/10-architecture/02-workbench-ux.md) · [API](docs/20-contracts/02-workbench-api.md) |
| Platform spec (v3, proposed) | [`docs/00-intent/03-spec-v3-platform.md`](docs/00-intent/03-spec-v3-platform.md): capabilities, deterministic-first ladder, open knowledge formats, ELT on your engines |
| Unified platform spec (v4, proposed) | [`docs/00-intent/04-spec-v4-unified-data-platform.md`](docs/00-intent/04-spec-v4-unified-data-platform.md): one platform for analyst, data-science, data-engineering and governed ML work; Atlas and DataPilot as donors |
| Latest reviews | [`docs/70-reviews/2026-09-26-agent-os-comparison-review.md`](docs/70-reviews/2026-09-26-agent-os-comparison-review.md) (comparison, dispositions) · [`docs/70-reviews/2026-09-25-architecture-review.md`](docs/70-reviews/2026-09-25-architecture-review.md) |
| Agent mapping and workspace lifecycle | [`docs/30-operations/workspace-lifecycle.md`](docs/30-operations/workspace-lifecycle.md) |
| Architecture + ADRs | [`docs/10-architecture/`](docs/10-architecture/01-architecture.md) |
| Status (tracker) · evidence · readiness | [`docs/60-delivery/`](docs/60-delivery/01-tracker.md) |
| Working agreement for AI sessions | [`CLAUDE.md`](CLAUDE.md) |

## What happens in a run

```
context → metadata → relationships → profile → data quality
      → hypotheses (LLM, closed vocabulary, JEV-prioritised) → test:H-1..n (SQL pushdown + statistics)
      → follow-up rounds → insights (BH-corrected, numbers-guarded) → REV verification
      → reusable dataset → validated KPIs → charts + executive/operational dashboards
      → governance review → approval (bound to bundle hash + plan hash) → Superset → lineage graph
```

You can pause, resume, cancel, redirect ("exclude inquiry incidents"), reject a finding, or
approve/reject publication at any point; redirects replan and invalidate stale approvals.

## Key properties

* **Governed data access.** One SQL gateway for every path. Statements are parsed and checked
  against the caller's server-resolved scope (selected tables only; restricted/PII columns denied
  even through `*`, aliases and CTEs), run read-only with timeouts and row caps, cached by scope,
  and audited. The query identity cannot reach the control-plane database.
* **Reproducible findings.** Hypotheses compile from a closed analysis vocabulary to SQL; every
  number comes from SciPy/statsmodels/scikit-learn; findings are verified by re-running evidence
  queries (hash match) and an independent second method.
* **Model routing with JEV.** Purpose-based routing over OpenRouter with fallback, budgets and
  redaction; **TypeSafe Jev** decision model for typed, probability-scored choices (priority, risk
  escalation, feedback classification, chart choice, verification second opinion, stop check).
* **Durable execution.** Temporal workflows over idempotent, plan-versioned tasks in Postgres.
* **Full provenance.** Queries, experiments, artifacts (versioned), approvals, publications,
  model/tool calls; lineage mirrored to Neo4j.

## Quick start

```bash
cp .env.example .env              # add OPENROUTER_API_KEY — never commit .env
uv venv -p 3.11 .venv && uv pip install -e ".[dev]"
docker compose up -d postgres redis neo4j temporal superset
.venv/bin/analystos migrate && .venv/bin/analystos seed
scripts/dev_up.sh                 # ServiceNow mock :8090, Temporal worker, API :8000
(cd web && npm install && npm run dev)   # UI :5173 — admin@analystos.local / ChangeMe123!
```

Prove the MVP end to end (writes a dated evidence report):

```bash
.venv/bin/python scripts/e2e_demo.py
```

Tests: `.venv/bin/pytest -m "not integration"` (no services) · `.venv/bin/pytest -m integration`
(compose stack) · `cd web && npm test`.

## Stack

Python 3.11 · FastAPI · SQLAlchemy/Alembic · PostgreSQL 16 + pgvector · Redis · Neo4j 5 ·
Temporal · sqlglot · DuckDB/Polars · SciPy/statsmodels/scikit-learn · Apache Superset 4.1 ·
OpenRouter (chat + Decisions API) · React 18 + Vite + ECharts.

## Status

Phase 0 + Phase 1 (MVP) of the spec, at **prototype** readiness: synthetic ServiceNow-shaped
data, read-only sources, local identity. See [`docs/60-delivery/03-release-readiness.md`](docs/60-delivery/03-release-readiness.md)
for what a controlled pilot still needs (SSO, a real ServiceNow instance for connector
certification, isolated sandbox containers, measured SLOs).
