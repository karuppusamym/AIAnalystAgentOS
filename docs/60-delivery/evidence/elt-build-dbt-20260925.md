# Live dbt build through the BuildGateway — 2026-09-25 (P4-E04, P4-E06)

**What this is.** A live run of `playbook.elt_build` against the compose Postgres 16 with a real
**dbt Core 1.12.5 + dbt-postgres 1.11.0** runner (a separate virtualenv invoked as a subprocess, i.e.
"the customer's runner"). Captured by `tests/integration/test_elt_build.py::test_live_dbt_build_through_the_build_gateway`
with `ANALYSTOS_EVIDENCE_OUT` set; the raw record is [`elt-build-dbt-20260925.json`](elt-build-dbt-20260925.json).

**What this is not.** Not a dbt Cloud or Spark run, not a customer warehouse: the engine is the
platform's own analytics database (`postgres:analytics`), the source is the ServiceNow mock staged
into it, and the database and roles are the test session's isolated ones
(`analystos_test_dp_e04_analytics`, roles `aostest_e04_*`). No model provider was configured: every
step ran on its deterministic path.

## Steps and outcome

| Step | Result |
|---|---|
| Source run | `playbook.investigate` on the staged ServiceNow `incident` table (20,000 rows), publication skipped; one KPI (`record_count`) approved in the P4-K03 semantic model by the approver user |
| Target | `aos_mart` designated by the workspace owner: build role `aostest_e04_b_<ws>` gets USAGE+CREATE on `aos_mart` only, reads staged schemas through the workspace reader role |
| Plan (`elt_plan`) | dbt project of 5 files (model, time spine, `schema.yml` with tests + semantic model + metric, `sources.yml`, `dbt_project.yml`); project hash `e7b0126d…`; `dbt parse` (no connection) clean; manifest guard passed |
| Dry run / estimate | candidate tests `not_null(sys_id)`, `unique(sys_id)`, `not_null(opened_at_month)` all hold on current data (read through the query gateway); estimate 20,000 rows × 30 columns ≈ 9.6 MB (rough, labelled) |
| KPIs | `record_count` built (approved); 6 run KPIs skipped with the remedy, because the workspace policy `require_approved_metrics` is on |
| Approval | `elt_build`, payload hash `aad0e6cc…` binding job id, project hash, engine, target schema, relations, runner and rollback statements; plan hash = the run's plan hash; approved by a different user |
| Build (`elt_run`) | BuildGateway re-verified the approval immediately before running, consumed it (`executed`), re-hashed the files on disk, re-parsed and re-checked the manifest, then `dbt build`: 2 models `success` (table: `SELECT 20000`; time-spine view), 3 tests `pass`; 0.53 s dbt time, 8.4 s for the whole step |
| Result | `aos_mart.aos_find_the_drivers_of_sla_breach_007f3a` has 20,000 rows (= the estimate), is owned by the workspace build role, and is readable by the workspace reader role (the query identity) |
| Ossie | dbt 1.12 wrote `osi_document.json` (Ossie 0.1.1) from the generated semantic YAML; the P4-K03 importer reads it back as valid, metric `record_count`, no issues |
| Lineage | `approval -authorized-> build_job`, `transformation -executed_as-> build_job`, `build_job -produced-> table` ×2, `table(src) -transformed_into-> table(mart)`, `dataset/metric -materialized_by-> transformation` |
| OpenLineage | START + COMPLETE `RunEvent` per model (2-0-2 shape, jobType/SQL job facets, schema dataset facet) stored on the job |
| Audit | `approval.requested`, `policy.build`, `approval.decided`, `build.started`, `build.node` per dbt node, `build.completed` |
| Rollback plan | recorded before approval and inside the approved payload: `DROP TABLE IF EXISTS "aos_mart"."aos_find_…" CASCADE`, `DROP VIEW IF EXISTS "aos_mart"."metricflow_time_spine" CASCADE`; restore = re-run the previous succeeded job for the target (none yet) under its own approval |

## Negative evidence in the same session (same test module)

- Postgres refuses the build role `CREATE TABLE public.*`, `CREATE SCHEMA`, and any `CREATE/INSERT/DELETE/DROP/ALTER`
  on the source's staged schema; the builder login without `SET ROLE` cannot create in the target; the reader cannot write the target.
- A source schema cannot be designated a target; an undesignated target cannot start a build.
- BuildGateway refuses: a pending approval, no approval, an expired approval (status `expired`), a project
  file changed after approval (approval `invalidated`, job `refused`, `build.refused` audited), a target
  schema changed after approval, and a target retired after approval.
