---
item: DEX-001 (DuckDB engine)
date: 2026-09-26
engine: DuckDB 1.5.5 (database file, pushdown opt-in)
test: tests/integration/test_duckdb_engine_run.py
result: pass
commit: bf40aa8
---
# DEX-001: a governed analysis run through the DuckDB engine

Live run 2026-09-26 at `bf40aa8` on the local compose Postgres (throwaway control plane
`analystos_test_askx`, analytics plane `analystos_test_dp_askx*`), local orchestrator, no model
provider key (the rule path).

Command:

```
ANALYSTOS_TEST_DATABASE_URL=postgresql+psycopg://…/analystos_test_askx ANALYSTOS_TEST_DP_DB=analystos_test_dp_askx \
ANALYSTOS_ORCHESTRATOR=local pytest -q -m integration tests/integration/test_duckdb_engine_run.py
```

## What ran

`evaluation.analytical.run_platform("itsm", 1, effects=True, source="duckdb")`, twice:

1. The V01 ITSM dataset (6,000 incidents, four planted effects) is written to a DuckDB database file
   in the workspace upload folder.
2. It is registered as a `duckdb` source with `execution_mode: pushdown` (the kind is staged by
   default; pushdown is the opt-in). Discovery reads the file's catalog; `main.incident` is selected.
   Nothing is staged: the source has no staging schema.
3. An investigation (`create_run`, publish skipped) runs the `investigate.v1` playbook end to end.
   Every statement goes through `QueryGateway.execute`, validated in the duckdb dialect and executed
   by the DuckDB engine on the file (read-only connection, external access off).

## Result

| | run 1 | run 2 |
|---|---|---|
| run | `run_02c9e46719dd` | `run_ade15afaef5d` |
| status | COMPLETED | COMPLETED |
| source | duckdb, pushdown, staging_schema none | duckdb, pushdown, staging_schema none |
| gateway statements (ok / rejected) | 64 (64 / 0) | 64 (64 / 0) |
| sources queried | only the DuckDB source | only the DuckDB source |
| tables referenced | `main.incident` | `main.incident` |
| verified findings (true / false) | 5 (5 / 0) | 5 (5 / 0) |
| planted effects found | 4 of 4 | 4 of 4 |
| wall time | 21.2 s | 16.1 s |

Verified findings (identical in both runs):

* I-1 Resolution hours is higher for opened after hours = true (numeric_by_segment, after_hours(opened_at))
* I-2 Missed SLA concentrates in reassignment count = 3+ (rate_by_segment)
* I-3 Volume is concentrated in few cmdb ci name values (pareto)
* I-4 Missed SLA is driven mainly by reassignment count (driver_model)
* I-5 Resolution hours is higher for assignment group name = Network (numeric_by_segment)

Deterministic end to end: the 64 result hashes of run 1 equal those of run 2, and both runs verified
the same findings from them.

## Scope

This demonstrates the DuckDB engine for a governed single-source investigation. It does not cover
federation across a DuckDB and another source (P4-E03 has its own test,
`tests/integration/test_cross_source_runs.py`) or DuckDB as a materialization engine.
