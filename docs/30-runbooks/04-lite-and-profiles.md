# Runbook 04 — Lite by default, profiles and configuration tiers

Decision: [ADR-0025](../10-architecture/adr/0025-lite-by-default.md) (spec v4 §12a; tracker P7-16, P7-17).
One codebase and one behaviour; the profile only decides which services run and which defaults apply.

## 1. Profiles

| Profile | Containers | How to start |
|---|---|---|
| `lite` (default) | Postgres (pgvector), `api`, `web` | `docker compose up -d --build` |
| `standard` | lite + Redis, Temporal, `worker` (every queue), `scheduler` | `docker compose --env-file .env --env-file deploy/compose/standard.env up -d --build` |
| `scale` | standard with one worker pool per queue | `docker compose --env-file .env --env-file deploy/compose/scale.env up -d --build` |

Optional features, on top of any profile (name every profile on the command line: a `--profile` flag
replaces `COMPOSE_PROFILES` from the env files):

| Feature | Adds | How |
|---|---|---|
| `bi` | Superset; publications may target it (preview stays available) | `--env-file deploy/compose/bi.env --profile bi` |
| `graph` | Neo4j projection (lineage is served from Postgres without it) | `--profile graph`, `ANALYSTOS_GRAPH_ENABLED=true` |
| `demo` | the ServiceNow mock source, in its own small image | `--env-file deploy/compose/demo.env --profile demo` |
| `sandbox` | the container sandbox image (build only) | `docker compose --profile sandbox build sandbox` |
| `pooled` | PgBouncer | runbook 02 "Connection pooling" |

The CI stack (everything): `docker compose --env-file .env --env-file deploy/compose/standard.env
--env-file deploy/compose/bi.env --env-file deploy/compose/demo.env --profile standard --profile bi --profile demo up -d`.
Naming services still works without a profile flag (`docker compose up -d postgres redis temporal superset`).

### What `lite` runs inside the API process

* **Local orchestrator** (`workflows/orchestrator.py::LocalRuntime`): the Temporal workflow's control loop.
  * **Resume:** on startup every non-terminal run is re-driven from its Postgres task state. Claims the
    previous process held are released first; a late result of a dead attempt is `superseded`.
  * **No thread while waiting:** a run in `WAITING_USER` (or paused) parks. The approval signal
    (`POST /api/approvals/{id}/approve`), a control or feedback re-drives it, and there is no wall-clock cap.
  * **Sweep:** every `ANALYSTOS_LOCAL_SWEEP_SECONDS` a sweep re-drives parked runs whose state changed
    without a signal, for example an approval decided by another process.
  * **Bounded parallelism:** ready steps of all runs execute on `ANALYSTOS_LOCAL_WORKERS` threads.
* **In-process scheduler:** the schedule and monitor loop runs as a thread of the API. Claim-then-execute
  keeps it safe next to a separate `analystos scheduler`.
* **Spend caps in Postgres:** with no `ANALYSTOS_REDIS_URL`, hard cap reservations lock the
  `spend_counter` rows (migration 0035).
  * They are atomic and still fail closed: a database error refuses the billable call.
  * The store is chosen by configuration (`ANALYSTOS_SPEND_COUNTER_STORE`), never switched at runtime. A
    Redis outage in `standard` refuses calls, as before.
* **Preview publishing:** without a Superset URL, publications target the preview destination with no
  connection attempt, and `tool.superset_publish` is listed as unavailable with the reason.

Durability in `lite` is "resume from Postgres after a restart", not Temporal's timers and history, and
it assumes **one API process** drives local runs (resume releases the claims it finds). Move to
`standard` for more than one API replica or for durable timers.

### Unavailable, with the reason

A capability manifest may require an installation feature: `requires: [profile:bi]` or `requires: [extra:ml]`.

* `GET /api/capabilities` returns `available` and `unavailable_reason` for each capability.
* Binding and invocation refuse an unavailable capability, with the reason (`capabilities/enablement.usable`).
* `GET /api/health` shows `installation` (the profile, its features and the installed extras).
* Built-in declarations today:
  * `tool.superset_publish` needs `profile:bi`.
  * `method.driver_model`, `skill.logistic_regression` and `skill.feature_importance` need `extra:ml`.
  * Holt-Winters forecasts fall back to drift, with a stated warning.
  * PDF and XLSX reports need `extra:reports`. Markdown and HTML reports need nothing.

## 2. Images and extras (P7-17)

