---
name: add-connector
description: Checklist for adding a new source kind (database, warehouse, file or API) without weakening governance. Use when asked to support a new data source.
---

**Most SQL databases need no code (ADR-0010).**

1. **SQLAlchemy dialect exists:** add an entry to `config/source_kinds.yaml`:
   - `kind`, `label`, `category`, `required` / `optional` fields
   - `secret` field, `url_template`, `driver` (modules, packages, pip `extra`)
   - native `sqlglot_dialect`, `session_sql.readonly` / `timeout`
   - `catalog` method, `system_schemas`, and a `docs` line saying how read-only access is enforced

   Add the pip extra to `pyproject.toml`. The `GenericSQLConnector` does the rest.
2. **Pushdown** only if the gateway validator has a security suite for the dialect
   (`gateway/dialects.py` + `tests/unit/test_gateway_dialects.py`), the analysis compiler emits it
   (postgres, tsql, duckdb) **and** the session can be made read-only (`kinds.PUSHDOWN_DIALECTS`,
   `SourceKind.readonly_enforcement`); the kind's engine comes from `engines/registry.py`.
   Everything else stays **staged**, which is the default. Never widen pushdown to make a kind "faster".
3. **Credentials** only via `secret_ref` (`env:NAME` / `file:/path`), resolved just in time.
   `register_source` rejects secrets in config. Errors must not echo URLs or passwords (see
   `generic_sql.describe_error`).
4. **Non-SQL systems** (APIs, SaaS): implement the `Connector` protocol in
   `src/analystos/connectors/<kind>.py` (`test`, `discover`, `extract`) and wire it in
   `connectors/registry.py`. It is always staged.
5. **Metadata:** return `DiscoveredAsset/DiscoveredColumn` with keys, `references` and comments.
   The crawler (`services/crawler.py`) derives semantics, PII and drift from that metadata, so
   there is nothing to add there.
6. **Tests:**
   - unit tests for URL rendering, type mapping and the driver message (`tests/unit/test_source_kinds.py`,
     `test_connectors_generic.py`);
   - an integration test against a **real** engine (docker image or file) in
     `tests/integration/test_generic_sources.py`, proving discovery, a read-only session, staging and
     a gateway query with a denied column rejected.

   Mock tests do not certify a connector (spec v1 §62).
7. **Certify it from live evidence:** add the kind's `-k` expression to `LIVE_TESTS` in
   `scripts/certify_connectors.py` and run the script. It writes
   `docs/60-delivery/evidence/connector-<kind>-YYYYMMDD.md` only when every real-engine test passed;
   `connectors/certification.py` derives the kind's `certified` flag (`/api/source-kinds`, the
   `connector.<kind>` capability manifest) from that file. Never write the file by hand.
8. **Record it:** add a tracker row and a capability-register entry stating which kinds are
   live-tested and which are catalog-only.
