---
kind: postgres
date: 2026-09-27
engine: PostgreSQL 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1) (localhost:5432)
test: tests/integration/test_generic_sources.py -k "postgres"
result: pass
commit: 39d099f
---
# Connector certification evidence: postgres

Live run 2026-09-27 16:18 UTC at `39d099f`, written by `scripts/certify_connectors.py` after every selected real-engine test passed (none skipped).

Command: `python -m pytest -q -p no:cacheprovider -m integration tests/integration/test_generic_sources.py -k postgres`

| Test | Result | Seconds |
|---|---|---|
| `test_postgres_generic_discovery_with_filters` | passed | 1.3 |
| `test_postgres_pushdown_gateway_and_read_only_session` | passed | 0.3 |