| Extra | Libraries | Needed by |
|---|---|---|
| core | FastAPI, SQLAlchemy/psycopg, numpy, scipy, duckdb, pandas, polars, pyarrow, redis client | every method of the investigate playbook (grouped logistic, Mantel-Haenszel and Durbin-Watson are numpy) |
| `reports` | matplotlib, fpdf2, openpyxl | PDF/XLSX reports |
| `ml` | scikit-learn, statsmodels | driver model; Holt-Winters |
| `temporal` | temporalio | `standard`/`scale` API and `analystos worker` |
| `graph` | neo4j | the Neo4j projection |
| `standard` | all four | worker and scheduler images |

`dev` pulls every extra, so the test suite exercises everything.

Images (`deploy/docker/Dockerfile`, build argument `EXTRAS`):

* the lite API image is `EXTRAS=reports`;
* the standard API image is `EXTRAS=reports,temporal`;
* the worker image is `EXTRAS=standard`;
* the mock has its own image (`deploy/docker/Dockerfile.mock`: FastAPI, numpy, pyarrow and two modules).

Measured 2026-09-26, with `uv` installing into empty Python 3.11 virtualenvs. Docker was not available,
so these are the installed site-packages sizes, not image sizes:

| Dependency set | site-packages |
|---|---|
| before (every library in core) | 900 MB |
| core only | 680 MB |
| core + `reports` (the lite API image) | 765 MB (with pytest) |
| mock image deps | 229 MB (with httpx) |

The API's import cold start (`import analystos.api.app`, median of 3, shared CPU):

* before, with every library installed: ~2.0 s;
* a core-only environment: ~1.3 s.

No ML, Temporal, Neo4j or report library is imported at API start in either case.

The largest remaining core libraries are polars (172 MB, used only by the CSV/Parquet connector) and
pyarrow (152 MB).

## 3. Helm: `values-small.yaml`

`helm install analystos deploy/helm/analystos -f deploy/helm/analystos/values-small.yaml`:

* one API (with the in-process scheduler), one worker on every queue and one web pod;
* no PDBs and no HPAs;
* total requests of 0.8 CPU and 1.8 GiB, so it fits a 2-CPU / 4 GiB node (`tests/unit/test_helm_chart.py`);
* Superset is off, so publishing uses preview;
* Temporal is still needed; for no Temporal use the lite profile with Docker Compose.

The chart runs one image for every component, so build it with `EXTRAS=standard`.

## 4. Environment variables in three tiers

**Tier 1, required (3).** A fresh install needs only these:

* `ANALYSTOS_DATABASE_URL`;
* `ANALYSTOS_JWT_SECRET`;
* `ANALYSTOS_BOOTSTRAP_ADMIN_PASSWORD`.

Compose sets its own database URL. On a laptop, development defaults stand in for the other two.

**Tier 2, optional, by feature.**

| Feature | Variables |
|---|---|
| Profile | `ANALYSTOS_PROFILE`, `ANALYSTOS_ENV`, `ANALYSTOS_BOOTSTRAP_ADMIN_EMAIL` |
| Models | `OPENROUTER_API_KEY`, `ANALYSTOS_MODELS_CONFIG`, `ANALYSTOS_AIR_GAPPED` |
| standard / scale | `ANALYSTOS_ORCHESTRATOR`, `ANALYSTOS_REDIS_URL`, `ANALYSTOS_TEMPORAL_ADDRESS`, `ANALYSTOS_TEMPORAL_NAMESPACE`, `ANALYSTOS_TEMPORAL_QUEUE_PREFIX`, `ANALYSTOS_WORKER_QUEUES` |
| bi | `ANALYSTOS_SUPERSET_URL`, `ANALYSTOS_SUPERSET_PUBLIC_URL`, `ANALYSTOS_SUPERSET_USERNAME`, `ANALYSTOS_SUPERSET_PASSWORD`, `ANALYSTOS_SUPERSET_ANALYTICS_SQLALCHEMY_URI`, `ANALYSTOS_ANALYTICS_BI_ROLE_PREFIX`, `ANALYSTOS_ANALYTICS_BI_SECRET` |
| graph | `ANALYSTOS_GRAPH_ENABLED`, `ANALYSTOS_NEO4J_URI`, `ANALYSTOS_NEO4J_USER`, `ANALYSTOS_NEO4J_PASSWORD` |
| demo | `ANALYSTOS_SERVICENOW_MOCK_URL`, `SERVICENOW_PASSWORD` |
| Data plane | `ANALYSTOS_ANALYTICS_READER_URL`, `ANALYSTOS_ANALYTICS_LOADER_URL`, `ANALYSTOS_ANALYTICS_BUILDER_URL`, `ANALYSTOS_UPLOAD_DIR`, `ANALYSTOS_ARTIFACT_DIR` |
| ELT builds | `ANALYSTOS_DBT_EXECUTABLE`, `ANALYSTOS_BUILD_DIR`, `ANALYSTOS_BUILD_TIMEOUT_SECONDS` |
| SSO | `ANALYSTOS_OIDC_ISSUER`, `ANALYSTOS_OIDC_CLIENT_ID`, `ANALYSTOS_OIDC_CLIENT_SECRET`, `ANALYSTOS_OIDC_REDIRECT_URI`, `ANALYSTOS_OIDC_SCOPES`, `ANALYSTOS_OIDC_GROUPS_CLAIM`, `ANALYSTOS_OIDC_MAPPING_FILE`, `ANALYSTOS_OIDC_JWKS_FILE`, `ANALYSTOS_OIDC_DISCOVERY_URL`, `ANALYSTOS_OIDC_PROVIDER_NAME`, `ANALYSTOS_PASSWORD_LOGIN` |
| Web | `ANALYSTOS_WEB_URL`, `ANALYSTOS_CORS_ORIGINS` |
| Knowledge embeddings | `ANALYSTOS_KNOWLEDGE_EMBEDDING_PROVIDER`, `ANALYSTOS_KNOWLEDGE_EMBEDDING_MODEL`, `ANALYSTOS_KNOWLEDGE_EMBEDDING_DIM`, `ANALYSTOS_KNOWLEDGE_EMBEDDING_ALLOW_DOWNLOAD` |

