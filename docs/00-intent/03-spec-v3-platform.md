# Context2AI AnalystOS — Specification v3 (platform)

**Status:** proposed, 2026-09-25. Rows move to Done only through the
[tracker](../60-delivery/01-tracker.md), increment 4.

**Relationship to v2:**

- v2 ([`02-spec-v2.md`](02-spec-v2.md)) stays authoritative for *what must stay true*: the
  principles P1–P9, the governance invariants, the verification semantics and the run lifecycle.
- v3 decides *how the platform becomes extensible, cost-efficient, open-format, multi-engine and
  scalable*, for a **self-hosted enterprise** deployment.
- Where v3 changes a v2 section, the section says so.

**Why v3 exists:** the [2026-09-25 architecture review](../70-reviews/2026-09-25-architecture-review.md).

**Builds on increment 3** ([ADR-0010](../10-architecture/adr/0010-universal-sources-crawler-token-economy.md)):

- the source-kind catalog and generic SQL connector;
- the deterministic crawler;
- per-purpose `off/auto/always` modes, the response cache and the savings ledger;
- the admin control plane.

v3 extends those pieces and does not replace them. Each section below says which increment-3
piece it builds on.

**Decisions:** ADR-0011 to ADR-0015 in [`../10-architecture/adr/`](../10-architecture/adr/).

---

## 1. Positioning

> **AnalystOS is the governed analytics OS that runs on your stack.** You state a business problem.
> Agents propose; deterministic code tests, verifies and computes. You approve what leaves. The
> results are findings that survive audit, KPIs, dashboards, monitors and data products. The
> knowledge they rest on is kept in open formats you own.

**What it replaces, in the order it absorbs them.** Each item is a set of capabilities on one
platform, not a new subsystem.

1. **Analyst and data-science work, and monitoring.** Hypotheses, statistical tests, verification,
   root cause and reports. This is the differentiator.
2. **NL→SQL Q&A (Ask).** It runs on the same knowledge pack and the same verified-query registry.
3. **BI and dashboards.** The bundled Apache Superset is the BI runtime. Dashboards are portable,
   hash-bound *bundles* (dashboard-as-code) with adapters for other destinations.
4. **ETL/ELT, the minimum needed.** AnalystOS generates the plan: dbt models, Spark SQL, tests and
   metrics. It runs them on the customer's engines (dbt, Spark/Databricks, Snowflake, Postgres,
   Trino) under approval. **AnalystOS never becomes an ETL engine.**

**Non-goals.**

- A proprietary ETL engine or scheduler for data pipelines.
- A proprietary semantic-layer language.
- A general-purpose chat agent.
- Moving customer data into AnalystOS when the customer's engine can compute in place.

## 2. Principles added to v2 §2

| # | Rule | Enforced by |
|---|---|---|
| P10 | **Deterministic first.** Every purpose declares an execution ladder: cache → registry → rules → typed decision → small model → large model. A higher rung runs only when every lower rung has abstained. The rung that answered is recorded. This generalises the increment-3 modes: `off` = rungs L0–L3 only, `auto` = the full ladder, `always` = skip to the model. **`auto` is the default wherever a rule path exists.** | `llm/router.py` + `model_gate` (increment 3), extended by `llm/ladder.py`; ADR-0012 |
| P11 | **Everything is a capability.** Agents, skills, tools, analysis methods, connectors, engines, publishers, decision purposes, detectors, crawlers, knowledge packs and playbooks are all registered through one manifest contract that declares side effects, cost and determinism. The policy engine governs capabilities it has never seen. | `capabilities/` (new); ADR-0011 |
| P12 | **Open formats at every boundary.** Knowledge: OKF. Metrics: Ossie. Dataset contracts: ODCS. Provenance: OpenLineage. Tools: MCP. Skills: SKILL.md. Every format is pinned to a version and passes conformance tests. | ADR-0013 |
| P13 | **Compute goes to the data.** Pushdown or federation first. Materialize only on the customer's engine, only under approval. | ADR-0014 |
| P14 | **Knowledge is a reviewable artifact.** The OKF pack is the system of record; the database is a rebuildable index. Nothing a model writes becomes knowledge without review. | ADR-0013 |
| P15 | **Probabilities earn authority through calibration.** A decision purpose may rank or escalate without calibration evidence. It may stop, route or choose content only above a measured calibration threshold. | ADR-0015 |
| P16 | **An extension point is not done until it is used twice.** Every new registry ships with at least two real implementations, or with one implementation plus a test double. | review |

## 3. Capability platform (ADR-0011)

### 3.1 The manifest

Every capability, whether built in or a plugin, is described by one manifest. The manifest is
validated against `contracts/capability.schema.json`.

