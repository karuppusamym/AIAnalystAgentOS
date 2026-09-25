# Release readiness (spec v1 §70)

**Current level: Prototype.** Assessed 2026-09-25 against the evidence in the
[capability register](02-capability-register.md).

| Level | Requirement | State |
|---|---|---|
| Prototype | Synthetic or approved non-sensitive data | ✅ Synthetic ServiceNow-shaped data (seeded generator + Table-API mock) |
| | Isolated development environment | ✅ Compose stack |
| | Read-only access | ✅ Enforced at 3 layers: validator, read-only transaction, reader role with `default_transaction_read_only` |
| | Visible limits and unsupported paths | ✅ Phase-3 endpoints return explicit errors; fallbacks are labelled; this document |
| Controlled pilot | Named owners and users | ❌ Needs the business owner to nominate |
| | Workspace access checks, positive + negative | ✅ Integration tests (scope, roles, SoD, revocation, expiry, cross-workspace 404) |
| | Each pilot connector tested against a real source | ⚠️ Live against a real engine: PostgreSQL, MySQL 8.4, SQLite, DuckDB. Mock only: ServiceNow. Catalog and unit tests only: SQL Server, Oracle, Snowflake, BigQuery, Databricks, Trino, Redshift, ClickHouse, MariaDB |
| | Audit and approval paths exercised | ✅ Live e2e exercises approval, role denial, pause/redirect |
| | Operational monitoring, rollback/recovery documented | ⚠️ Health endpoint, usage/audit APIs, rollback endpoint and runbook exist; no alerting/dashboards |
| | Unverified capabilities labelled | ✅ SSO, residency, connectors, scale are labelled here and in the tracker |
| Production | SSO + role mapping | ❌ Local JWT (OIDC-shaped claims) |
| | Independent security review | ❌ |
| | Secret rotation | ⚠️ Secrets are env/file references resolved just in time; rotation procedure documented, not automated |
| | Live connector certification (all connectors) | ❌ |
| | Backup/restore, DR tested | ❌ |
| | Deployment/schema parity | ⚠️ Alembic migrations; no multi-environment deploy yet |
| | Measured SLOs, load tests | ❌ |
| | Risk-tier evaluation thresholds (adversarial false-approval corpus) | ❌ Human approval stays mandatory for every external side effect until this exists |

## Known limitations that matter for a pilot

1. **Python sandbox** uses rlimits, an import allowlist and a stripped environment; it is not a
   security boundary. For a pilot run it in a network-less, read-only, non-root container
   (`--network none --read-only --cap-drop ALL`, or gVisor).
2. **SQL Server** has no read-only transaction in the gateway; it relies on a least-privilege
   login. Never connect it with a writable identity.
3. **Context2AI** integration is an adapter written against the v1 §11.1 paths with no live service
   to test; the local context store provides the same function.
4. **Superset** shares one analytics reader identity across workspaces; row-level isolation
   between workspaces inside Superset relies on per-workspace datasets and Superset permissions,
   which are not configured by this release.
5. **One source per run**; no cross-source federation.
6. **Scheduling**: one `analystos scheduler` process per deployment is enough, and several are
   safe (claim-then-execute), but there is no backlog alerting yet. Missed
   firings during downtime are not back-filled: the next firing uses the next cron slot.
7. **Notifications are in-app only**; email/chat/webhook delivery is deliberately absent until it
   can be approval-gated.
8. **PDF reports use a core font**: non-Latin characters render as `?` (a bundled Unicode font is needed).
9. **Source kinds without a session read-only switch** (SQL Server, Oracle, Snowflake, BigQuery,
   Databricks, Trino, Redshift, ClickHouse) rely on a least-privilege, read-only login. All of them
   except SQL Server are staged, so the gateway reads a snapshot with the reader identity; their
   extract still runs as the source login.
10. **Crawler semantics are English, keyword-based heuristics.** Descriptions are template sentences
    built from metadata. Model descriptions are optional (`crawl.llm_enrichment`, off by default),
    screened and never override curated text. PII detection from names and values is a safety net,
    not a data-classification programme: an owner's tags remain authoritative.
11. **Platform settings** are cached for 5 s in each process; a change reaches other processes within
    that window. A settings-table read error keeps the last-known-good document.