**Tier 3, advanced.** Defaults are right for almost every install.

| Area | Variables |
|---|---|
| Local orchestrator (lite) | `ANALYSTOS_LOCAL_WORKERS`, `ANALYSTOS_LOCAL_RESUME`, `ANALYSTOS_LOCAL_SWEEP_SECONDS`, `ANALYSTOS_INPROCESS_SCHEDULER` |
| Spend caps and counters | `ANALYSTOS_SPEND_COUNTER_STORE`, `ANALYSTOS_BUDGET_COUNTER_PREFIX` |
| Connection pools | `ANALYSTOS_DB_POOL_MODE`, `ANALYSTOS_DB_POOL_SIZE`, `ANALYSTOS_DB_MAX_OVERFLOW`, `ANALYSTOS_DB_POOL_TIMEOUT`, `ANALYSTOS_DB_POOL_RECYCLE`, `ANALYSTOS_ANALYTICS_POOL_MODE`, `ANALYSTOS_ANALYTICS_POOL_SIZE`, `ANALYSTOS_ANALYTICS_MAX_OVERFLOW`, `ANALYSTOS_ANALYTICS_POOL_TIMEOUT`, `ANALYSTOS_LOADER_POOL_MODE`, `ANALYSTOS_LOADER_POOL_SIZE`, `ANALYSTOS_LOADER_MAX_OVERFLOW`, `ANALYSTOS_LOADER_POOL_TIMEOUT`, `ANALYSTOS_DB_TRANSACTION_POOLER` |
| Analytics roles | `ANALYSTOS_ANALYTICS_WORKSPACE_ROLE_PREFIX`, `ANALYSTOS_ANALYTICS_BUILD_ROLE_PREFIX` |
| Gateway | `ANALYSTOS_QUERY_TIMEOUT_SECONDS`, `ANALYSTOS_QUERY_MAX_ROWS`, `ANALYSTOS_QUERY_CACHE_TTL_SECONDS` |
| Sandbox | `ANALYSTOS_SANDBOX_ISOLATION`, `ANALYSTOS_SANDBOX_TIMEOUT_SECONDS`, `ANALYSTOS_SANDBOX_MEMORY_MB`, `ANALYSTOS_SANDBOX_CONTAINER_IMAGE`, `ANALYSTOS_SANDBOX_CONTAINER_PYTHON`, `ANALYSTOS_SANDBOX_CONTAINER_RUNTIME`, `ANALYSTOS_SANDBOX_CONTAINER_STARTUP_SECONDS`, `ANALYSTOS_SANDBOX_CPUS`, `ANALYSTOS_SANDBOX_PIDS_LIMIT`, `ANALYSTOS_SANDBOX_SCRATCH_MB`, `ANALYSTOS_SANDBOX_MASKED_PATHS` |
| Auth | `ANALYSTOS_JWT_TTL_MINUTES` |
| Catalogs | `ANALYSTOS_AGENTS_DIR` |

`tests/unit/test_lite_profile.py` checks that every setting in `core/config.py` is listed here.
