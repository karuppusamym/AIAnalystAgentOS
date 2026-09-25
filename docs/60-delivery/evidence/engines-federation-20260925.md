# Evidence — Engine layer, multi-dialect validation and cross-source runs (P4-E01, P4-E03)

**Date:** 2026-09-25 · **Branch:** worktree-agent-a74f76984a1a34b19 · **Kind:** unit suites + live integration
run against a real Postgres (compose) and a real DuckDB file. **Not** a connector certification: this
file is deliberately not named `connector-*.md`, so it certifies no source kind.

## What was run

| Evidence | Where | Result |
|---|---|---|
| Validator security suite, 6 new dialects × (legitimate queries, denied columns through 10 paths, 14 generic attacks, unknown/namespaced functions, comment stripping) + per-dialect attack lists (snowflake 18, bigquery 16, databricks 22, trino 16, duckdb 29, mysql 18) + postgres-unchanged checks | `tests/unit/test_gateway_dialects.py` | 286 passed |
| Existing validator/gateway suites through the Engine layer (postgres, tsql unchanged) | `tests/unit/test_gateway_validator.py`, `test_gateway_service_unit.py`, `test_skills_gateway.py` | pass |
| Federation without services: two real DuckDB files as two pushdown sources, planted effect, denied column in source B, join-proposal decisions, engine sandbox, engine catalog | `tests/unit/test_federation.py` | 8 passed |
| **Live acceptance:** Postgres pushdown source (`xops.tickets`, 1,200 rows) + DuckDB file source (`crm.customers`, 150 rows), both discovered by their real connectors, scope from `resolve_scope` over both | `tests/integration/test_cross_source_runs.py` | 2 passed |
| Fast suite | `pytest -m "not integration"` | 1685 passed |

## Live cross-source run (tests/integration/test_cross_source_runs.py)

* Planted effect: tickets of `enterprise` customers take 3× longer to resolve; the tier exists only in
  the DuckDB source, the hours only in Postgres.
* Join keys: rules proposed `xops.tickets.customer_id → crm.customers.customer_id`; a simulated model
  proposal `xops.tickets.id → crm.customers.customer_id` was also submitted. Deterministic decision
  through the federated gateway runner: the rule key accepted (containment 1.0, many_to_one), the model
  key **rejected** (containment below 0.8).
* Effect: `resolution_hours` by `tier` across the accepted join — supported, top segment `enterprise`,
  p < 1e-6; the unplanted `region` attribute not supported.
* Audit: each source leg is its own `query_execution` row (source id set, `federation.leg:<parent>`,
  no result preview retained); the federated statement is audited as `federated:<purpose>`.
* Negative (per-source scope): `crm.customers.email` is tagged `restricted` in source B; selecting it,
  expanding `*` over it, or using it in a subquery of a federated statement is rejected
  (`restricted by policy`) before any leg runs; an unknown asset is rejected; the same join without the
  federated runner is refused (`Cross-source queries are not supported`).

## Design facts the tests pin

* Pushdown = validator suite ∩ analysis compiler ∩ read-only session. New: DuckDB files (opt-in,
  `pushdown_default: false`; attached READ_ONLY, external access off, configuration locked). MySQL has a
  validator suite and a read-only session but the analysis compiler does not emit MySQL, so it stays
  staged. Snowflake, BigQuery, Databricks and Trino have validator suites but no session read-only
  switch: draft engines, staged.
* The federation engine never connects to a source: legs go through `QueryGateway.execute` under each
  source's bound scope and identity; the in-memory DuckDB is sandboxed (a direct `read_csv` or an
  attempt to re-enable external access fails even without the validator).

## Not evidenced here

* No live Snowflake, BigQuery, Databricks, Spark or Trino instance (P4-E02): their engines are `draft`.
* Full `-m integration` run on the shared Postgres (max_connections 100, other agents running): the
  failures/errors caused by `too many clients` were re-run file by file and pass
  (`test_generic_sources.py`, `test_snapshot_population.py`, `test_temporal_queues.py`,
  `test_model_replay_db.py`, `test_cross_source_runs.py`). Two failures remain and reproduce
  identically on the unmodified HEAD (5796077), so they are not caused by this change:
  `test_context_tokens.py::test_prompt_tokens_per_run_with_every_chat_purpose_on` and
  `test_registries.py::test_scheduled_reanalysis_replays_the_registry_with_zero_model_calls`.