```yaml
apiVersion: analystos/v1
kind: Method            # Agent | Skill | Tool | Method | Connector | Engine | Publisher |
                        # DecisionPurpose | Detector | Crawler | KnowledgePack | Playbook | Renderer
id: method.cohort_retention
version: 1.0.0
summary: Retention of entity cohorts over time buckets
entry: python:analystos_methods.cohort:CohortRetention   # | mcp://<server>/<tool> | http:<tool-id>
                                                           # | sql-template:<file> | skill-md:<path>
input_schema:  {$ref: "schemas/cohort_input.json"}
output_schema: {$ref: "schemas/stat_result.json"}
determinism: deterministic          # deterministic | seeded | model
side_effect: read_source            # none | read_source | write_internal | write_external
cost_class: query                   # free | query | compute | llm_small | llm_large
permissions: [scope:read]
requires: [engine:sql]              # other capabilities / engine features it needs
certification:
  status: certified                 # draft | tested | certified | deprecated
  evidence: tests/methods/test_cohort.py
ui:
  form: auto                        # a JSON-Schema form is generated
  renderer: renderer.stat_result
```

**Rules.**

- **Unknown side effect means `write_external`.** An MCP tool whose manifest the platform did not
  write gets `side_effect: write_external`, so it needs an approval, until an admin classifies it.
  MCP `readOnlyHint` is advisory, not trusted.
- **Disabled by default.** A capability is enabled per workspace. Only `certified` capabilities can
  run under autonomy ≥ 3 or on a schedule.
- **Versioned and hashed.** Manifests are versioned. The plan hash (v2 §5) includes the id and
  version of every capability a plan binds, so an approval covers exact capability versions.
- **Hot reload, safely.** A registry reload does not need a process restart. Runs already in flight
  keep the versions they bound.

### 3.2 Discovery

1. **Built-in.** `src/analystos/capabilities/builtin/*.yaml`.
2. **Directory packs.** Manifests, SKILL.md files and templates under `packs/<name>/`. They are
   config-only: no code deploy for skills written as SQL templates or SKILL.md, for domain packs,
   or for playbooks.
3. **Python entry points.** The group `analystos.capabilities` holds packaged plugins, so a
   `pip install analystos-methods-finance` adds methods.
4. **MCP servers.** Registered per workspace, on an allowlist. `tools/list` becomes Tool
   capabilities. Descriptions are screened for injection.

**Validation at load.** Unknown `requires`, a schema that fails to resolve, or a skill name that
does not exist makes the load fail. This replaces today's silent YAML drift.

### 3.3 Playbooks replace the hardcoded plan (changes v2 §5)

A playbook is a versioned YAML DAG. `BASE_STEPS` becomes `playbooks/investigate.v1.yaml`. The
behaviour stays the same and the plan hash stays the same for the same inputs. Gates become
**step types**, not magic keys.

```yaml
kind: Playbook
id: investigate
version: 1
steps:
  - {key: context,       use: agent.context}
  - {key: metadata,      use: agent.metadata}
  - {key: relationships, use: skill.relationships, after: [metadata], optional: true}
  - {key: profile,       use: skill.profile, after: [metadata]}
  - {key: quality,       use: skill.quality, after: [profile, relationships], optional: true}
  - key: hypotheses
    use: agent.investigator
    after: [context, profile, quality]
    expands: {prefix: "test:", use: "method.*", loop: {key: followups, max: policy.max_iterations,
              stop: decision.stop_check}}
  - {key: insights,  use: skill.insights, after: [hypotheses, "test:*", "followups:*"]}
  - {key: verify,    use: skill.rev_verify, after: [insights], replan_boundary: downstream}
  - {key: dataset,   use: agent.sql, after: [verify]}
  - {key: semantic,  use: agent.semantic, after: [dataset]}
  - {key: visualize, use: agent.visualization, after: [semantic]}
  - {key: publish_request, type: approval_gate, payload: bundle, after: [visualize]}
  - {key: publish,   type: side_effect, use: "publisher.${workspace.bi_destination}",
     after: [publish_request], skip_when: "run.publish == 'skip'"}
  - {key: finalize,  use: agent.supervisor.finalize, after: [visualize, publish]}
```

**Other playbooks** (each is data, not code):

- `ask` — retrieve → tool-first plan → generate → validate → execute → explain.
- `monitor_investigate` — alert → investigate on a reduced scope.
- `elt_build` — dataset → dbt project → dry run → approval → run on engine → register lineage.
- `knowledge_refresh` — crawl → diff → AI suggestions → review queue.
- `scheduled_reanalysis` — replay the hypothesis registry, then an optional novelty round.

**Engine semantics that move into step types:**

- `approval_gate`: creates a hash-bound approval and waits, with `WAITING_USER`.
- `side_effect`: calls `verify_for_execution` immediately before acting, and checks cancel.
- `replan_boundary`: controls what a redirect or a rejected finding resets.

**As implemented (P4-X02, 2026-09-25).** The shipped `investigate.v1`
(`src/analystos/capabilities/builtin/playbooks/investigate.v1.yaml`) differs from the sketch above
where the v1 behaviour and hash required it:

- Steps `use` agents only (`agent.metadata` with `behaviour: discover_relationships`, not a skill), and
  carry the v1 `title`, because the plan dict and its hash are unchanged.
- `approval_gate` has two forms. `payload: plan` (the `plan_approval` step, present `when:
  "run.autonomy_level <= 2"`, `gates_roots: true`): the engine requests the approval and the step waits.
  `payload: bundle, approval_for: publish`: the step's behaviour requests the approval and the named
  `side_effect` step waits for it; a side effect with no approval is skipped.
- `replan_boundary` is a list of `{trigger: redirect | finding_rejected, reset: self_and_downstream |
  downstream}`.
