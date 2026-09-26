# AnalystOS — working agreement for Claude sessions on this repository

Context2AI AnalystOS is a governed, autonomous analytics agent OS (Python 3.11 / FastAPI /
Postgres+pgvector / Temporal / Neo4j / Redis / Superset / React). Read
[`docs/00-intent/02-spec-v2.md`](docs/00-intent/02-spec-v2.md) before changing behaviour, and
[`docs/00-intent/03-spec-v3-platform.md`](docs/00-intent/03-spec-v3-platform.md) before starting an
increment-4 (`P4-*`) row, and [`docs/00-intent/04-spec-v4-unified-data-platform.md`](docs/00-intent/04-spec-v4-unified-data-platform.md)
before starting a `P7-*`, `P5-04+` or `P6-04+` row. AnalystOS is the single platform; `AIDataAnalyst` (Atlas)
and `AienginnerAgentOs` (DataPilot) are donor repositories: port behaviour and tests under AnalystOS
contracts, never runtime calls or schemas (ADR-0018). The original product vision is
[`Context2AI_AnalystOS_Complete_Spec.md`](Context2AI_AnalystOS_Complete_Spec.md). Reviews under
`docs/70-reviews/` are dated evidence, not work queues.

## Rules (each one exists because the failure mode is expensive here)

1. **The work queue is the tracker.** [`docs/60-delivery/01-tracker.md`](docs/60-delivery/01-tracker.md)
   is the only status authority (v1 task IDs). Evidence (code path, test, live run, date) goes in
   [`docs/60-delivery/02-capability-register.md`](docs/60-delivery/02-capability-register.md).
   Do not mark a row Done without a test or a dated live-run evidence file.
2. **Never commit secrets.** `OPENROUTER_API_KEY` and source passwords come from the environment
   (`.env` is git-ignored; sources use `secret_ref: env:NAME`). If a key appears in a diff, stop.
3. **Models propose, code decides.** Do not let an LLM output reach SQL execution, authorization,
   approvals, or a published number without passing the deterministic path: `AnalysisSpec`
   validation → `skills/sqlbuild` → `gateway/validator` → skills statistics → numbers guard → REV.
   New model uses go through `llm/router.py` purposes in `config/models.yaml`; JEV (TypeSafe
   decision model) may rank, choose among valid options, or *escalate* risk — never grant.
4. **One gateway.** Every SQL statement from every path (agents, Ask, console, verification,
   dataset, chart preview, metric validation) goes through `QueryGateway.execute`. Never open a
   source connection elsewhere; never use the loader or control-plane identity for queries.
5. **Side effects are approvals bound to hashes.** Anything that writes outside the platform
   (publish, schedule, export, notify) must use `governance/approvals.py` and call
   `verify_for_execution` immediately before acting.
6. **Several sessions may share this branch.** Re-run `git status` and `git log --oneline -3`
   before summarising. Commit only the paths you edited, by name — never `git add -A`.

## Commands

```bash
uv venv -p 3.11 .venv && uv pip install -e ".[dev]"      # deps
docker compose up -d postgres redis temporal superset       # infra (Superset image adds psycopg2); Neo4j is optional:
                                                            # --profile graph + ANALYSTOS_GRAPH_ENABLED=true
.venv/bin/analystos migrate && .venv/bin/analystos seed     # schema + users/registries/glossary
.venv/bin/pytest -q -m "not integration"                    # fast suite (no services)
.venv/bin/pytest -q -m integration                          # needs the compose stack
.venv/bin/ruff check src tests
.venv/bin/uvicorn analystos.api.app:app --reload            # API :8000
.venv/bin/analystos worker [--queues analysis,compute]      # Temporal worker (all queues by default; config/task_queues.yaml)
.venv/bin/analystos scheduler                               # schedules + monitors (claim-then-execute)
.venv/bin/uvicorn analystos.connectors.servicenow_mock:app --port 8090   # demo source
cd web && npm install && npm run dev                        # UI :5173
.venv/bin/python scripts/e2e_demo.py                        # live v1 §62 scenario + evidence report
.venv/bin/python scripts/e2e_phase3.py                      # live Phase-3 (schedules, reports, monitors) evidence
.venv/bin/python scripts/e2e_increment3.py                  # live increment-3 (any database, crawler, token economy) evidence
```

Seeded users (dev only): `admin@analystos.local`, `analyst@…`, `approver@…` / `ChangeMe123!`.

## Map

| Where | What |
|---|---|
| `src/analystos/contracts/` | Pydantic contracts (agent/tool/skill/policy/analysis/bi/events); `analystos export-contracts` writes `contracts/*.json` |
| `src/analystos/runtime/` | plan (hashes), engine (get_state/execute_task/replan: interprets the run's bound playbook, no step keys), per-step RunContext (agent manifest, budget) |
| `src/analystos/agents/` | agent behaviours; `dispatch.py` resolves a task through the playbook + agent manifest; `generic.py` runs declarative agents (propose → validate → execute) |
| `src/analystos/workflows/` | Temporal workflow + activities; `orchestrator.py` local runner |
| `src/analystos/governance/` | scope, policy decisions, approvals, audit |
| `src/analystos/gateway/`, `connectors/`, `staging/` | data plane; `config/source_kinds.yaml` + `connectors/kinds.py` + `generic_sql.py` = every database kind |
| `src/analystos/services/crawler.py`, `skills/catalog.py` | metadata crawler (deterministic semantics/PII/drift; model optional) |
| `src/analystos/contracts/platform.py`, `services/platform_settings.py` | admin control plane: versioned runtime settings, LLM modes, presets |
| `src/analystos/skills/`, `sandbox/` | deterministic analytics |
| `src/analystos/llm/` | router, JEV, redaction; `config/models.yaml` |
| `src/analystos/publishing/` | BI publisher interface, Superset adapter, preview |
| `src/analystos/services/{schedules,monitors,reports,changes,notifications}.py`, `src/analystos/reports/` | Phase 3: scheduling, monitoring, reports |
| `config/agents/*.yaml` | agent manifests (kind Agent); every field is enforced (`capabilities/agents.py`). `entry: builtin:generic` = a YAML-only agent |
| `src/analystos/capabilities/` | registry (discovery, validation, reload), `builtin/playbooks/*.yaml` (`investigate.v1` = the v1 plan), bindings (run keeps its versions; refs in the plan hash), per-workspace enablement + certification gate; API `/api/capabilities`, `POST /api/admin/capabilities/reload` |
| `migrations/` | Alembic; regenerate with `alembic revision --autogenerate` after model changes |
| `docs/` | intent (spec v2, spec v3, spec v4), architecture + ADRs, runbooks, delivery tracker/register/readiness, dated reviews (`70-reviews/`) |

## Conventions

* Match surrounding code: short docstrings that explain *why*, typed signatures, no comment noise.
* Deterministic first: a new model use needs a purpose in `config/models.yaml`, a rule path where one
  can exist (so `off`/`auto` modes work), and `model_gate`/`record_skip` so avoided calls show as savings.
* Crawler invariants: owner tags and reviewed/user descriptions are never overwritten; tags only tighten;
  data-touching crawl steps go through the gateway on selected assets only.
* Errors: raise `core/errors.py` classes (they carry HTTP status and retryability).
* Every new persisted concept needs: model + migration + lineage edge where it has provenance +
  an event type in `contracts/events.py` if the UI should see it.
* Tests: unit tests must not need services; integration tests are marked `integration` and skip
  cleanly when the stack is down. A connector is not "certified" by mock tests (spec v1 §62).
