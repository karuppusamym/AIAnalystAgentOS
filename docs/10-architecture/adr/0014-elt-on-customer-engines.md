# ADR-0014 — Engines, federation and minimal ELT on the customer's compute

**Status:** proposed (2026-09-25, review C8)

**Context.** Today a run reads one source. Nothing can write anywhere, staged snapshots reload in
full with a 200k-row cap, and Superset is the only destination. The owner wants AnalystOS to
replace the analytics tool chain, including "minimum ETL/ELT", using "any Spark environment to run
and load" and "dbt or any database as ELT". Deployment is self-hosted enterprise.

**Update after increment 3** ([ADR-0010](0010-universal-sources-crawler-token-economy.md)).
Fifteen source kinds now connect through one generic connector. But only postgres and tsql push
down. Every other kind, including Snowflake, BigQuery and Databricks, is staged as an unordered
`LIMIT` of up to 1,000,000 rows (`connectors/generic_sql.py:634`), and nothing flags it. That
copies the data instead of moving the computation to it, and it can bias findings.

**Decision.**
0. **Multi-dialect validation** (a security suite per dialect) makes warehouses
   pushdown engines. Until a dialect is validated, staging declares a sampling strategy (full,
   time window, or native `TABLESAMPLE`), and truncation reaches verification as a population
   caveat (P4-C12).
1. An **`Engine` capability**: `execute_read`, `estimate`, `plan_write` (dry run) and
   `execute_write` (approved only). Planned implementations: Postgres, SQL Server, DuckDB, Trino,
   Spark (Spark Connect or Databricks SQL), Snowflake and BigQuery. Each stays `draft` until it is
   certified against a live instance. Feature flags are derived from the certification evidence.
2. **Cross-source runs** execute on a federating engine (Trino or DuckDB), or on Spark where the
   data already lives. Scope and identity stay per source.
3. **ELT = generate, dry-run, approve, run on their engine.** The `elt_build` playbook emits a dbt
   project (models, tests, Ossie metrics), or Spark SQL, from the virtual dataset and approved
   KPIs. It is dry-run and estimated on the target. A hash-bound approval covers the project hash,
   engine, target schema and plan hash. It then executes on the customer's dbt or Spark runner
   under a separate write identity limited to designated target schemas. Manifests and
   OpenLineage events feed lineage.
4. A **`BuildGateway`**, separate from the read-only `QueryGateway`, accepts only approved build
   plans, re-verifies them immediately before running, audits every job, and records a rollback
   plan. Sources stay read-only.
5. AnalystOS does **not** build an ETL engine or a pipeline scheduler. A spike evaluates dlt for
   incremental ingestion from non-SQL sources, and its outcome gets its own ADR.

**Consequences.** Compute stays in the customer's governed estate, and AnalystOS owns the *plan*
and its evidence. Write access is a new risk class. It is contained by the separate identity, the
target-schema allowlist, the approvals and the rollback plan. Every engine adds a certification
burden (live instance, dialect tests); P16 keeps that honest.
