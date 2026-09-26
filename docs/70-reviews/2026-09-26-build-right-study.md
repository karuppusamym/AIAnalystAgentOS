# Build-right study — status count, AgentSwarms patterns, donor code quality, product weight (2026-09-26)

**What this is.** A dated record of four questions the owner asked on 2026-09-26, after
[spec v4](../00-intent/04-spec-v4-unified-data-platform.md):

1. How much is done, and how much is pending?
2. Is the donor code (Atlas, DataPilot) good enough to port, or should we build something thinner?
3. What can AnalystOS reuse from AgentSwarms?
4. How do we keep the application from looking heavy, in both the UI and the install?

Like every file in `70-reviews/`, this is evidence, not a work queue. The resulting decisions
are ADR-0025, the revised ADR-0018 port table, spec v4 P23 and §15, and tracker rows P7-15..P7-18.
Measured against `develop@5ea25dd`, merged into the PR branch as `d6340bb`.

## 1. Status count (tracker rows with a status column)

| Scope | Rows | Done | Done (mock) | Partial | Not started | Later phase / deferred | Proposed |
|---|---|---|---|---|---|---|---|
| Increments 2–4 and the v1 phase tables (`develop`) | 157 | 124 | 1 | 11 | 15 | 5 | 1 |
| Added by spec v4 and this study | 30 | — | — | — | 26 | 4 | — |
| **All** | **187** | **124 (66 %)** | **1** | **11** | **41** | **9** | **1** |

Increment 1 is recorded as prose, not rows. Row counts are not effort: a Done row can be a
one-line fix, and the open rows include the largest pieces (the semantic compiler, the ML
lifecycle, pipelines, the workbench UI).

The 11 Partial rows mostly need **live certification**, not code:

- P4-X05: a real MCP server;
- P4-T04: prompt-cache share with a funded provider;
- P4-K09 and CTX-001: a live Atlas instance;
- P4-E01: live Snowflake or Databricks;
- P4-S04: air-gapped run with SSO;
- P4-V01 and P4-V02: live-model tiers;
- FND-004: a deployment target;
- META-002: live SQL Server.

P4-06 is the exception: work orders, idempotency and the outbox are still unbuilt there.

## 2. AgentSwarms (`AgentSwarms-fyi/agentswarms@9c8ea32`, read-only clone)

**Licence and fit.**

- **Licence:** Elastic License 2.0 across the whole repository, including `sdk/`, `evals/` and
  `services/`; none of it is under a different licence. Copied code would bring the ELv2 terms with
  it, including the ban on offering it as a hosted service, and there are notice obligations.
- **Fit:** it is also about 359k lines of TypeScript on Supabase, so there is nothing to port into a
  Python codebase anyway.
- **Rule:** re-implement ideas only. Do not transcribe code, prompt strings or document prose.

**What it does well.** These are patterns that sharpen designs we already adopted:

| Pattern (AgentSwarms file) | What we take | Lands in |
|---|---|---|
| Verdict pinned to a fingerprint of the semantic model and normalised SQL; void is *shown*, not hidden; a prior verdict is *offered, not applied* on a repeat question; flagging "wrong" needs a reason (`src/lib/analystVerification.ts`) | The last three behaviours. Our fingerprint stays wider: it also covers data version, method, context and policy. | ADR-0020 |
| Schedules re-run pinned SQL with no model call, compute deltas in code, say "nothing changed" out loud, and mark the narrative stale until it is rewritten (`src/lib/analystSchedule.ts`) | Computed deltas, "nothing changed", narrative-stale state | ADR-0021 |
| Governed step `{model, metrics, dimensions}` whose names are validated as a block; any unknown name downgrades the badge (`src/lib/aiAnalyst.ts` `parseSemanticStep`) | Same as ADR-0019 decision 4; confirms the design | ADR-0019 |
| Fan-out handled with a **multi-fact plan** (one branch per fact, stitched on a dimension spine) *or* a refusal with the reason; certification only after measured probes; an edit demotes to draft; `check:semantics` runs offline in CI (`src/lib/semanticLayer.ts`, `docs/SEMANTIC_LAYER.md`) | Refuse first, multi-fact plan as a later compiler feature; `analystos check-semantics` in CI | ADR-0019 |
| Row filters as `dimension ∈ {{user.attr}}`, failing closed when the attribute is missing; masked fields hidden from the planner's catalog (`src/lib/semanticPolicy.ts`) | User-attribute row filters in the compiler | ADR-0019 |
| ETL: the watermark is persisted **after** the durable load; gate severities `fail/warn/drop`; schema policy `evolve/warn/strict` (`docs/ETL_PIPELINES.md`) | All three | ADR-0023 |
| Idempotency key resolved as claim, replay, in-progress, or **mismatch → 409** when the request hash differs (`src/utils/swarmIdempotency.ts`) | Mismatch rule | P4-06 |
| One navigation manifest drives the sidebar, command palette and docs checks; a first-run checklist built from real state; a status band that says "everything is running"; starter questions written from the schema (`src/lib/appNav.ts`, `FirstRun.tsx`, `StatusBand.tsx`) | AnalystOS already has the manifest (`web/src/routes.ts`, `SCREEN_BUDGET = 20`, tested). We take the two-state Home and first-run checklist. | spec v4 §15, P7-18 |