- Conditions (`when`, `skip_when: {if, reason}`) are one comparison `<path> <op> <literal>`, never code.
- The plan hash is the v1 hash plus the sorted `id@version` refs the run bound; without bindings it is
  exactly the v1 value (`tests/unit/test_playbooks.py` pins it).

### 3.4 Agents become declarative (changes v2 §6)

An agent is a manifest. Python is optional: it is needed only for behaviour the generic runtime
cannot express.

```yaml
kind: Agent
id: agent.investigator
role: Investigation / Hypothesis Agent
goal: Propose falsifiable hypotheses for the objective, in the registered method vocabulary
behaviour: generic.propose_validate_execute   # or python:analystos.agents.investigator:run
model_purpose: hypothesis_generation          # routed by the ladder (§4)
capabilities: ["method.*", skill.profile_lookup, decision.hypothesis_priority]
output_contract: AnalysisSpec[]               # validated; invalid items dropped with reasons
budget: {llm_calls: 3, usd: 0.05, queries: 0}
knowledge: {purposes: [glossary, business_rules, prior_findings, negative_knowledge], budget_chars: 12000}
```

**The generic runtime** runs one loop, capped by the agent budget and a step limit:

1. The model proposes typed actions, chosen only from the bound capabilities.
2. Each action is validated against its schema, policy, scope and budget.
3. The runtime executes it through the capability's entry point.
4. The result is summarised back to the model.

The model never executes anything itself. Every manifest field is enforced; nothing is decorative.

**Publishing an agent:** it must pass its eval set at a score of 0.8 or more (the DataPilot
pattern). An audited override exists.

**As implemented (P4-X03, 2026-09-25).** Agent manifests live in `config/agents/*.yaml` (and in packs
or entry points); the body is `capabilities/agents.py:AgentBody`, and unknown fields fail the load.
`entry: builtin:generic` selects the generic runtime (`agents/generic.py`); a `python:` entry (plus
named `behaviours` a playbook step can select) keeps a Python behaviour. Enforced fields: `capabilities`
(validated at load, bound into the plan hash, the only actions the generic runtime accepts), `tools`
(tool gate), `model_purpose`/`model_purposes` (the only purposes `llm_json` routes for the agent),
`budget` (`llm_calls`, `usd`, `queries`, `max_steps`, per task execution), `policies` (tighten the
workspace policy: `pii_access`, `max_iterations`, `max_rows_extract`), `default_actions` (the rule
path for `off`/`auto`) and `output`. Not implemented yet: the eval-set publishing gate.

**As implemented (FND-006, 2026-09-26).** `knowledge` (`sections`, or `purposes` as written above, plus
an optional `budget_chars`) limits the knowledge sections the context compiler puts in the agent's
prompts; an agent that declares none gets none. `output_contract` is a list of `{type, schema}`: the
artifact or run-record types the agent may persist, each checked against a `contract:<module>.<Model>`
or an inline JSON Schema just before the write. An undeclared type, or content that fails its schema,
raises `OutputContractViolation` and nothing is written. The investigator's `hypothesis:
contract:analysis.AnalysisSpec` drops an invalid spec and records the reason. `AgentSpec` is now a view
derived from the manifest, and `GET /api/agents` shows that view. See
[`docs/20-contracts/03-agent-contract.md`](../20-contracts/03-agent-contract.md).

### 3.5 Analysis methods as plugins (changes v2 §7)

The closed vocabulary stays closed: a hypothesis may only use a *registered* method. The registry
itself is open. A method is a single module implementing:

```python
class Method(Protocol):
    name: str
    def applicable(self, outcome: ColumnType, segment: ColumnType | None) -> bool: ...
    def compile(self, spec: AnalysisSpec, dialect: Dialect) -> CompiledQuery: ...   # pushdown SQL
    def test(self, rows: Rows, spec: AnalysisSpec) -> StatResult: ...              # primary method
    def verify(self, rows: Rows, spec: AnalysisSpec) -> StatResult: ...            # independent 2nd method
    def claim_key(self, spec: AnalysisSpec, result: StatResult) -> str: ...        # stable across runs
    def template_text(self, spec: AnalysisSpec, result: StatResult) -> str: ...    # numbers-guard-safe
    def chart_intent(self, spec: AnalysisSpec, result: StatResult) -> ChartIntent: ...
