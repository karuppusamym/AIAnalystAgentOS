# Connector certification run (P4-09) — 2026-09-27

`scripts/certify_connectors.py --kinds postgres sqlite duckdb csv mysql servicenow snowflake databricks sqlserver`
at `39d099f`, against the local PostgreSQL 16.15 on localhost:5432 (no Docker on this host), with isolated test
databases (`analystos_test_b2c`, `analystos_test_dp_b2c`). A kind is certified only by its per-kind evidence file,
which the script writes after every selected real-engine test passed with none skipped
(`connectors/certification.py` reads those files; nothing is hand-kept).

| Kind | Result | Evidence / reason |
|---|---|---|
| postgres | **certified** (2 real-engine tests) | `connector-postgres-20260927.md` |
| sqlite | **certified** (1) | `connector-sqlite-20260927.md` |
| duckdb | **certified** (1) | `connector-duckdb-20260927.md` |
| csv (file sources: CSV, JSON, NDJSON, Excel, Parquet) | **certified** (4) — new: the script now runs `tests/integration/test_file_ingest_loads.py` for this kind | `connector-csv-20260927.md` |
| mysql | not certified today: its 4 tests skipped (they start a MySQL container; no Docker here). The 2026-09-25 evidence (`connector-mysql-20260925.md`) still stands | — |
| servicenow | **not certifiable here**: only the Table-API mock exists. `tests/integration/test_staging_servicenow_load.py` passed 3/3 against the mock the same day — that exercises the connector, it does not certify it (spec v1 §62) | needs a live ServiceNow instance |
| snowflake, databricks, sqlserver | **not certifiable here**: no real-engine test in this repository | need a live warehouse / server and a live test |

Oracle, BigQuery, Trino, Redshift, ClickHouse and MariaDB were not attempted: like the warehouses above they have
catalog and unit tests only. Live warehouse certification is a pilot step outside this repository.
