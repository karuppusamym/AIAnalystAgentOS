---
kind: mysql
date: 2026-09-25
engine: MySQL (docker image mysql:8.4)
test: tests/integration/test_generic_sources.py -k "mysql"
result: pass
commit: c8ff8d2
---
# Connector certification evidence: mysql

Live run 2026-09-25 16:56 UTC at `c8ff8d2`, written by `scripts/certify_connectors.py` after every selected real-engine test passed (none skipped).

Command: `python -m pytest -q -p no:cacheprovider -m integration tests/integration/test_generic_sources.py -k mysql`

| Test | Result | Seconds |
|---|---|---|
| `test_mysql_test_and_discovery` | passed | 11.0 |
| `test_mysql_bad_password_is_a_clear_secret_free_error` | passed | 0.0 |
| `test_mysql_session_is_read_only_even_for_root` | passed | 0.0 |
| `test_mysql_extract_stage_and_gateway_query` | passed | 1.6 |
