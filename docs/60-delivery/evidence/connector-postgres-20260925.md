---
kind: postgres
date: 2026-09-25
engine: PostgreSQL 16.15 (Debian 16.15-1.pgdg12+2) (compose)
test: tests/integration/test_generic_sources.py -k "postgres"
result: pass
commit: c8ff8d2
---
# Connector certification evidence: postgres

Live run 2026-09-25 16:56 UTC at `c8ff8d2`, written by `scripts/certify_connectors.py` after every selected real-engine test passed (none skipped).

Command: `python -m pytest -q -p no:cacheprovider -m integration tests/integration/test_generic_sources.py -k postgres`

| Test | Result | Seconds |
|---|---|---|
| `test_postgres_generic_discovery_with_filters` | passed | 1.2 |
| `test_postgres_pushdown_gateway_and_read_only_session` | passed | 2.0 |
