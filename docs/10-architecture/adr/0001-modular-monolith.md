# ADR-0001 — Modular monolith with process roles, not 15 services

**Status:** accepted (2026-09-25)

**Context.** Spec v1 §57 lists ~15 services. In Phase 1 all of them share one database, one
policy model and one release cadence; their calls are synchronous and on the same critical path.

**Decision.** One Python package with enforced package boundaries (contracts in
`analystos.contracts`), deployed as three roles from one image: `api`, `worker` (Temporal
activities + workflows), `web`. Infrastructure stays separate (Postgres, Redis, Neo4j, Temporal, Superset).

**Consequences.** + fewer network failure modes, one migration stream, trivial local dev, same code
in tests and prod. − scaling is per role, not per module; a module that needs independent scaling
(e.g. query workers, publishing workers) is split out later behind its existing interface
(`QueryGateway`, `BIPublisher`), which is why those are protocol-shaped.
