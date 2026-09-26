# Pipelines, incremental processing and the managed writer (P6-01, P6-02, P6-03, P6-08)

Backend reference for the pipeline workbench (the UI follows in its own row). Everything here is under
`/api`, authenticated with a bearer token, and every child id is bound to the path's workspace.

## Concepts

| Concept | Where | What |
|---|---|---|
| Recipe | `contracts/recipe.py`, ADR-0023 | Typed transformation IR, validated before any compiler sees it. |
| `incremental` block | `contracts/recipe.py:Incremental` | `{watermark, key, late_window, deletes: reconcile\|soft\|ignore, reconcile_every_hours, max_delete_pct, source, backfill}`. The same block on a staged source table (source config `incremental: {<table>: {...}}`), on a recipe, and in a PipelineSpec. |
| PipelineSpec | `contracts/work.py:PipelineSpec` | Envelope around published recipes: input versions, output grain/keys/schema, join expectations, reconciliation checks, freshness, budgets, incremental block, destination. Checked against the recipes by `pipelines/spec.py`. Also the `pipeline` payload of a work order and the `pipeline` definition kind. |
| Dry run | `pipelines/dryrun.py` | Per-source scope and snapshot manifest, compiled SQL, checks and the reconciled virtual output; the output rows become a content-addressed candidate. |
| Managed writer | `pipelines/writer.py` | A fourth analytics identity (`ANALYSTOS_ANALYTICS_WRITER_URL`, login `analystos_writer`) that `SET LOCAL ROLE`s to the workspace writer role (`analystos_w_<workspace>`). CREATE on allowlisted destination schemas only; no privilege on any source schema. |
| Materialization | `db/models.py:Materialization` | One version of a destination table: `<table>__v<n>` behind the view `<schema>.<table>`; `previous_id` is the rollback pointer; `idempotency_key` + `checkpoint` make retries resume. |

## Incremental staging (P6-02)

Declare it on the source (ServiceNow implements the watermark reads):

```json
{"incremental": {"change_request": {"watermark": "sys_updated_on", "key": ["sys_id"],
                                    "late_window": "10m", "deletes": "reconcile"}}}
```

* Every refresh (asset selection, `dataset_refresh` schedules) reads `[watermark - late_window, high watermark]`,
  the high watermark measured once before paging (fixed cursor, keyset pagination), de-duplicates on the
  key, merges it and commits the new watermark in the same transaction (`<schema>.aos_load_state`,
  loader-only). A crash before the commit re-reads the window; writers of one table are serialized.
* Deletes: a full key reconcile runs every `reconcile_every_hours` (default weekly) or on demand; a reconcile
  that would remove more than `max_delete_pct` of the table is refused as a probable outage.
* `POST /workspaces/{ws}/sources/{source_id}/refresh` `{asset, mode: full|reconcile|replay|backfill, since, until}`
  re-stages one table; replay/backfill merge the closed range without moving the watermark.

Recipes declare the same block (or receive it from a PipelineSpec). Only row-level recipes qualify (no
aggregate or window after the watermark source, dedupe on the key only, the source on the left of joins).
`POST /workspaces/{ws}/recipes/{id}/runs` accepts `refresh: auto|full|reconcile|replay|backfill` and
`since`/`until`.

## Endpoints

| Method and path | Role | Does |
|---|---|---|
| `POST /workspaces/{ws}/pipelines/validate` `{spec}` | analyst | Checks a PipelineSpec against its recipes; 422 `pipeline_invalid` lists every problem. |
| `POST /workspaces/{ws}/pipelines` `{spec}` | editor | Saves a draft version (unchanged content returns the latest). |
| `GET /workspaces/{ws}/pipelines`, `GET .../pipelines/{id}` | viewer | Versions. |
| `POST .../pipelines/{id}/publish` | editor | Publishes (its recipes must be published). Runs and schedules use published versions; drafts only in a dev workspace. |
| `POST .../pipelines/{id}/dry-run` `{engine?}` | analyst | Returns the run: `manifest`, `sql`, `checks`, `reconciliation`, `candidate`. Status `succeeded`, `blocked` (a check failed; an alert is raised) or `awaiting_approval` with `approval_id` when the pipeline names a destination. |
| `POST .../pipelines/{id}/runs` `{mode: auto\|full\|reconcile\|replay\|backfill, since?, until?}` | editor | Runs the recipes into the workspace's managed recipe-output source, incremental when declared. |
| `GET /workspaces/{ws}/pipeline-runs[?pipeline=]`, `GET .../pipeline-runs/{run_id}` | viewer | Runs. |
| `POST .../pipeline-runs/{run_id}/materialize` `{approval_id}` | editor | `verify_for_execution` immediately before the write, then stage, validate in the database, promote. Re-posting after a failure resumes from the checkpoint under the same approval; re-posting after success returns the same materialization. |
| `GET/POST /workspaces/{ws}/writer-destinations` `{schema, tables?}` | viewer / owner | Allowlists a destination schema (and optionally its tables); never a source schema, a build target or a system schema. |
| `GET /workspaces/{ws}/materializations[?table=]` | viewer | Versions with status `promoted`, `superseded`, `rolled_back`, `failed`. |
| `POST .../materializations/{id}/rollback` | owner | Re-points the destination at the version the current one replaced. |

Approval (`action: pipeline_materialize`, risk `high`) binds the pipeline run, spec and recipe hashes, the
destination, the candidate digest, row count, columns and keys, and the rollback pointer. Another
promotion or a rollback in between changes the payload and invalidates the approval.

## Events and alerts

`pipeline.saved`, `pipeline.published`, `pipeline.dry_run.{completed,blocked,failed}`,
`pipeline.run.{completed,blocked,failed}`, `writer_destination.provisioned`, `pipeline.materialized`,
`pipeline.materialization.failed`, `pipeline.rolled_back`, `pipeline.freshness.breached`. Failures, blocked
runs and stale destinations (older than `freshness.max_age_hours`, checked by the scheduler's
housekeeping, once per version) also create a `pipeline` notification.

Schedules: kind `pipeline`, config `{pipeline: <name>, action: run|dry_run, mode: auto|full|reconcile}`.
A schedule never writes a destination: `dry_run` proposes the approval.

## The engineer agent and `prepare.v1` (P6-08)

`agent.engineer` (`config/agents/engineer.yaml`, `builtin:generic`) has one action, `skill.pipeline_propose`,
which returns recipe and PipelineSpec proposals only after the recipe IR validator, the scope check and the
PipelineSpec check accept them. Model purpose `pipeline_proposal` (`[cache, rules, llm_small]`); in `off` or
`auto` mode the rules path proposes a catalog-derived staging recipe. `playbook.prepare` runs metadata, then
the engineer; a person saves, publishes, dry-runs and approves.

## Operations

* New clusters: `deploy/postgres/01-init.sql` creates `analystos_writer`. Existing clusters: `analystos migrate`
  creates the login through the loader (like the builder). Outside `env=dev` the writer password must not be
  the development default.
* The writer URL must be a direct (or session-pooled) connection; it sets its role per transaction.
* `ANALYSTOS_WRITER_KEEP_VERSIONS` (default 3) version tables are kept per destination table.
