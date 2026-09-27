# P7-17 core install without extras — evidence, 2026-09-27

What was checked without Docker or a cluster: a core-only `pip install` starts the API with only the
three tier-1 variables and completes a run; every extra stays unimported at start; installed sizes and
API import cost per install set; the compose `lite` profile, the env files, the Dockerfile and the Helm
`values-small.yaml` agree with the extras in `pyproject.toml`.

Machine: 4 vCPUs shared with five other agent sessions (load average 14–18 during the timings), Python
3.11.15, pip 26.2.1. Resolved versions: fastapi 0.141.1, SQLAlchemy 2.1.1, numpy 2.4.6, scipy 1.17.1,
pandas 3.0.6, polars 1.44.2, pyarrow 25.0.1, duckdb 1.5.5.

## 1. Core install and start with three variables

```
uv venv --seed -p 3.11 core-venv
core-venv/bin/python -m pip install -e <repo>            # no extras (the Dockerfile's install form)
env -i PATH=core-venv/bin:/usr/bin:/bin \
  ANALYSTOS_DATABASE_URL=postgresql+psycopg://…/analystos_f6_core \
  ANALYSTOS_JWT_SECRET=… ANALYSTOS_BOOTSTRAP_ADMIN_PASSWORD=… \
  analystos migrate && analystos seed && uvicorn analystos.api.app:app --port 8016
```

* sklearn, statsmodels, temporalio, neo4j, matplotlib, fpdf, openpyxl, mcp and pytest are absent.
* `analystos migrate` reached head 0042; `analystos seed` seeded users, registries and the platform pack.
* First start, before the fix below: `GET /api/health` reported `profile: standard`, `orchestrator:
  temporal`, `features: [bi, demo, standard]` and `extras` all false. The code default `standard` picks
  Temporal, Redis and Superset, but the `temporal` extra is not installed, so no run could start.
  The health checks passed only because this machine runs Redis and Temporal on their default ports.
* Fix (`core/config.py`): when neither `ANALYSTOS_PROFILE` nor `ANALYSTOS_ORCHESTRATOR` is set and
  `temporalio` is not importable, the profile is `lite`. An explicit choice still wins. An install that
  has the extra keeps `standard`, which covers dev and the worker images.
* After the fix: `ok: true, orchestrator: local, installation: {profile: lite, features: [demo],
  extras: {ml: false, reports: false, temporal: false, graph: false}}`. Redis is `not configured (lite
  profile)` and spend counters use `postgres`.
* Through the HTTP API on the core-only process: login → new workspace → 9 capabilities listed
  unavailable with the reason (8 need the `ml` extra, `tool.superset_publish` needs the `bi` profile) →
  upload a 400-row CSV → `csv` source → discover → select → enable `playbook.data_dictionary` → run
  **COMPLETED** in 1.1 s on the local orchestrator, with no Temporal, no Redis and no model key.
  The API's RSS after the run was about 295 MB.

A non-editable `pip install .` installs only `src/analystos`. `REPO_ROOT` then resolves inside
site-packages, so `config/`, `migrations/` and `packs/` are not found. The image and this check use
`pip install -e`. Shipping those directories as package data is not done (see §5).

## 2. Guarded imports

`tests/unit/test_slim_install.py::test_a_core_install_starts_without_any_extra_and_defaults_to_lite`
starts a child interpreter in which every extra's top-level module is missing. It imports
`analystos.api.app`, `analystos.cli`, `analystos.workflows.orchestrator` and
`analystos.services.schedules`, and asserts three things:

* none of those modules, and not polars, is loaded;
* the installation summary reports every extra as false;
* the settings resolve to `lite` / `local`.

`services/file_ingest.py` now imports polars on first use. It was the only module that loaded polars at
API start (`-X importtime`: 0.16–0.28 s cumulative on this machine). pyarrow is still loaded at start by
`connectors/base.py` and the connectors, staging and recipes modules.

## 3. Installed size (site-packages, pip and setuptools included, about 16 MB)

| Install | site-packages |
|---|---|
| core only (`-e .`) | 818 MB |
| core + `reports` (the lite API image) | 920 MB |
| core + `standard` (ml, reports, temporal, graph: the worker image) | 1117 MB |

The largest core packages are the polars runtime (172 MB), pyarrow (156 MB), scipy (113 + 30 MB of
libs), pandas (75 MB), duckdb (58 MB) and numpy (45 + 28 MB). The 2026-09-26 figures (680 / 765 MB,
installed with `uv`) came from older resolved versions. This run resolved newer ones, so compare
install sets within one table, not across the two dates.

## 4. API import cost (`import analystos.api.app`, 7 runs each, same source tree)

| Install | wall median | CPU median (user+sys) | heavy modules loaded |
|---|---|---|---|
| core only | 4.96 s | 2.76 s | pyarrow |
| core + reports | 4.89 s | 2.84 s | pyarrow |
| core + standard | 4.89 s | 2.68 s | pyarrow |

The import cost does not depend on the extras, because none is imported at start. The absolute numbers
reflect a machine at load 14–18 on 4 CPUs. They are not comparable with the 2026-09-26 figures (~1.3 s
core, ~2.0 s with everything), which were taken on a quieter machine.

## 5. Consistency of deploy files with the extras (checked by a test)

`test_image_extras_agree_across_compose_dockerfile_and_helm` checks each of these:

* every `EXTRAS` in `compose.yaml` and every `ANALYSTOS_API_EXTRAS` in `deploy/compose/*.env` is an extra
  in `pyproject.toml`;
* the Dockerfile default `ARG EXTRAS=reports` equals compose's lite API default;
* the lite API has no temporal, ml or graph extra, and compose defaults `ANALYSTOS_PROFILE` to `lite`;
* every env file that selects `ANALYSTOS_ORCHESTRATOR=temporal` builds its API with `temporal`;
* the worker, scheduler and scale pools carry `standard`, `compute-ml` carries `ml`+`temporal`, and
  `compute-py` has no `ml`;
* Helm `values-small.yaml` runs `standard` with Temporal, one `all` worker and no isolated pools, and its
  header now states the one image is built with `EXTRAS=standard`.

## Not verified here (needs Docker or a cluster)

* Docker image sizes and container cold start, before and after. No Docker daemon is available here.
* `helm install -f values-small.yaml` on a 2-CPU / 4 GiB node. No cluster is available, and no `helm`
  binary, so the `helm lint`/`template` tests in `test_helm_chart.py` were skipped.
* The compose `lite` stack booting with `docker compose up -d`.
* A non-editable wheel install (`pip install analystos` from an index): `config/`, `migrations/` and
  `packs/` are not package data yet.
* A per-pool image override in Helm (`compute-ml` only with `ml`). The chart runs one image for every pod.
