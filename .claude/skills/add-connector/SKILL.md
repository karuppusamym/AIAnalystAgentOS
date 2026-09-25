---
name: add-connector
description: Checklist for adding a new source connector (pushdown or staged) without weakening governance. Use when asked to support a new data source.
---

1. Implement the `Connector` protocol in `src/analystos/connectors/<kind>.py`
   (`test`, `discover`, and either `extract` for staged or `sqlalchemy_url` for pushdown).
   Credentials only via `secret_ref` → `connectors/secrets.resolve_secret`, resolved just in time.
2. Register the kind in `connectors/registry.py`, `services/sources.py:KINDS`, and the dialect in
   `governance/policy.py:SOURCE_DIALECT`.
3. Pushdown: the identity must be read-only at the database level; the gateway also enforces a
   read-only transaction where the engine supports it — document if it does not (e.g. SQL Server).
4. Tests: metadata mapping unit tests; a validator test proving the dialect's SQL is scope-checked;
   an integration test against a real instance (mock tests do not certify a connector, spec v1 §62).
5. Add a tracker row + capability register entry with the certification status.
