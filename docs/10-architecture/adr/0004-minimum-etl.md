# ADR-0004 — Minimum ETL: pushdown or bounded snapshot; virtual datasets

**Status:** accepted

**Decision.** SQL-queryable sources are queried in place with a least-privilege read-only identity.
Non-SQL sources (ServiceNow Table API, files) are extracted into `analytics.src_<source>` as a
bounded snapshot with an atomic swap; only the reader identity queries it. The reusable analytical
dataset is a *virtual* dataset (governed SELECT with derived columns), re-validated against current
scope at publish time. No writes to sources in this release.

**Consequences.** Superset reads the same analytics DB as the gateway, with the same reader
identity. Cross-source federation (Trino) and materialization are Phase 2.