**What not to copy.**

- **Governance gaps:**
  - The analyst's SQL gate checks only the first verb, so `WITH d AS (DELETE …) SELECT` gets through.
  - The self-check is an LLM reviewing itself.
  - Approvals are not bound to a hash.
  - The budget cap is opt-in and fails open.
  - The analyst loop runs in the browser by default.
- **Weight:**
  - 36 navigation destinations; pages of up to 4,000 lines; the knowledge and IAM pages have 15 tabs each.
  - 13 compose services, all started by default. Its own docs say they should sit behind profiles.
- **Supabase coupling:** row-level security as the access authority, and a service-role key in the app.

AnalystOS is already ahead on the parts that matter most here: one AST-checked gateway, a
deterministic REV and numbers guard, hash-bound approvals, and spend caps that fail closed. The
lesson from AgentSwarms is **what a large feature set does to a product when every feature gets
a screen and a service**. That is the failure mode ADR-0025 and P23 exist to prevent.

## 3. Product weight today

**UI:**

- 19 routed screens (a tested budget of 20), with 16 navigation entries in 6 groups.
- 35 tabs across 9 screens.
- About 35 concepts on first login.
- No guided path; P4-07 is not started.
- Admin screens (Registry, Platform settings, Usage & cost) are visible to every role.
- Several concepts have more than one home:
  - metrics: Knowledge and Build;
  - dashboards: Studio and Artifacts;
  - model configuration: in three places;
  - audit: in two places;
  - autonomy: in three places.
- Vocabulary is mixed: run / investigation / analysis, insight / finding.
- The policy is edited as raw JSON.

**Spec conflict.** Spec v3 §9 says "five journeys" but lists six, and those six are what is built.
The workbench design ([`02-workbench-ux.md`](../10-architecture/02-workbench-ux.md)) proposes a
different five with a **Start work** action. My own spec v4 §15 then added Pipelines and Models
*tabs*, which contradicts both. Spec v4 §15 is rewritten by this study.

**Install:**

- The default compose file starts 12 containers: Postgres, Redis, Temporal and Superset; the demo
  mock; the API, four worker pools, the scheduler and the web front end.
- Helm defaults request about 7 CPU and 12 GiB before any external service.
- One Python image carries the whole ML/reporting stack to every service.
- 73 environment variables; about 74 platform settings; 27 model purposes.

**The true minimum** is Postgres + API (local orchestrator) + web. It works today with three gaps:

1. There is no resume after a restart.
2. A run fails after one hour, even one waiting for an approval.
3. Tasks run one at a time.

Separately, spend caps refuse every model call without Redis.

**Code:**

- About 51k lines in `src/analystos` and 27k lines of tests.
- 62 tables, 182 API routes.
- 13 capability kinds, 18 agents, 8 methods, 33 skills.

## 4. Donor code quality (measured, not inferred)

**What was run (scratch venv, no donor install):**

