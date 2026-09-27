---
kind: sqlite
date: 2026-09-27
engine: SQLite 3.45.1 (file database)
test: tests/integration/test_generic_sources.py -k "file_database and sqlite"
result: pass
commit: 39d099f
---
# Connector certification evidence: sqlite

Live run 2026-09-27 16:18 UTC at `39d099f`, written by `scripts/certify_connectors.py` after every selected real-engine test passed (none skipped).

Command: `python -m pytest -q -p no:cacheprovider -m integration tests/integration/test_generic_sources.py -k "file_database and sqlite"`

| Test | Result | Seconds |
|---|---|---|
| `test_file_database_discover_stage_and_query[sqlite]` | passed | 2.1 |