```

**Derived from the registry, never hand-edited:**

- the prompt vocabulary block;
- the `AnalysisSpec` JSON Schema;
- validation;
- the insight template;
- the chart rules;
- the run-diff claim keys.

**Port and extend.** The six current methods are ported. Two new methods prove the extension point
(P16): `cohort_retention` and `contribution_decomposition` (mix/volume/rate).

### 3.6 Domain packs

A domain pack is a directory pack of type `KnowledgePack` plus hypothesis templates. It contains:

- glossary and business rules (OKF documents);
- PII and sensitivity hints;
- heuristic hypothesis templates, as `AnalysisSpec` patterns keyed on semantic roles rather than
  column names;
- starter KPIs (Ossie);
- dashboard patterns;
- a benchmark dataset with planted effects.

`packs/itsm` holds everything that is ServiceNow-specific today. A second pack, `packs/sales`,
proves the design against its own benchmark.

### 3.7 MCP in both directions, A2A later

**MCP client.** Workspace-registered servers, for example the Superset 6.1 MCP service, the dbt
MCP server and Atlas context tools. Their tools become Tool capabilities (§3.2) and run under the
tool gate, the budget and the audit.

**MCP server.** Built on the MCP SDK and targeting spec 2026-07-28. It exposes:

- **resources:** knowledge pack documents, verified findings (OKF Attested Computations), Ossie
  metrics, published datasets (ODCS);
- **tools:** `ask`, `investigate` (starts a run and returns a handle), `get_finding_evidence`,
  `validate_sql`;
- **prompts:** playbook entry points.

**Grants and access.** Grants are scoped per client, with `client_id.secret` hashed, per-tool
quotas and a per-client OpenAPI document (the DataPilot pattern). OAuth follows the MCP enterprise
authorization extension once SSO lands.

**A2A.** An A2A agent card for "AnalystOS Investigator" comes after the MCP server.

### 3.8 Publishers and dashboard-as-code

`PublishBundle` becomes the portable format: datasets, Ossie metrics, charts, layout and a hash.

- **Superset** is the default adapter and the bundled BI runtime.
- **Preview** is a native ECharts renderer inside AnalystOS. It is used for approval review and
  when no BI destination is configured.
- **Power BI and Tableau** adapters are later capabilities that implement the same protocol.
- **Nothing is hardcoded.** The strings `"superset"` in agent code are removed.

## 4. Deterministic-first execution and the token economy (ADR-0012)

### 4.1 The ladder

| Rung | What answers | Typical purposes | Cost |
|---|---|---|---|
| L0 cache | Exact response cache. It exists since increment 3 (`llm/cache.py`), keyed by workspace, purpose, models and messages; v3 adds the knowledge version to the key | Any purpose at temperature 0 | 0 |
| L1 registry | Verified query or parameterised tool; registered hypothesis; approved metric | Ask, scheduled re-analysis, KPI definition | query only |
| L2 rules/templates | Deterministic code | narrative, chart, summary, stop rule, risk tier, alert materiality | 0 |
| L3 typed decision | DecisionService (JEV, local classifier or rules) | choose among valid options, rank, escalate | ≈ $0.00002 |
| L4 small model | `low_cost` profile | term interpretation, feedback interpretation, SQL repair | low |
| L5 strong model | `reasoning_strong` | hypothesis generation, novel NL→SQL, KPI proposals | high |

Each purpose in `config/models.yaml` declares `ladder: [L1, L2, L5]` and so on. The router records
`answered_by` on every call. The cost dashboard shows spend by purpose and by rung, **including
tokens avoided**.

### 4.2 Purpose changes (applied by P4-T02)

| Purpose | Today | v3 |
|---|---|---|
| `planning` | LLM per run | merged into `hypothesis_generation` (one call frames and proposes) |
| `insight_narrative` | LLM per finding | L2 template by default; optional L4 polish only when a report is rendered |
| `verification` (independent family) | LLM per finding | off by default; policy can enable it for high-stakes workspaces; confidence effect unchanged |
| `rev_second_opinion` | JEV per finding | kept, L3; one batched decision per run where the API allows it |
| `risk_check` at publish | JEV | removed: approval is always required, and the tier is deterministic |
| `chart_selection` | JEV override at p ≥ 0.6 | L2 rules; JEV only among rule-tied candidates |
| `stop_check` | JEV can stop early | L2 rule (no new supported finding in the last round) then L3 as a tie-break, only after the minimum number of rounds |
| `summarization` | LLM | L2 template; optional L4 |
| `alert_triage` | JEV escalate | L2 materiality rules first (completeness, minimum effect, deduplication); JEV escalate-only on what remains |
| `sql_generation` (Ask) | LLM | L0 → L1 verified query or tool (tool-first; decline when a required input is missing) → L5 |
| scheduled re-analysis | LLM hypotheses each run | L1 replays the hypothesis registry; a novelty round is opt-in with its own budget |

Increment 3 already gives `planning`, `hypothesis_generation`, `follow_up_generation`,
`hypothesis_priority`, `stop_check` and `insight_narrative` a rule path under `auto`. For those, the
change is the **default** (`balanced` leaves most purposes on `always`), not new code. Its
`token_saver` run found every planted effect for $0.023 and 10 calls.

**Targets (not claims):**

- a standard run makes ≤ 10 chat calls;
- a scheduled re-analysis with novelty off makes 0 chat calls;
- Ask answered from the registry at p50 < 1 s;
- a CI gate fails a PR that raises calls or tokens per replayed run by more than 10% (P4-T10).

### 4.3 Context compiler (changes v2 §13 retrieval)

This extends the increment-3 compaction (catalog ranked and capped by relevance) and replaces the
`compact_json(payload)[:60_000]` cut in `agents/common.py`. The compiler:

1. Resolves the **purpose profile**: which knowledge types, which catalog detail, and the character
   budget.
2. Selects **relevant columns only**, from the hypothesis or question terms, glossary mappings,
   semantic roles and join paths. Top values are sent only if
   `policy.send_data_samples_to_models` is true.
3. Ranks knowledge sections deterministically:
   - BM25 over document summaries, fused by reciprocal rank with pgvector HNSW and one hop over the
     graph;
   - trimmed to headings and rows (the Atlas `okf_context` algorithm);
   - `NO_MATCH` returned instead of unrelated grounding;
   - an `omitted` list, never silent truncation.
4. Emits **receipts** (document path, sha256, anchor). They are stored on the model call and shown
   in the UI.
5. Lays the prompt out for caching:

   `[system + method vocabulary] [workspace knowledge header] [run context] [the ask]`

   The first two blocks are stable and cached (Anthropic `cache_control` through OpenRouter where
   supported; automatic prefix caching on OpenAI-compatible providers). Volatile content goes last.
6. **Fails visibly** when the required context exceeds the budget. It never cuts mid-JSON.

### 4.4 Budgets

- **Counters.** Run, workspace and purpose counters live in Redis (atomic increments) and are
  reconciled to Postgres. This replaces the `COUNT(*)` per statement and the `SUM` per call.
- **Enforced.** All four currently-dead policy fields are enforced:
  - `send_data_samples_to_models`;
  - `allowed_providers` (intersected with the route);
  - `expensive_model_approval_usd` (a pre-call estimate above it becomes `approval_required`);
  - `data_residency` (fail closed when set and the provider has no region metadata).
- **Cost.** Provider-reported cost is preferred. Otherwise a versioned price table is used. A
  missing price fails visibly instead of counting 0.

## 5. Decision service and JEV (ADR-0015; changes v2 §10)

`DecisionService.decide(purpose, state, question) -> Decision`

- **Backends:** `jev` (OpenRouter Decisions API, pinned model), `rules`, `local_classifier` (for
  air-gapped installs) and `llm_structured` (a small model with a JSON schema).
- **Each purpose declares** an **authority class**, its allowed backends in order, a timeout
  (default 3 s) and a fallback.
- **Every decision is persisted** in a `decision` table: purpose, backend, options, answer,
  **probabilities**, latency, cost and trusted-state hash.
- **A circuit breaker** runs per backend.

| Authority class | May | May not | Calibration needed |
|---|---|---|---|
| `rank` | order valid options | drop options | no |
| `choose_presentation` | pick among rule-valid presentations | change content | no |
| `escalate_only` | raise risk or severity, ask the user | lower risk, grant, suppress | no |
| `route` | pick a processing path among valid ones | skip gates | yes (Brier ≤ 0.2 on labelled set) |
| `bounded_stop` | end exploration after the minimum work | end before minimum rounds | yes |

**Calibration** (P4-T09). Labelled outcomes come from real use:

- findings accepted or rejected by users;
- alerts acknowledged or dismissed;
- feedback classes the user corrected.

A nightly job computes Brier score and ECE per purpose and backend. A purpose whose calibration
falls below its threshold drops to the next backend automatically. The drop is recorded, and it is
visible in Operate.

**New purposes:**

- `ask_route`: choose, via `route`, between a verified query or tool and generation;
- `clarify_needed`: `escalate_only`, asks the user;
- `metric_match`: `rank`, over existing Ossie metrics;
- `join_path_choice`: `rank`, over validated join paths.

## 6. Knowledge layer — open formats (ADR-0013; changes v2 §13)

### 6.1 The knowledge pack is the system of record

```
knowledge/<workspace-slug>/                  # OKF v0.2 bundle (pinned by upstream commit + SPEC.md sha256)
  index.md  log.md                           # OKF reserved files
  sources/<source>.md                        # type: source
  tables/<schema>.<table>.md                 # type: table; column sets split at 100 columns
  glossary/<term>.md                         # type: term (synonyms, mapped columns)
  rules/<rule>.md                            # type: business_rule
  metrics/semantic_model.ossie.yaml          # Apache Ossie 0.1.1 (datasets, metrics, dimensions, relationships, ai_context)
  metrics/<metric>.md                        # OKF doc linking to the Ossie metric
  findings/<finding-id>.md                   # type: attested_computation (method, params, query hash, result hash,
                                             #   q-value, effect size, verified_by, approved_by, stale_after)
  anomalies/<id>.md  negative/<id>.md        # known anomalies; "we checked, this is not true"
  contracts/<dataset>.odcs.yaml              # ODCS v3.2 for published datasets
  skills/<name>/SKILL.md                     # analytical know-how (Agent Skills format, progressive disclosure)
