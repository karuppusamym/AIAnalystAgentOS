# ADR-0003 — Full infrastructure from day one, orchestrator-agnostic engine

**Status:** accepted (user decision, 2026-09-25: "full stack from day one")

**Decision.** Compose runs Postgres (pgvector), Redis, Neo4j, Temporal, Superset from the start.
The run engine exposes four operations (plan, get_state, execute_task, finish) that both the
Temporal workflow and a local loop call, so integration tests and single-process demos exercise
the same code as production.

**Consequences.** Superset 4.1.1 needs a derived image with `psycopg2-binary` (`deploy/superset/Dockerfile`).
Neo4j is a projection (rebuildable from Postgres) so its outage never loses provenance.
