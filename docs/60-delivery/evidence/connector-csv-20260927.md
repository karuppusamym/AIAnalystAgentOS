---
kind: csv
date: 2026-09-27
engine: real CSV, JSON, NDJSON, Excel and Parquet files staged into the test PostgreSQL
test: tests/integration/test_file_ingest_loads.py
result: pass
commit: 39d099f
---
# Connector certification evidence: csv

Live run 2026-09-27 16:18 UTC at `39d099f`, written by `scripts/certify_connectors.py` after every selected real-engine test passed (none skipped).

Command: `python -m pytest -q -p no:cacheprovider -m integration tests/integration/test_file_ingest_loads.py`

| Test | Result | Seconds |
|---|---|---|
| `test_merge_is_idempotent_and_the_fingerprint_follows_content` | passed | 3.1 |
| `test_null_keys_repeated_keys_and_column_mismatches_are_refused_with_the_column` | passed | 0.1 |
| `test_json_excel_and_parquet` | passed | 0.2 |
| `test_ingest_api_binds_the_source_to_its_workspace` | passed | 2.2 |