```

**Frontmatter** carries the OKF v0.2 trust fields:

- `status`: draft, stable or deprecated;
- `generated`: by whom or by which run;
- `verified`: who and when;
- `sources`;
- `stale_after`.

**Storage.** A content-addressed object store per workspace, versioned: every change is a new
revision with author and reason. An optional **git remote** lets a customer review knowledge
through pull requests.

**Index.** Postgres is only an index: sections, BM25 `tsvector`, pgvector **HNSW**, and a link
graph. It is rebuildable from the pack at any time (`analystos knowledge reindex`).

**Isolation.** `context_entry` rows with a NULL workspace are replaced by an explicit read-only
`platform` pack.

### 6.2 Import, export, conformance

| Format | Import | Export | Pinned version | Conformance |
|---|---|---|---|---|
| OKF | Atlas bundles, Google Knowledge Catalog | full pack | v0.2 (commit-pinned) | round-trip test; publish policy (no dangling links, size caps) |
| Apache Ossie | dbt Core 1.12 `osi_document.json`, Snowflake/Databricks exports | Superset metrics, dbt `osi/`, Ossie JSON | 0.1.1 | official examples validate; round trip |
| ODCS | — | published datasets | v3.2.0 | JSON Schema validation |
| OpenLineage | dbt, Spark (via the engine) | run, query and job events | 1.x client | event schema validation |
| dbt manifest | lineage and model docs | generated projects (§7) | manifest v12+ | parse test |

Every adapter is a `KnowledgePack` or `Crawler` capability behind a version-pinned interface. The
threat is standards churn, so no core code imports a standard's library directly.

### 6.3 Crawlers (the minimal built-in set)

Increment 3's `services/crawler.py` (ADR-0010) is the base. It already has:

- discovery, fingerprints and drift;
- curation precedence;
- PII by name and by gateway-sampled value;
- deterministic semantics, and incremental scheduled crawls.

v3 changes where the crawler writes: to **OKF documents** in the pack (§6.1), with the index
updated from them. It also adds the following:

- **Database catalog.** Tables, columns, keys, comments, views and row counts. Facet-level failure,
  so a refused permission costs one facet and not the scan.
- **Query history.** Structure only, never values: join paths, popular filters and groupings.
- **dbt manifest and Ossie.**
- **Superset metadata.** Datasets, charts and dashboards, feeding existing-dashboard mode
  (BI-011).
- **Documents.** Markdown, PDF and TXT upload first; Confluence and SharePoint later as
  capabilities.

**Change detection.** A fingerprint per object, rename candidates, and dropped objects. A change
becomes a knowledge **draft** in the review queue; it is never published automatically.

**Scheduling and defaults.** Crawls run on the existing claim-then-execute scheduler. They are
**enabled by default** with sane intervals: no dark passes.

### 6.4 AI-suggested knowledge

Builds on the increment-3 rules: placeholders only, confidence below 0.6, screened batches, and
`user` or `reviewed` text never overwritten.

- The model sees **schema and statistics only**, never rows unless the policy allows samples.
- Invented column names are dropped.
- It never overwrites human-authored text.
- It is called only for low-evidence objects, batched about 25 tables per call.
- Each suggestion carries per-field provenance (`generated: model@version, run`) and a confidence.
- Suggestions go to a batch review queue: accept, edit or reject, and a rejection becomes negative
  knowledge.

### 6.5 Learning loop

Four things feed back as knowledge drafts:

- verified and approved findings;
- approved KPIs;
- user corrections and redirects;
- rejected findings, which become negative knowledge.

**Episode memory** reaches prompts only through the compiler, as `prior_findings` and
`negative_knowledge` purposes with their own budget. That fixes today's write-only memory.

### 6.6 Context2AI interop (answers v2 §18 open question 1)

`ContextProvider` capabilities:

- `local`: this workspace's pack;
- `okf_import`: a scheduled or on-demand import of an Atlas or Knowledge Catalog bundle;
- `mcp`: live calls to Atlas `get_knowledge_context` and `get_object_knowledge`.

The speculative REST adapter (`context/service.py`, v1 §11.1 paths) is retired. When Atlas is
present, AnalystOS does not run its own database crawler for the same sources.

## 7. Data plane — engines, federation and minimal ELT (ADR-0014; changes v2 §11)

### 7.1 Engines

Wraps the increment-3 source-kind catalog (`config/source_kinds.yaml`, `connectors/generic_sql.py`).
The key change is **multi-dialect validation**. Today only postgres and tsql push down, and every
other kind, including cloud warehouses, is staged as an unordered `LIMIT` of up to 1M rows.
Each dialect gets its own validator security suite so warehouses can be queried in place. Until
then, staging must declare a sampling strategy, and truncation must reach verification (P4-C12).

```python
class Engine(Protocol):
    id: str; dialect: str
    features: set[Literal["pushdown", "federate", "materialize", "write", "spark_sql", "python_udf"]]
    def execute_read(self, stmt: ValidatedSQL, *, identity: ReaderIdentity, limits: Limits) -> Result: ...
    def estimate(self, stmt: ValidatedSQL) -> CostEstimate: ...                 # rows/bytes/credits when available
    def plan_write(self, build: BuildPlan) -> DryRun: ...                         # never mutates
    def execute_write(self, approved: ApprovedBuild) -> BuildResult: ...          # only with verify_for_execution
