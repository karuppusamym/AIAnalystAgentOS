# Architecture, market and devil's-advocate review — 2026-09-25

**Kind:** dated evidence snapshot (not a queue). It was first measured against commit `86abd76` of
the PR branch. While it was being written, a peer session landed increment 3 (`124f86a` … `59e85da`:
universal sources, crawler, token economy, admin control plane). Every finding was **re-checked at
`59e85da`**, and §0.1 records what that increment changed. Its claims can go stale without being
wrong. The work it produced lives in the
[tracker, section P, increment 4](../60-delivery/01-tracker.md#increment-4--platform-re-architecture-proposed-2026-09-25).
The target design is [spec v3](../00-intent/03-spec-v3-platform.md) with ADR-0011 to ADR-0015.

**Question asked by the product owner:** did we pick the right approach, and are we building it
right? The goals are:

- An extensible OS: add agents, tools and skills at any time.
- Token cost kept low by working deterministically first. Models and JEV are used only for decisions.
- Context stored in an open knowledge format.
- A replacement for today's analytics tool chain: minimal ETL/ELT on the customer's own Spark,
  dbt or database; BI and dashboards; NL→SQL; analyst and data-science work; monitoring.
- Deployment: **self-hosted enterprise**.

**Inputs:**

- A full read of this repository: 19.0k lines of Python in 133 files, 5.7k lines of tests,
  7.9k lines of web code.
- A read of the two sibling repositories:
  - `AIDataAnalyst` ("Atlas", HEAD `8b48fd9`). It is the most complete Context2AI context layer:
    crawlers, OKF, a context compiler and MCP.
  - `AienginnerAgentOs` ("DataPilot", HEAD `15235dc`). It has the UI and external-gateway patterns,
    plus AI-suggested metadata.
- Web research on the market and on standards, dated 2026-09-25.

Every code claim below was checked against the source. Market claims cite a URL. Anything marked
**[unverified]** rests on secondary or vendor sources.

---

## 0. Verdict in one page

**The core bet is right, and it is the only part of the product that is hard to copy.**
"Models propose, deterministic code computes and checks, people approve side effects" rests on
five things:

- a closed analysis vocabulary;
- statistics computed by SciPy and statsmodels;
- Benjamini–Hochberg correction;
- a verification step that re-runs queries (hash match) and applies a second method;
- approvals bound to a hash.

The market research found **no competitor that publicly documents** pre-registered hypotheses,
multiple-comparison control, effect sizes, or an independent re-derivation of a finding.
Databricks Genie Agent Mode, Snowflake CoWork Deep Research and ThoughtSpot Spotter 3 all market
"hypothesis testing". In each case it means an LLM looping over SQL (§5).

**The platform built around that core does not match what the product claims to be.**

| Claim | Reality in code | Severity |
|---|---|---|
| "Agent operating system", config-driven agents | Agent behaviour is hardcoded Python. The plan is a fixed 14-step list with its keys hardcoded in the engine. YAML fields other than `id`/`tools` are never read. Adding an agent touches about 6 files and needs a deploy (§2 C1). | **High** — this is the owner's main requirement |
| General analytics | Increment 3 added domain-neutral hypotheses driven by column roles, and they found planted retail effects. ITSM-specific code is still in the core, and there is no second-domain benchmark in CI (§2 C2). | Medium (was High) |
| "Cheap by default" | Increment 3 added per-purpose `off/auto/always` modes, a response cache and compaction. Under `token_saver` a run cost $0.023 with 10 calls. But the **default preset still calls the model for every purpose**. Also still true: the prompt is cut at 60,000 characters, there is no prompt caching, four policy fields are never enforced (including `send_data_samples_to_models`), and there is no verified-query or hypothesis registry (§2 C3). | Medium-High (was High) |
| Warehouse analytics | Snowflake, BigQuery, Databricks, Trino, Redshift and ClickHouse are *staged*, not pushed down: the first ≤1,000,000 rows of an unordered `LIMIT` are copied into Postgres. Nothing flags that truncation to findings or verification (§2 C8). | **High** — a correctness issue: findings can describe an arbitrary subset |
| Memory and context | One table. Hash embeddings with no ANN index. Episode memory is written and never read into a prompt. Context2AI results reach no prompt. No export (§2 C4). | High |
| Governed, multi-workspace | Isolation between workspaces rests on the SQL validator alone: one reader role can read every workspace's staged schema. Lineage and Neo4j keys ignore the workspace (§2 C6). | **High** for self-hosted enterprise |
| Scales | Every SSE client blocks the event loop every 0.5 s. CPU-heavy statistics run in 8 threads. Temporal runs a thin polling loop with no heartbeats (§2 C7). | Medium, but the fixes are cheap |
| Replaces ETL/BI tools | One source per run, no write path, Superset only (§2 C8) | Medium, and a strategic gap |
| Analyst-grade UI | An operator console: raw JSON in 15 places, no visual answer inspector, no knowledge UI (§2 C10) | High for adoption |

**Recommendation: keep the core, rebuild the shell.**

1. Turn the pipeline into a real capability platform: manifests, playbooks, method plugins, MCP in
   both directions. See ADR-0011.
2. Put a deterministic-first execution ladder in front of every model call. See ADR-0012.
3. Make an **OKF v0.2 knowledge pack**, plus **Apache Ossie (OSI)** metrics, the system of record
   for context. The database becomes a rebuildable index. See ADR-0013.
4. Push ELT onto the customer's own engines (dbt, Spark, the warehouse) under approval, and never
   build an ETL engine. See ADR-0014.
5. Put JEV behind a decision service with authority classes and calibration. See ADR-0015.
6. Rebuild the UI around five journeys.

**Sequence:** fix the governance and correctness debt first (wave 0). Then build the capability
platform (wave 1), because every later wave plugs into it.

**Strategic warning.** Three sibling repositories now each build a context layer, an SQL gateway, a
JEV client, an approval model and an MCP surface. Spec v1 §11 said "the second application should
not rebuild Context2AI". Atlas already exports pinned OKF v0.2 bundles and serves MCP knowledge
tools. AnalystOS should *consume* them, keeping a minimal built-in crawler for standalone installs,
and should not grow a third context platform (§4).

### 0.1 Re-measured at `59e85da` — what increment 3 changed

Increment 3 ([ADR-0010](../10-architecture/adr/0010-universal-sources-crawler-token-economy.md))
delivered five things:

- a declarative source-kind catalog with one generic SQL connector (15 kinds);
- a deterministic crawler (fingerprints, drift, curation precedence, PII by name and by
  gateway-sampled value, optional batched model enrichment);
- per-purpose model modes `off | auto | always`, a response cache, oversize refusal, a budget
  downgrade and a savings ledger;
- a versioned admin control plane with presets;
- a role-driven, domain-neutral hypothesis block.

It **validates this review's direction**. Under `token_saver`, an analysis of a retail SQLite
database cost **$0.023, 8,048 tokens and 10 model calls, with 15 deterministic skips**, and still
found all three planted effects (`evidence/e2e-increment3-20260925-150550.md`). Compare $0.3186,
88,657 tokens and 41 calls in the v1 run.

Increment 4 builds on it and does not redo it:

| Challenge | Status at `59e85da` | What remains (increment 4) |
|---|---|---|
| C1 "agent OS" | **Unchanged.** `runtime/plan.py`, `agents/dispatch.py` and `runtime/engine.py` did not change. The new `catalog_steward` agent is again YAML plus Python. The admin control plane makes *model settings* change at runtime, which helps. | Capabilities, playbooks, declarative agents, method plugins (P4-X*) |
| C2 ServiceNow-shaped | **Partly resolved.** Role-driven hypotheses found planted retail effects with rules alone. | ITSM specifics are still in the core (`investigator.py:119,173`, `sql_agent.py:83`); domain packs; second-domain benchmark in CI (P4-X07, P4-V01) |
| C3 tokens | **Partly resolved:** modes, response cache, compaction, savings ledger. | The default `balanced` preset still leaves most purposes on `always`, so `auto` should become the default wherever a rule path exists. Also: `compact_json(payload)[:60_000]` still truncates (`agents/common.py:118`); no prompt caching; policy fields unenforced; the per-finding verification call and the publish `risk_check` (`publisher.py:91`) remain; no L1 registries (P4-C01, C05, T01–T05) |
| C4 knowledge | **Storage unchanged.** The crawler now keeps the context store and graph current. | OKF pack as system of record, Ossie, export, compiler (P4-K*) |
| C6 isolation | **Unchanged.** Crawler PII sampling correctly goes through the gateway with `retain_rows=False`. | P4-C02, C03, C11 |
| C7 scale | **Unchanged.** SSE still polls synchronously in an async generator. | P4-C04, P4-S* |
| C8 data plane | **Connectors broadened** to 15 kinds. **New risk:** every kind other than Postgres and T-SQL is staged, including cloud warehouses, as an unordered `LIMIT` of up to 1,000,000 rows (`generic_sql.py:634`, `platform.py:75`), and nothing marks the truncation. | **P4-C12** flags truncation and carries a caveat or fails verification. **P4-E01** validates multiple dialects so warehouses push down. ELT and federation (P4-E*). |
| C9 JEV | **Partly resolved:** in `auto` mode, priority and the stop check use rules. | DecisionService, authority classes, calibration (P4-T08, T09) |
| C10 UI | Catalog, admin settings and crawl panels added; there are now 17 pages. `JsonView` is used 22 times in 7 files, and `api.ts` is 1,554 hand-written lines. | Five-journey IA, generated client (P4-U*) |

---

## 1. What is right — keep and defend

| Decision | Why it is right (first principles) | Evidence |
|---|---|---|
| Models propose, code decides | A language model is a probability distribution over text. It cannot guarantee that a number it states was computed. Only code that ran the query can. So every number must come from code, and the model's job shrinks to the steps where fluency beats computation: framing, hypotheses, prose. | Spec v2 §1. Numbers guard in `agents/insight.py`. |
| Closed analysis vocabulary (ADR-0002) | A hypothesis you cannot compile cannot be reproduced. A compiled spec can be hashed, re-run, benchmarked with planted effects, and diffed across runs. | 9 benchmark functions with planted effects and null controls |
| "Verified" is deterministic (ADR-0008) | Agreement between models is correlated error, not independent evidence. A re-run hash match and a second statistical method are independent. | `agents/critic.py:115` decides `verified` |
| One SQL gateway | Authorization must sit on the only path to the data. A second path makes it a suggestion. | 109-case validator suite |
| Approvals bound to a hash | An approval of "something" can be replayed against something else. An approval of *this payload hash under this plan hash and policy version* cannot. | `governance/approvals.py` |
| Modular monolith + Temporal + Postgres as system of record | At this scale, process boundaries add failure modes without adding safety. Durable execution is what matters for long runs and approval waits. | ADR-0001, ADR-0005 |
| Python core | The statistics and ML ecosystem (SciPy, statsmodels, scikit-learn, Polars, DuckDB, sqlglot) has no .NET equivalent of the same depth. .NET clients should integrate through the OpenAPI/MCP surfaces, not a port. | — |
| Superset as the open BI runtime | For a self-hosted enterprise, "replace Power BI/Tableau" needs a BI engine the customer can run. Superset 6.1 is Apache-2.0 and now ships an MCP service and an extensions framework ([Preset, 2026-06-11](https://preset.io/blog/apache-superset-6-1-release/)). The *dashboard definition* must be ours and portable (§3.8). | live Superset 4.1.1 publish |
| JEV for bounded choices | Typed answers with probabilities are auditable, and output tokens are free. It has cost about $0.00001–0.00002 per call in our runs. | ADR-0006, run evidence |

---

## 2. Devil's advocate — the twelve challenges

Each challenge gives the claim, the evidence, why it matters, and the decision (the tracker row
that fixes it).

### C1. "It is an agent OS." — It is a hardcoded pipeline.

**Evidence**

- `runtime/plan.py:15-30`: `BASE_STEPS` is a fixed list of 14 steps.
- The engine hardcodes step keys in five places: `REPLAN_RESET`, `DOWNSTREAM_OF_VERIFY`
  (`engine.py:264`), the `plan_approval`/`publish` gates (`engine.py:104,141`), publish-skip
  (`engine.py:60`) and `tasks["publish"]` in finalize.
- `agents/dispatch.py:24-45` is a literal dict plus two prefix checks.
- The only agent-YAML fields read at runtime are `id`, `tools` and `prompt_version`, the last as a
  label only. `capabilities`, `skills`, `model_profile`, `policies.*` and `verification_required`
  are never read. Seven skill names in YAML refer to skills that do not exist.
- `POST /api/agents` stores an agent that nothing can ever dispatch.
- A tool stored with `POST /api/tools` cannot execute, because `ToolRuntime.invoke` needs a Python
  callable from agent code.
- Settings, the model config and the router are `@lru_cache`d, so changing them needs a restart.

**Why it matters.** The owner's main requirement is "add agents, tools, skills anytime". Today:

- a new analysis method touches about 10 files;
- a new agent about 6 files plus a deploy;
- a new connector 5 files including the UI;
- a new BI destination about 6 files with `"superset"` hardcoded in 4 places.

**First principle.** *The unit of extension must be the unit of governance.* If a capability
declares its inputs, outputs, side-effect class, cost class and determinism, the policy engine can
govern a capability it has never seen. If it does not, every addition needs a code review of the
engine.

**Decision.** Build a capability manifest and registry with discovery (entry points, directory
packs, MCP). Replace hardcoded plans with playbooks, and give gates their own step types.
Declarative agents run in a generic "propose → validate → execute" runtime. Analysis methods
become plugins. See ADR-0011 and rows P4-X01 to P4-X08.

### C2. "It analyses enterprise data." — It is shaped around ServiceNow.

**Evidence**

- The heuristic hypotheses in `agents/investigator.py:129-199` key on `made_sla`, `priority`,
  `ci|cmdb` and reassignment buckets.
- `agents/sql_agent.py:83` expects `u_name`.
- The glossary is a Python tuple list in `cli.py:21`.
- `services/sources.py:17` lists `caller` as a PII hint.
- Every benchmark and live run uses synthetic ServiceNow data.

**Why it matters.** On a sales or finance dataset the deterministic fallback disappears and the
product becomes "LLM hypotheses only", which is exactly the competitor weakness we sell against.

**Update at `59e85da`.** Increment 3 added a domain-neutral hypothesis block driven by the
crawler's column roles. On a retail database it found every planted effect with rules alone, so the
"LLM only" risk is much smaller. What remains is the ITSM code in the core
(`investigator.py:119,173`, `sql_agent.py:83`) and the fact that only one live run shows a second
domain; there is no benchmark in CI.

**Decision.** Build **domain packs**: knowledge, hypothesis templates, glossary and PII hints as
data. Move ServiceNow into `packs/itsm`, and prove a second domain against a benchmark with planted
effects (P4-X07, P4-V01).

### C3. "Cheap by default." — Tokens go where they change nothing.

**Evidence.** The evidence run `e2e-20260925-054942` made 41 model calls: 22 chat and 19 JEV. It
used 88,657 tokens, cost $0.3186 and produced 5 verified findings.

**Per-finding calls with little effect on the outcome.** Each finding gets three calls:

- `insight_narrative`: a template exists, and the model text is thrown away whenever it fails the
  numbers guard.
- `verification` (another model family) and `rev_second_opinion` (JEV): together they move
  confidence by only ±0.05–0.1 (`critic.py:120-125`).

**Calls that cannot change anything:**

- `risk_check` at publish: the tier is hardcoded to high and an approval is always required.
- `chart_selection`: the rules already decide the chart.
- `planning`: its output only seeds hypothesis generation, and its exceptions are swallowed.

**Payload.** `catalog_for_prompt` (`agents/common.py:38-65`) sends every in-scope column with its
statistics and up to 12 top values. It is re-sent to planning, hypothesis generation, every
follow-up round, `sql_generation` and every `sql_repair`. The payload is then truncated by
`json.dumps(payload)[:60_000]` (`common.py:76`; at `59e85da`, `compact_json(payload)[:60_000]` at line 118),
which can hand the model malformed JSON.

**Caching.** There is no `cache_control` anywhere. `request_hash` is computed and stored but never
looked up. There is no verified-query library. Scheduled re-analysis calls the LLM again for new
hypotheses on unchanged data; the first Phase-3 pass reported 5 findings "resolved" on identical
data.

**Unenforced policy fields.** `send_data_samples_to_models`, `allowed_providers`,
`expensive_model_approval_usd` and `data_residency` are defined in `contracts/policy.py:20-24` and
referenced nowhere else. For a self-hosted enterprise buyer, that promise failing silently is a
trust problem, not a cost problem.

**First principle.** A model call is worth its price only if **(a)** its output can change what
happens next, and **(b)** no cheaper rung can produce that output as reliably. The rungs, cheapest
first:

1. cache
2. registry
3. rules
4. typed decision
5. small model
6. large model

Cost ≈ calls × tokens per call × price per token. Remove calls that fail (a) first. Then cut
tokens per call by sending only relevant context in a cache-stable order. Only then negotiate
price by routing to smaller models.

**Decision.** Increment 3 (ADR-0010) already provides per-purpose `off/auto/always` modes, the L0
response cache and a savings ledger. Build on them rather than beside them:

- generalise the modes into an execution ladder per purpose, with the rung that answered recorded;
- make `auto` the default wherever a rule path exists.

Then:

- delete the no-effect calls;
- add a context compiler with budgets and receipts;
- lay prompts out so they stay cache-stable;
- add a verified-query and hypothesis registry, which scheduled runs replay with 0 LLM calls;
- add a CI cost gate.

See ADR-0012 and P4-T01 to P4-T10.

**Targets (not claims):**

- ≤ 10 chat calls per standard run;
- 0 chat calls for a scheduled re-analysis with no novelty exploration;
- ≥ 60% of prompt tokens served from cache on providers that support caching.

### C4. "Layered context and memory." — Knowledge is locked in, leaky, and partly unused.

*At `59e85da` the crawler (ADR-0010) keeps the context store and graph current. The storage model,
the embeddings and the gaps below are unchanged.*

**Evidence**

- **Storage.** One `context_entry` table holds terms, notes, episodes and metrics. Rows with
  `workspace_id NULL` are shared across tenants. Embeddings are 256-dimension feature hashing and
  the dimension is hardcoded (`db/models.py:29,170`). There is **no ANN index**, so every search
  is a sequential scan.
- **Crowding.** Glossary retrieval takes the top 12 of *all* kinds and only then drops episodes
  (`context/service.py:84,96`). Every run writes an episode containing its objective, so recurring
  runs crowd out glossary terms.
- **Unused memory.** Episode memory is written (`supervisor.py:88`) and never enters a prompt.
- **Context2AI.** The adapter's `external` results land in an artifact only, never in a prompt.
  `metrics()` is never called and no auth token is sent.
- **No bulk export or import.**

**Why it matters.** Knowledge is the asset that compounds. It must outlive the software that holds
it, be reviewable by people, diff cleanly, and move between tools (Atlas, dbt, Superset,
Snowflake, Databricks).

**First principle.** *The system of record for knowledge should be a format people can read and
review, and that other tools accept.* The database is an index over it, and an index can be
rebuilt.

**Decision.** A knowledge pack in **Google's Open Knowledge Format v0.2**, pinned. Its concept
types include "Attested Computation", which maps almost one-to-one onto a verified finding.

- **Metrics** in **Apache Ossie** (formerly OSI), pinned to 0.1.1 because dbt Core 1.12 imports
  0.1.x.
- **Dataset contracts** in **ODCS v3.2**.
- **Provenance** as **OpenLineage** events.
- **The index** is BM25 plus pgvector HNSW, rebuildable from the pack.
- **Consumption** from Atlas as OKF import or through its MCP knowledge tools.

See ADR-0013 and P4-K01 to P4-K10.

### C5. "Verified findings." — The rigour is real, the evidence is narrow and stale.

**Evidence**

- Benchmarks cover planted effects on ServiceNow-shaped data only.
- There is no accuracy benchmark for Ask (NL→SQL), and no JEV calibration data.
- The v1 end-to-end evidence was produced **before** commits `55b1eef` and `1e8aa20`. Its KPI table
  shows the duplicate-metric and ×100-percentage defects those commits fixed, and it has not been
  re-run at HEAD. Run duration is not recorded.
- The Phase-3 alert list shows the same alert 3 times, including a variant at 100× scale.
- There are no API router tests. The engine's `execute_task` and `get_state` are covered only by 2
  integration tests that skip when the stack is down.

**Lesson from Atlas.** Its own 2026-09-11 review found "four evaluation mechanisms … none measures
whether an answer is correct" (`AIDataAnalyst/Docs/review-2026-09-11/REVIEW.md`). Do not repeat
that.

**Decision.** Rows P4-C08 (re-run at HEAD), P4-C10 (router and engine tests), P4-V01
(multi-domain analytical benchmark) and P4-V02 (Ask accuracy with a real model).

### C6. "Governed, per-workspace isolation." — Isolation has a single layer.

**Evidence**

- `staging/loader.py:178-179` grants the single `analystos_reader` role SELECT on every staged
  `src_*` schema, whichever workspace owns it. The SQL validator is therefore the *only* barrier
  between workspaces on the analytics plane.
- The `lineage_edge` unique constraint `(from_type, from_id, relation, to_type, to_id)`
  (`db/models.py:434`) omits `workspace_id`. Combined with `on_conflict_do_nothing`, a
  name-identified edge that already exists in another workspace is silently dropped.
- Neo4j nodes are merged on `(type, id)` and `workspace_id` is overwritten on every projection
  (`graph/projection.py:47-53`), while the neighbourhood query filters on it.
- Tool-gate coverage:
  - Skill SQL runs through `ctx.run_sql` (`runtime/context.py:123-141`) without the tool gate.
    The gateway still enforces scope.
  - The critic's verification re-runs call `gateway.execute` directly (`agents/critic.py:79`),
    which **bypasses the per-run query budget**.
  - Spec v2 §6 ("every invocation is policy-checked") overstates this.

**Why it matters.** For a self-hosted enterprise, "defence in depth" is a procurement question.
One validator bug would become a data leak across workspaces.

**Decision.** P4-C03 adds per-workspace reader roles, puts the workspace in every uniqueness and
graph key, and adds negative tests across workspaces. P4-C02 closes the budget bypass. P4-C11 makes
tool-gate coverage match the spec, or makes the spec say exactly which paths are exempt.

### C7. "Temporal and a modular monolith scale." — Only after cheap fixes.

**Evidence**

- **SSE.** `api/routers/analysis.py:159-178` is an `async` generator that makes a *synchronous*
  SQLAlchemy call every 0.5 s per client, which blocks the event loop.
- **Redis pub/sub.** Events are published (`events/bus.py:44`), but there is no subscriber.
- **Worker.** 8 threads (`workflows/worker.py:21`) run CPU-heavy statistics: a 150-tree forest
  with `n_jobs=1`, and 2,000 resamples.
- **Temporal.** The workflow is a polling loop with no heartbeats, a 30-minute `start_to_close`
  equal to the claim TTL, no `continue_as_new`, and a `FOR UPDATE` on the run row in every loop.
- **Per-statement budget check.** Every statement runs `COUNT(*)` on `query_execution`.
- **Lineage reads.** `lineage_for` loads every edge in the workspace on each artifact GET.
- **Neo4j.** It is used only for a best-effort projection and a neighbourhood query whose output
  feeds nothing.

**Decision.** Wave 6 (P4-S01 to P4-S05):

- Temporal task queues per workload (analysis, compute, publish, crawl, ELT), with heartbeats;
- a process pool for statistics;
- non-blocking SSE fed by LISTEN/NOTIFY or Redis;
- budget counters in Redis;
- Neo4j optional and off by default;
- a production database topology and a Helm chart.

The monolith stays. ADR-0001 was right that workers split behind existing interfaces.

### C8. "Replaces the tool chain." — It cannot move or join data yet.

**Evidence**

- One source per run (`services/runs.py:35-36`).
- Sources are read-only by design, and nothing can write *anywhere*, including the customer's
  warehouse.
- Staged sources reload a full snapshot with a 200k-row cap (`connectors/servicenow.py:153`).
- Superset is the only BI destination.
- **Added at `59e85da`: warehouses are copied, not queried.**
  - Pushdown needs a dialect the gateway validator understands (postgres, tsql), so Snowflake,
    BigQuery, Databricks, Trino, Redshift and ClickHouse are *staged*.
  - `GenericSQLConnector.extract` runs `SELECT … LIMIT max_rows` with no `ORDER BY` and no sampling
    (`connectors/generic_sql.py:634`), capped at `staged_max_rows = 1,000,000`
    (`contracts/platform.py:75`).
  - For any larger table, the analysis runs on an arbitrary, non-random subset: often the oldest
    partitions, which may miss the most recent period entirely.
  - Nothing marks the snapshot as truncated: there is no truncation flag in `staging/`,
    `services/sources.py`, `agents/insight.py` or `agents/critic.py`.
  - So a finding can be "verified" (reproducible, second method agrees) and still describe the
    wrong population.
  - **First principle:** a statistical claim is about the population you sampled. An unordered
    `LIMIT` is not a sample of anything defined.

**The owner's answer.** Minimal ETL/ELT, using any Spark environment to run and load, and dbt or
any database for ELT.

**First principle.** *Move the computation to the data, not the data to the computation.* Own the
**plan** (what to build, why, with what tests), not the **engine** (Spark, dbt, the warehouse
already exist and are already governed by the customer). An ETL engine of our own would compete
with free, mature ones and add a data-movement risk that our governance model then has to cover.

**Decision.**

- **Immediately (P4-C12, P0).**
  - Record `truncated` and the row count against the source's total on every staged snapshot.
  - Propagate a "population" caveat into findings, and fail a new `representative_population`
    check in verification when the snapshot is truncated without a declared sampling method.
  - Replace the unordered `LIMIT` with a declared strategy: the full table, a time window, or the
    engine's native random sample (`TABLESAMPLE` or equivalent).
- **Engine abstraction.**
  - Wraps the increment-3 `GenericSQLConnector`, adding **multi-dialect validation**: sqlglot
    already parses these dialects, and each one gets its own validator security suite.
  - Warehouses then push down instead of being copied.
  - Implementations: Postgres, SQL Server, DuckDB, Trino, Spark (Spark Connect or Databricks SQL),
    Snowflake and BigQuery.
  - Each engine is certified against a live instance before it is advertised.
- **dbt builder.** Emits a dbt project (models, tests, Ossie metrics) from the virtual dataset and
  the KPIs, dry-runs it, and executes it on the customer's runner only under a hash-bound approval.
- **Write-path gateway.** A separate identity and a cost estimate.
- **Federation** (Trino or DuckDB) for cross-source runs.
- **dlt is a spike, not a decision**, for incremental ingestion from non-SQL sources.

See ADR-0014 and P4-E01 to P4-E06.

### C9. "JEV only escalates." — It also steers the analysis, and it is an alpha dependency.

**Evidence: JEV decides beyond escalation**

- `stop_check` ends the investigation early (`investigator.py:364-366`).
- `chart_selection` overrides the chart at p ≥ 0.6.
- `feedback_classification` picks the replan path.

**Evidence: calls that are wasted or harmful**

- `risk_check` at publish has no possible effect (p=0.98 on a platform-generated string).
- `alert_triage` escalated a "> 0.0" threshold and a dip caused by an incomplete period to
  critical (p 0.85–0.90).

**Evidence: what is not recorded**

- Probabilities are not persisted. Hypothesis priority keeps only `priority_by="jev:<model>"`.
- `model_call` stores neither prompt nor answer, so decisions cannot be replayed.

**Evidence: the endpoint**

- The Decisions API is `/api/alpha/`, has a single vendor and a 32k context, and publishes no
  calibration data.
- A pydantic-ai issue reports about 15% timeouts on the alpha endpoint
  ([pydantic-ai#8552](https://github.com/pydantic/pydantic-ai/issues/8552), **[unverified]**).
- A self-hosted, air-gapped customer cannot reach OpenRouter at all.

**First principle.** A probability is useful only if it is *calibrated*: when JEV says 0.8, the
event must happen about 80% of the time. Until calibration is measured on our own labelled
outcomes, a JEV probability may rank or escalate. It may not decide analysis depth.

**Decision.** Put JEV behind a `DecisionService` with these backends: JEV, rules, a local
classifier, or structured output from a small model.

- **Authority classes per purpose:** rank, choose-presentation, escalate-only, bounded-early-stop,
  route.
- **Operations:** persisted decisions with probabilities, a circuit breaker, a 3 s timeout.
- **Calibration:** Brier score and ECE per purpose. A purpose may use JEV beyond "rank" only above
  a calibration threshold.
- **Changes to current purposes:** drop `risk_check` at publish; move `chart_selection` to rules;
  run `alert_triage` only after deterministic materiality rules (completeness, minimum effect).
- **New JEV uses where it fits:**
  - Ask: choose between a verified query and generation;
  - clarification needed? (escalate to the user);
  - pick one of several valid metric matches or join paths.

See ADR-0015 and P4-T08, P4-T09.

### C10. "Analyst product UI." — It is an operator console.

**Evidence**

- At `86abd76`: 15 pages in about 7.9k lines, with raw `JsonView` in 15 places across 6 files.
  At `59e85da`: 17 pages, with `JsonView` in 22 places across 7 files.
- The agent console is not live.
- Feedback kind `question` is stored and never answered.
- The Sources form lacks SQL Server.
- `api.ts` has 1,554 hand-written lines at `59e85da`, with no OpenAPI codegen.
- Studio lists up to 500 artifacts without paging.
- There is no knowledge, semantic or metric UI, and no conversational Ask with visual answers.

**Siblings, to learn from and not copy**

- **Atlas does well:**
  - streamed stages in plain language;
  - provenance pills and "changed since published" staleness;
  - one refusal state per kind, each with a remedy;
  - ambiguity shown as two competing definitions;
  - a Ctrl/Cmd-K palette;
  - "never show an unknown as 0";
  - axe and Playwright accessibility tests.
- **Atlas does badly:** no charts at all, 40 screens against a review target of 19, and heavy
  governance jargon.
- **DataPilot does well:**
  - a 3-panel conversation with an inspector bound to the selected answer
    (Result/SQL/Context/Decision tabs, "Escalated by Jev");
  - paste-SQL explain;
  - an AI-suggested-metadata review banner;
  - a semantic graph with governed joins solid and inferred joins dashed.
- **DataPilot does badly:** a static, hardcoded architecture page, a 5,631-line CSS file, 19 flat
  nav items, and tests that are regex over source.

**Decision.** Spec v3 §9 defines five journeys (Ask, Investigate, Knowledge, Build, Operate) with at
most 20 screens.

- Visual answers with an evidence inspector.
- An investigation board with a "why trust this" drawer.
- A knowledge studio over OKF.
- A **capability-driven UI**: forms generated from JSON Schema plus a registry of renderers, so a
  new capability needs no new screen.
- A client generated from OpenAPI.

Rows P4-U01 to P4-U07.

### C11. "Continuous analytics says what changed." — Only if the questions stay the same.

**Evidence.** Pass 1 on unchanged data reported 2 new, 1 changed and 5 resolved findings. After the
carry-forward fix, pass 2 still reported 2 "new" findings, because the LLM invents new hypotheses
on every run. Profiles run at temperature 0.2.

**Decision.** A **hypothesis registry**. A scheduled re-analysis replays the registered hypothesis
set deterministically. Novelty exploration is an explicit option with its own budget, and its
findings are labelled "new question" rather than "new finding" (P4-T05).

### C12. "Replace everything." — The over-build trap.

**Evidence from Atlas's own review** (`AIDataAnalyst/Docs/review-2026-09-11/REVIEW.md` §1):
"~415K lines … 44 screens, 471 endpoints, ~200 tables, 243 settings and 19 compose services". It
had 101 routes used by nothing, and "the core journey still breaks at the payoff". Replacing five
tool categories at once is the fastest way to reproduce that.

**Decision: replace by absorption, in this order.**

1. **Analyst and data-science work plus monitoring.** This is differentiated; nobody else does
   rigorous inference.
2. **Ask (NL→SQL)** on the same knowledge pack and verified-query registry. This is table stakes,
   but cheap once the knowledge layer exists.
3. **BI** through the bundled Superset, with dashboard-as-code bundles that stay portable.
4. **ELT** by generating dbt or Spark work that runs on the customer's engines.

Each step reuses the capability platform, so each adds capabilities, not subsystems. There is a
**budget of at most 20 screens** and a rule that every new endpoint ships with a UI consumer or an
MCP or API consumer.

---

## 3. Sibling projects — what to adopt, and how to do it better

| Area | Atlas (AIDataAnalyst) | DataPilot (AienginnerAgentOs) | AnalystOS target |
|---|---|---|---|
| Knowledge format | OKF v0.2 bundle pinned to an upstream commit (`src/aida/okf_export.py`, `Docs/90-reference/okf-export-profile.md`). Import is off by default. OSI output is a placeholder (ADR-0022). | Frictionless Data Package only (`app/frictionless.py`) | OKF pack as **system of record** (not just an export), real Ossie metrics with conformance tests, findings as Attested Computations |
| Context selection | Deterministic, budgeted, section-level excerpts with receipts, `NO_MATCH` and a list of what was omitted (`src/aida/okf_context.py`) | Top-N truncation; "summary" = last 12 messages cut to 360 characters | Atlas's algorithm plus hybrid RRF (BM25 + HNSW) plus a layout that stays stable for prompt caching; receipts shown in the UI |
| Compile to many targets | `context_compiler.py` → MCP, REST, YAML, OSI, ODCS, Snowflake Semantic View, Databricks Metric View, each hashed with a drift check | — | The same idea, but only targets with a conformance test ship |
| Crawlers | 6 BETA connectors; facet-level failure; value-free profiling; fingerprints and renames; query-history mining; dbt, OpenLineage, Tableau and Power BI lineage | Columns only (capped at 5000); drift on rescan with acknowledgement; scans on demand only | Increment 3 already has a deterministic crawler: fingerprints, drift, curation precedence, PII by name and by value, scheduled, and incremental by default (ADR-0010). Still to add: query-history mining, dbt manifest, Superset metadata, facet-level failure, and OKF documents as output (P4-K06) |
| AI metadata | Model only for columns with little evidence, batched; the 0/196 bar cleared on a thin catalog is a warning | Schema only; drops invented columns; never overwrites human text; `ai_suggested` → `reviewed` | Increment 3 already fills placeholders only, below confidence 0.6, in screened batches, never overwriting `user`/`reviewed` text. Still to add: per-field provenance and confidence, and a batch review queue, so that rejections become negative knowledge (P4-K07) |
| Verified queries | "Replay verified SQL" was CANCELLED for value-freedom reasons | Exact reuse (0.72 s vs 8.2 s) plus a SQL cache keyed on the grounding signature | A parameterised verified-query registry; tool-first planning that declines a tool when a required input is missing (Atlas `GovernedPlanner` lesson) |
| JEV | Escalate-only; a failure means "no signal"; no route approved | Decision purposes only; trusted-text-only state; 3 s timeout; can be routed locally for residency | DecisionService with authority classes, calibration and persisted probabilities |
| Extensibility | Registered in code; ADR-0008 bans agent frameworks; tools SDK exists; hand-rolled 4k-line MCP server | Agents are DB rows gated by an eval score of 0.8 or more; HTTP/query tools are config-only; SSRF-hardened HTTP runtime | Manifests plus entry points plus MCP. Adopt the eval gate for publishing an agent and the SSRF runtime for HTTP tools. Use the MCP SDK rather than hand-rolling. |
| External gateway | MCP server with context products as prompts | Grant-scoped `client_id.secret`, per-tool quota, OpenAPI per client, MCP `tools/*` only (no resources) | MCP server with **resources** (knowledge, findings, metrics) and tools, grant-scoped (the DataPilot pattern); A2A agent card later |
| UI | See C10 | See C10 | Five journeys, visual evidence-first answers, capability-driven forms |

**Anti-patterns to avoid**, each already observed in a sibling:

- a vector index without a tenant key (DataPilot `vector_store.py`);
- unversioned metadata (DataPilot);
- truncation passed off as a "summary" (DataPilot);
- one global cost rate (DataPilot);
- features built and then left off by default (Atlas S9 and F04);
- a declared cap that nothing enforces (Atlas `agent_budget.py`). AnalystOS has four of these
  today (C3).

---

## 4. Strategic finding — three repositories, one context platform

Six things are duplicated, with more or less the same contracts:

- a context/metadata layer (Atlas: comprehensive; DataPilot: grounding; AnalystOS: `context/`);
- a SQL gateway with a sqlglot guard (all three);
- a JEV client with escalate-only rules (all three);
- hash-bound approvals with separation of duties (all three);
- an MCP surface (Atlas and DataPilot).

Spec v1 §11 already decided that AnalystOS "should not rebuild Context2AI".

**Recommendation.** Make the *interchange formats* the integration contract, not shared code:

1. **OKF v0.2 bundles**, a shared profile pinned by commit, for knowledge.
2. **Ossie 0.1.1** for metrics.
3. **MCP** for live context tools: Atlas already serves `get_knowledge_context` and
   `get_object_knowledge`.
4. A shared **decision-purpose schema** and **approval-hash format**, written down in each repo's
   contracts folder.

AnalystOS ships a *minimal* crawler so a standalone self-hosted install works without Atlas. When
Atlas (Context2AI) is present, AnalystOS imports its bundles or calls its MCP tools and does not
crawl twice (P4-K09, P4-G01).

---

## 5. Market — where this product can win, as of 2026-09-25

**What every major platform shipped in 2026:** an agentic analyst over *its own* data.

- **Snowflake.** CoWork (formerly Intelligence) with Deep Research; semantic views with
  `AI_VERIFIED_QUERIES`; AI Credits since 2026-04-01.
  ([docs](https://docs.snowflake.com/en/release-notes/2026/other/2026-04-05-semantic-views-verified-queries);
  [Summit recap](https://atlan.com/know/snowflake/summit-2026-announcements/) **[secondary]**)
- **Databricks.** Genie Agent Mode "tests hypotheses" by running multiple SQL queries; Unity
  Catalog Metrics GA with OSI support.
  ([Genie agent mode](https://docs.databricks.com/aws/en/genie/agent-mode);
  [UC at DAIS 2026](https://www.databricks.com/blog/whats-new-unity-catalog-data-ai-summit-2026))
- **ThoughtSpot.** Spotter 3 with root-cause analysis; SpotIQ drivers.
- **Tableau Next.** An Inspector agent for monitoring.
- **Microsoft.** Power BI Copilot and Fabric data and operations agents.
- **Google.** Looker Conversational Analytics GA, plus Code Interpreter.
  ([Next '26](https://cloud.google.com/blog/products/business-intelligence/looker-updates-for-agentic-bi-at-next26))
- **Amazon.** Quick, with agent-hour pricing. **[secondary]**

**Open source and startups**

- Wren AI is active under Apache-2.0, but gates RLS/CLS and dashboards behind its commercial
  edition.
- Vanna is archived and Dataherald is dormant.
- Metabase open-sourced its AI features in v60.
- Superset 6.1 ships an MCP service.
- Tellius Kaiya does statistical driver decomposition. It is the closest to our differentiator.

**Who really does inference?**

- Formal tests: Julius, on request and ungoverned.
- Statistical driver decomposition: Tellius and SpotIQ.
- Everyone else: LLM loops over SQL.
- **Nobody** publicly documents pre-registration, multiplicity control, effect size or power, an
  independent re-derivation, or a reproducible finding bundle gated by approval.
- Research backs the concern: LLM agents are unreliable hypothesis testers
  ([arXiv 2608.07437](https://arxiv.org/pdf/2608.07437)).

**White space, in priority order**

1. **Findings that survive audit.** Deterministic inference and verification sold as the product,
   not a feature.
2. **An open evidence bundle.** Ossie deliberately has no provenance, freshness or approval fields.
   OKF v0.2 has `verified`, `stale_after` and Attested Computation. AnalystOS can be the reference
   producer of OKF evidence bundles that link Ossie metrics, OpenLineage runs and approvals.
3. **Warehouse-neutral and self-hosted.** Genie is tied to Unity Catalog and CoWork to Snowflake,
   and the open-source field is thin.
4. **An approval-gated analytics lifecycle.** Propose → verify → approve → publish → monitor, where
   monitors *re-test hypotheses* instead of only thresholds.
5. **Predictable cost per investigation.** Incumbents moved to metered AI in 2026. A
   deterministic-first ladder with hard budgets is a message for the CFO.

**Threats, and the answer to each**

1. **Bundled, near-free incumbents where the data already lives.** Interoperate: Ossie in and out,
   MCP both ways, run *on* Databricks and Snowflake engines. Do not ask customers to move data.
2. **Fast followers on "why" analysis.** Our moat is rigour plus governance plus the evidence
   format, not orchestration. Invest there first.
3. **Standards churn.** Ossie went to 0.2.0.dev0 while dbt accepts only 0.1.x, and was renamed
   within 10 months. OKF went from v0.1 to v0.2 in about a month and moved repositories. MCP
   2026-07-28 deprecated Sampling and Roots. Pin versions, keep adapters, and run conformance tests.
4. **Dependency on the alpha JEV.** See C9: a decision service with deterministic fallback and a
   local backend for air-gapped installs.
5. **Commoditised agent plumbing.** General agents plus 46 products compatible with Skills plus
   vendor MCP servers let customers assemble flows themselves. Be the *governed analytics
   capability* that those agents call (our MCP server), not another general agent.

**Standards status used by this review**

- **OKF.** Announced 2026-06-12, v0.2 in July 2026, Apache-2.0.
  [Spec](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md),
  [blog](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing).
  Adoption is thin, and it is not a W3C standard.
- **Apache Ossie (incubating, formerly OSI).** Spec 0.1 published 2026-01-27; entered the incubator
  2026-07-10. [repo](https://github.com/apache/ossie);
  [dbt 1.12 import](https://docs.getdbt.com/docs/build/ossie-semantic-models).
- **Others:**
  - dbt MetricFlow is Apache-2.0 (2025-10-14).
  - ODCS v3.2 and ODPS v1.1 were released 2026-09-08
    ([entropy-data](https://www.entropy-data.com/news/2026-09-08-odcs-3-2-odps-1-1)).
  - The OpenLineage Python client is at 1.53.0.
  - MCP spec 2026-07-28 ([blog](https://blog.modelcontextprotocol.io/posts/2026-07-28/)) includes
    MCP Apps.
  - A2A v1 joined AAIF on 2026-08-27.
  - Agent Skills (SKILL.md) was proposed for transfer to AAIF on 2026-09-24
    ([proposal](https://github.com/aaif/project-proposals/issues/47)).
- **JEV and the Decisions API** are documented as OpenRouter community guides and are **alpha**
  ([docs](https://openrouter.ai/docs/guides/community/jev)).

---

## 6. Scorecard

| Dimension | Today | Target (spec v3) | Rows |
|---|---|---|---|
| Add a skill, tool or MCP tool | Code in 3+ places plus a deploy | A manifest in a pack, or a registered MCP server; enabled per workspace once certified | P4-X01, X05 |
| Add an agent | About 6 files plus a deploy | A YAML agent under the generic runtime; Python only for new behaviour | P4-X03 |
| Add an analysis method | About 10 files | One method module plus a manifest | P4-X04 |
| New domain | Role-driven rules work on retail (increment 3); ITSM code is still in the core | A domain pack (data), with its benchmark in CI | P4-X07, V01 |
| Model calls per run | 41 (v1, default modes); 10 under `token_saver` (increment 3) | `auto` by default wherever a rule path exists; ≤ 10 by default; 0 for a scheduled replay | P4-T01, T02, T05 |
| Prompt caching | None | Stable prefix, ≥ 60% of tokens cached | P4-T04 |
| Knowledge format | Proprietary table | OKF v0.2 pack plus Ossie plus ODCS plus OpenLineage | P4-K01 to K04 |
| Isolation between workspaces | Validator only | Per-workspace roles plus validator plus keys that include the workspace | P4-C03 |
| Warehouse sources | Staged as an unflagged, unordered `LIMIT` of up to 1M rows | Pushdown through multi-dialect validation; declared sampling; truncation fails verification | P4-C12, E01 |
| Cross-source and ELT | None | Federation plus dbt or Spark on customer engines, approval-gated | P4-E01 to E06 |
| Air-gapped install | Only the `offline` preset works, with no model at all (increment 3); no local-model provider | OpenAI-compatible local models plus rules or a local decision backend | P4-S04, T08 |
| UI | Operator console, 17 pages | Five journeys, at most 20 screens, capability-driven | P4-U01 to U07 |
