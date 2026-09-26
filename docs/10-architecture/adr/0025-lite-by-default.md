# ADR-0025 — Lite by default: one small install, more services only when a feature needs them

**Status:** proposed (2026-09-26, spec v4 §12a; tracker P7-16..P7-18). **Amends
[ADR-0003](0003-full-stack-compose.md)**. The full stack stays the CI and production-scale profile
but stops being the default install. Source: the owner's requirement that the application "should not
look heavy" (UI *and* install), and the
[2026-09-26 weight audit](../../70-reviews/2026-09-26-build-right-study.md) §3.

**Context.** Today the default `compose.yaml` starts 12 containers:

- Postgres, Redis, Temporal and Superset;
- the ServiceNow demo mock;
- the API, four worker pools, the scheduler and the web front end.

The Helm chart's defaults request about 7 CPU and 12 GiB, before counting the external Postgres,
Redis, Temporal and Superset. One Python image carries numpy, scipy, statsmodels, scikit-learn,
pandas, polars, pyarrow, duckdb and matplotlib to every service, including the mock. There are 73
`ANALYSTOS_*` environment variables.

The engine is already orchestrator-agnostic (ADR-0003: `plan / get_state / execute_task /
finish`). `ANALYSTOS_ORCHESTRATOR=local` runs the same loop in-process
(`workflows/orchestrator.py::run_local`), so the minimum useful product is Postgres + API + web.
The local loop has three gaps that stop it being a supported mode:

1. It runs in a daemon thread and **does not resume after a restart**.
2. It **fails any run after one hour**, including one legitimately waiting for an approval.
3. It **executes ready tasks one at a time**.

Spend caps also fail closed when Redis is missing (P4-06), so a Redis-less install refuses every
model call.

**Decision.**

1. **Three profiles, one codebase and one behaviour.**

   | Profile | Runs | For |
   |---|---|---|
   | `lite` (default) | Postgres (pgvector) + `api` (local orchestrator, in-process scheduler, preview publisher) + `web` | Evaluation, a single team, the first pilot, air-gapped laptops |
   | `standard` | lite + Redis + Temporal + one worker serving all queues + a scheduler process | A shared team deployment |
   | `scale` | standard with separate pools per queue, PgBouncer and HA values (today's Helm defaults) | Many workspaces and concurrent runs (P4-S05 targets) |

   Optional features are compose profiles on top of any profile:
   - `bi`: Superset;
   - `graph`: Neo4j;
   - `demo`: the ServiceNow mock;
   - `sandbox`: the container sandbox;
   - `pooled`: PgBouncer.

   The policy default for publishing becomes `preview`, and `superset` is added when the `bi`
   profile is present.
2. **Close the local-loop gaps** so `lite` is a supported mode, not a demo:
   - **Resume on restart:** API startup re-drives every non-terminal run from its Postgres task state.
     The engine's claims already make this safe.
   - **No wall-clock cap on waiting:** a run in `WAITING_USER` holds no thread. It is re-driven
     when the approval signal lands, just as Temporal is nudged today.
   - **Bounded parallelism:** a small thread pool, sized by `ANALYSTOS_LOCAL_WORKERS`.
   - **Postgres budget reservations:** when Redis is absent, spend-cap reservations use a
     row-locked budget table. They are still atomic and still fail closed, but they no longer
     depend on Redis.
3. **Slim images.**
   - Heavy libraries move to extras: `[ml]` (scikit-learn, statsmodels), `[reports]` (matplotlib,
     fpdf2, openpyxl), `[temporal]`, `[graph]`. numpy, scipy and duckdb stay in core because the
     methods need them.
   - The API/lite image installs `core + reports`.
   - The isolated `compute-ml` image (ADR-0022) is the only one with the ML stack.
   - The mock gets its own tiny image.
4. **Small Helm values.** `values-small.yaml` runs one API, one worker with `--queues all` and one
   web pod, with no pod disruption budgets.
5. **Configuration tiers.** Document the environment in three tiers: 3 required variables,
   optional ones grouped by feature, and advanced. The admin control plane shows the preset,
   the spend cap and the feature flags by default, and the rest under *Advanced*.
6. **Features declare what they need.** A capability manifest can name a required profile
   (for example, a Superset publisher needs `bi`). The capability registry reports a feature
   whose profile is missing as *unavailable, with the reason*. The UI then hides it or explains
   it; it is never a broken button.

**Consequences.**

- A new user runs three containers and gets Ask, investigations, reports, schedules and monitors.
- Durability in `lite` is "resume from Postgres after a restart", not Temporal's timers and
  history. That is acceptable for one team and documented as the reason to move to `standard`.
- CI runs the end-to-end scenario on both `lite` and `standard`, so the two execution paths
  can't drift.
- ADR-0003's intent (the same engine everywhere) is kept. Its default ("everything runs from
  day one") is reversed.
