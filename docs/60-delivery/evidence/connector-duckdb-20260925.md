---
kind: duckdb
date: 2026-09-25
engine: DuckDB 1.5.5 (file database)
test: tests/integration/test_generic_sources.py -k "file_database and duckdb"
result: pass
commit: c8ff8d2
---
# Connector certification evidence: duckdb

Live run 2026-09-25 16:56 UTC at `c8ff8d2`, written by `scripts/certify_connectors.py` after every selected real-engine test passed (none skipped).

Command: `python -m pytest -q -p no:cacheprovider -m integration tests/integration/test_generic_sources.py -k "file_database and duckdb"`

| Test | Result | Seconds |
|---|---|---|
| `test_file_database_discover_stage_and_query[duckdb]` | passed | 1.7 |