1. **Atlas's pure-module tests:** 254/254 passed in 1.4 s. They are behavioural and use no mocks.
2. **Atlas's 106-case adversarial SQL corpus through AnalystOS `validate_sql`:** it found **two real
   gateway gaps**, reproduced for this record:
   - `SELECT fn_send_mail(customer_id) FROM retail.customer` is **accepted** on `postgres` and
     `tsql`. Both dialect profiles are `strict=False` ("increment-3 behaviour, unchanged",
     `gateway/dialects.py`), so an unknown, unqualified function passes. Snowflake, whose profile
     is strict, refuses it. Postgres still runs the query in a READ ONLY transaction, so table
     writes fail, but a function with external effects (dblink, untrusted-language functions,
     mail extensions) is not stopped. SQL Server has no read-only session SQL
     (`config/source_kinds.yaml`) and relies on the login's rights.
   - T-SQL table hints such as `WITH (UPDLOCK, HOLDLOCK)` are copied into the executable SQL.
   - Also accepted: `JOIN … ON TRUE` and comma (Cartesian) joins between authorised tables.
   - `SELECT *` is accepted by design (the gateway expands it to allowed columns).
3. **Prompt-injection screening (Atlas corpus plus its 40-case unseen set):**
   - AnalystOS `skills/catalog.has_injection` catches 10/37 of the corpus and 8/40 of the unseen
     set, with 2/46 false positives.
   - Atlas `screen_text` catches 37/37 and 38/40, with 0/46 false positives.
4. **Redaction:** AnalystOS `llm/redaction.redact` leaves SSNs, IBANs and 9–12 digit account
   numbers in the text sent to models. Atlas `redact_question` catches them, and its tokens can be
   restored locally.
5. **Lineage:** Atlas `parse_view_lineage` stops at CTE names and reports `c.amt → total` with FULL
   confidence. sqlglot's own `lineage()` traces the column to `o.amount`.

**Verdicts** (the full table is in [ADR-0018](../10-architecture/adr/0018-one-platform-donor-repositories.md) §4):

- **Atlas:** carefully tested pure modules, verbose (about half is docstrings). Several are shaped
  by Atlas's rule of never querying source data, so AnalystOS's measured approach is already
  stronger for relationship scoring, key inference and formula signatures.
- **DataPilot:** thinner but weaker. It uses the raw engine (which would break CLAUDE.md rule 4),
  f-string SQL and regex parsing, and has only API-level tests. AnalystOS already has stronger
  staging, profiling, dbt codegen, sandboxing and lineage traversal.
- **Overall:** use the donors as **specifications, corpora and test fixtures**, not code bases.
  A selective port adds about **2.1k lines against 10.4k donor lines**.

**Best value per line:**

1. `injection_defense` and its corpus, about 700 lines. On the unseen set, detection goes from 8 to 38 of 40 attacks.
2. The adversarial SQL corpus as a gateway fixture, about 120 lines. It already found two gaps.
3. `question_redaction`, about 130 lines.
4. The SSRF-safe HTTP call from DataPilot `tool_runtime`, about 110 lines. It also guards the MCP
   client, which has no such guard today, and it must use `not ip.is_global`, because 100.64/10 passes the
   donor check.
5. Weekday and month-end seasonal DQ baselines, about 70 lines.

## 5. Decisions taken from this study

| Question | Decision | Where |
|---|---|---|
| Donor code: port or rebuild? | Mostly rebuild thin, on AnalystOS's own gateway, with donor tests and corpora as the specification. Verbatim ports are limited to four pure modules. | ADR-0018 §4 (revised) |
| AgentSwarms | Patterns only (ELv2); eight patterns folded into ADR-0019/0020/0021/0023, P4-06 and the UI | §2 above |
| Install weight | Lite by default: three containers; everything else is a profile; slim images | [ADR-0025](../10-architecture/adr/0025-lite-by-default.md), P7-16, P7-17 |
| UI weight | One IA (workbench five areas + **Start work**). DS/DE/ML arrive as job kinds and output types, never new top-level screens. Admin behind a gear; one home per concept; one vocabulary; plain language first. | Spec v4 P23 and §15, P7-18 |
| Gateway gaps | P0: strict function allowlist for postgres/tsql, refuse table hints and unconditioned joins, corpus as a CI fixture | P7-15 |