```

**Implementations:**

- Postgres and SQL Server (ported);
- DuckDB (files, small data, local federation);
- Trino (federation);
- Spark, through Spark Connect or the Databricks SQL warehouse;
- Snowflake and BigQuery.

Each is `draft` until it is certified against a live instance (P16 and v1 §62). Capability flags
are derived from the certification evidence, never merely declared (the Atlas pattern).

### 7.2 Cross-source runs (INT-001..005, TRN-004)

This lifts v2's "one source per run".

- A run may bind several sources.
- The integration agent proposes join keys. They are validated by containment and cardinality
  checks (the existing skills).
- Execution goes through a federating engine (Trino or DuckDB), or through Spark when the sources
  already live there.
- The scope validator handles each source separately and each source keeps its own identity.

### 7.3 Minimal ELT: generate, dry-run, approve, run on their engine

1. The `elt_build` playbook turns the virtual dataset, derived columns and approved KPIs into a
   **dbt project** artifact: `models/*.sql`, `schema.yml` with tests, and `osi/` Ossie metrics.
   Spark SQL is the alternative for Spark-only estates.
2. **Dry run** on the target engine: compile, run the tests on a sample or `LIMIT 0`, and estimate
   the cost.
3. **Approval** is bound to the project hash, target engine, target schema and plan hash (P6).
4. **Execute** on the customer's runner: a dbt Core job container, the dbt Cloud API or a Spark
   job, with a separate **write identity** limited to the target schema.
5. **Harvest** the dbt manifest and OpenLineage events into lineage. The published dataset gets an
   ODCS contract.

**Incremental refresh (TRN-003):** dbt incremental models are generated only for sources with a
reliable watermark column; otherwise a full refresh.

**Ingestion from non-SQL sources** (ServiceNow, REST, files) stays on the staged loader. A spike
(P4-E05) evaluates **dlt** for incremental loads into the customer's warehouse and decides with an
ADR.

### 7.4 Write-path gateway

`QueryGateway` stays read-only. A separate `BuildGateway`:

- accepts only approved `BuildPlan`s;
- re-verifies the approval hashes immediately before running;
- runs under the write identity;
- audits every statement or job;
- records a rollback plan (drop or rename of the created objects).

Sources remain read-only. Writes go only to the target schemas the customer designated.

## 8. Scalability and self-hosted operations (changes v2 §15)

| Concern | v3 design |
|---|---|
| Work isolation | Temporal task queues: `analysis` (I/O), `compute` (CPU statistics in a **process pool**), `publish`, `crawl`, `elt`; each worker pool scales separately (ADR-0001 split points) |
| Liveness | Activity heartbeats; `start_to_close` sized per queue (stats 10 min, crawl 30 min with heartbeats); workflow `continue_as_new` every N loops |
| Engine loop | `get_state` reads without `FOR UPDATE`; claims use optimistic `plan_version` + a task claim row; per-run parallelism comes from policy |
| Events | `run_event` insert plus Postgres `LISTEN/NOTIFY` (or a Redis pub/sub subscriber) feeds **async** SSE fan-out; no per-client DB polling; blocking DB calls moved off the event loop |
| Budget counters | Redis atomic counters, reconciled |
| Tenant isolation | Per-workspace reader roles (`analystos_r_<ws>`) granted only that workspace's `src_*` schemas; `workspace_id` in every uniqueness and graph key; negative tests across workspaces in CI |
| Lineage | Postgres recursive CTE per artifact; Neo4j becomes an optional projection, **off by default** |
| Vectors | pgvector HNSW; configurable embedding dimension and provider (a local sentence-transformer by default for air-gapped installs; hashing stays as the zero-dependency fallback) |
| Cache | Result cache holds up to 5k rows per entry; larger results keep only the hash and a reference |
| Database topology | Separate instances (or at least clusters) for control plane, analytics staging, Temporal and Superset in production; PgBouncer |
| Packaging | Helm chart with HA values; offline image bundle. **Air-gapped mode** extends the increment-3 `offline` preset (which makes no model calls at all) with an OpenAI-compatible local model endpoint (vLLM or Ollama) and DecisionService `rules` or `local_classifier`; no OpenRouter |
| Model providers | Beyond OpenRouter: OpenAI-compatible (including local), Azure OpenAI, Bedrock and Anthropic direct, as `Provider` capabilities |

**Load targets (to be measured, never claimed here):**

- 50 concurrent runs on a four-node worker deployment;
- 1,000 workspaces;
- SSE event latency under 1 s at p95 with 500 open streams;
- Ask answered from the registry under 1 s at p50;
- Ask with generation under 8 s at p50.

## 9. UI — five journeys, capability-driven (changes v2 UI rows; v1 §52)

**Information architecture.** At most 20 screens. The increment-3 Catalog and crawl panels move
into Knowledge; admin settings and the token-savings view move into Operate. Every screen answers four questions (the Atlas
UX contract): what is this, what state is it in, what can I do, and why should I trust it.

| Journey | Screens | Key patterns |
|---|---|---|
| **Home** | persona landing | "What changed since you were last here": alerts, new verified findings, KPI moves, pending approvals. Unknown is never shown as 0. |
| **Ask** | conversation; answer inspector | threads (search, grouping); SSE stages in plain language ("Finding the tables that answer this"); **answer card = ECharts visual + table**; inspector tabs **Result / SQL / Evidence / Decision** (knowledge receipts, metric version, verified-query id, ladder rung, JEV probabilities); provenance and staleness pills; one refusal state per kind, with a remedy; ambiguity shown as competing definitions; promote actions (save as verified query, metric, monitor, add to dashboard, **"Investigate why"**); paste-SQL explain mode |
| **Investigate** | objective composer; investigation board; finding detail | pick data by searching knowledge, not by table name; live **hypothesis board** (proposed → testing → supported / not supported → verified); finding card with a **"Why trust this"** drawer (re-run hash, second method, q-value, effect size, n, data-quality caveats, approvals); redirect by chat; cost meter by rung ("tokens avoided"); raw JSON only under "Technical details" |
| **Knowledge** | pack browser and editor; review queue; semantic graph; metrics | OKF document tree and search; frontmatter form with trust fields; **review queue** (AI suggestions, crawler changes, learned knowledge) with batch accept, edit and reject; graph with governed edges solid and inferred edges dashed; Ossie metric editor with live validation; import and export |
| **Build** | datasets and models; KPIs; dashboards; reports | dbt or SQL diff view, dry-run results and cost estimate; KPI editor; native ECharts dashboard preview, then publish to Superset; report templates |
| **Operate** | approvals; monitors and alerts; schedules; runs; usage and cost; capabilities; admin | approval inbox with payload diff, hashes and policy version; alert triage explanation; spend by purpose, rung and model; a **capability registry page generated from the registry** (installed, certified, enabled, MCP servers) instead of a static architecture page; policies |

**Design system.**

- Tokens: light and dark, a status palette that is never reused for decoration.
- A Ctrl/Cmd-K command palette.
- State families: loading, empty, refused, failed, not-entitled, stale.
- A primitives library.
- An **API client generated from OpenAPI**, replacing the hand-written types in `api.ts`.
- axe and Playwright journey tests in CI.

**Capability-driven UI.** A capability's `input_schema` generates its form, and its `ui.renderer`
selects a result renderer from a registry: table, chart, stat_result, finding, knowledge doc, diff.
A new tool, method or MCP tool needs **no new screen**. That is how the UI keeps up with "add
anything anytime".

**Ask answer layout.** The inspector is bound to the selected answer:

```
┌ Threads ─────────┐┌ Conversation ───────────────────────────────┐┌ Inspector ───────────────────┐
│ ▸ SLA breaches   ││ You: Which teams breach SLA most, last 90d? ││ [Result][SQL][Evidence][Decision]
│   Change risk    ││ ● Finding the tables … ● Checking access …  ││ Evidence                      │
│   …              ││ ┌ Answer ─────────────────────────────────┐ ││  metric  sla_breach_rate v3   │
│                  ││ │ ▇▇▇▇▇ Network  18.2%                    │ ││  verified query VQ-12 (param) │
│                  ││ │ ▇▇▇   Desktop  11.0%   [chart|table]    │ ││  knowledge: glossary/sla.md#… │
│                  ││ │ from verified query · knowledge v14     │ ││ Decision                      │
│                  ││ │ [Investigate why] [Save as monitor] [+] │ ││  rung L1 registry · 0 tokens  │
│                  ││ └─────────────────────────────────────────┘ ││  ask_route: VQ 0.93 (jev)     │
└──────────────────┘└─────────────────────────────────────────────┘└───────────────────────────────┘
```

## 10. Evaluation (extends v2 §16)

- **Analytical benchmark across domains** (P4-V01): ITSM, sales and finance datasets with planted
  effects and null controls. Precision and recall of *verified* findings; false-discovery rate
  compared with the nominal α.
- **Ask accuracy** (P4-V02): a labelled question set per domain, run with a **real model**
  (the Atlas lesson). Execution-match accuracy, with refusal correctness as its own measure.
- **Decision calibration** (P4-T09): Brier score and ECE per purpose and backend.
- **Cost regression** (P4-T10): runs replayed with recorded model responses. Calls, tokens and
  rungs are compared with the baseline in CI.
- **Isolation** (P4-C03): tests that try to reach across workspaces through SQL, lineage, graph,
  knowledge and cache.

## 11. Migration and sequencing

The waves are listed in the tracker, increment 4.

| Wave | Content | Why it comes here |
|---|---|---|
| 0 | Correctness and governance debt (P4-C*) | Pilot blockers; cheap; every later wave builds on these paths |
| 1 | Capability platform (P4-X*) | Every later capability plugs into it; the owner's first requirement |
| 2 | Token economy and decisions (P4-T*) | Needs the ladder hooks and purposes from wave 1 |
| 3 | Knowledge layer (P4-K*) | Needs the capability kinds (Crawler, KnowledgePack) and the compiler hook |
| 4 | Engines and ELT (P4-E*) | Needs Engine and Publisher capabilities, and Ossie metrics from wave 3 |
| 5 | UI on the new IA (P4-U*) | Built on the registry (forms and renderers) and the knowledge APIs |
| 6 | Scale and operations (P4-S*) | Some items can run in parallel with waves 2–5; the load tests come last |

**Compatibility.** `investigate.v1.yaml` must reproduce the current plan hash, and the §62 live
scenario must pass unchanged, before any hardcoded path is deleted (P4-X02 acceptance).

## 12. Open questions

1. Which OKF commit to pin: share Atlas's pin (`Docs/90-reference/okf-export-profile.md`) so
   bundles interoperate, or take upstream v0.2 as it stands now?
2. Ossie: stay on 0.1.1 until dbt accepts 0.2, or support both behind the adapter?
3. Air-gapped installs: which local model families to certify for hypothesis generation, and at
   what quality bar (P4-V01 on local models)?
4. Whether the Power BI adapter (PBI-001/002) is needed before the pilot, or whether bundled
   Superset is enough for the first customers.
5. A shared contracts package across Atlas, DataPilot and AnalystOS (P4-G01), or only documented
   formats?
