# ADR-0010 — Universal sources, metadata crawler, token economy, admin control plane

**Status:** accepted (2026-09-25, increment 3)

**Context.** ServiceNow was only the first source. The platform has to reach any database the
business uses, keep its catalog current without a person re-running discovery, spend model
tokens only where a rule cannot produce an equivalent result, and let a platform administrator
change all of that at runtime, without a redeploy.

## Decisions

### 1. A declarative source-kind catalog (`config/source_kinds.yaml`, `connectors/kinds.py`)
Each kind declares its fields, its secret field, its URL template, its SQLAlchemy driver and pip extra,
its native sqlglot dialect, its read-only session statements and its catalog method. One
`GenericSQLConnector` serves every SQL kind. It uses the SQLAlchemy inspector (DuckDB uses its own
`duckdb_*()` functions), reads keys, foreign keys and comments, applies include/exclude and
`max_tables`, and extracts in Arrow batches on a read-only session.

**Pushdown vs staged.** Pushdown is allowed only when both of these hold:
- the gateway validator understands the dialect (postgres, tsql);
- the kind has a session-level read-only switch.

Every other kind is **staged** into `analytics.src_<id>`, and the gateway reads the staged copy.
This means one validator, one reader identity and one audit trail, whatever the origin.

A kind is not "certified" until it has been run against a real engine. The register lists which
kinds have been live-tested.

### 2. The crawler is deterministic; the model is last and optional (`services/crawler.py`, `skills/catalog.py`)
Every crawl runs these stages in order:
1. discover
2. fingerprint and diff
3. apply
4. semantics
5. PII
6. profile
7. relationships
8. glossary
9. enrich (optional)
10. publish

Each stage is logged on `crawl_run`.

- **Fingerprints** are order-independent structural hashes. An unchanged table costs one hash
  comparison on re-crawl: it is not re-profiled, re-described or re-embedded.
- **Only what was looked at can be judged missing.** An excluded or capped table is "not crawled",
  never "gone". Only a full, uncapped crawl deprecates a table. Deprecation deselects the table,
  which removes it from every scope.
- **Curation wins.** Owner tags (`tags_origin = user`) are never removed. Crawler tags only tighten:
  a column can gain `pii` or `restricted`, never lose it. The description precedence is:
  - `user` and `reviewed` are never overwritten;
  - `source` (a comment in the source system, screened before storage) comes next;
  - `model` fills placeholders only;
  - `rule` refreshes its own text.
- **Data-touching steps go through the gateway.** PII value sampling and profiling run only on
  selected assets, through `QueryGateway` with the caller's resolved scope. Denied columns are
  never sampled. Sampled values are classified in memory and are never stored or logged.
- **The model** (`metadata_enrichment`, `low_cost`) is called only for tables whose rules were
  unsure (confidence < 0.6 or role unknown), in screened batches without sensitive columns. It may
  describe only the keys it was sent. It is off unless `crawl.llm_enrichment` is enabled.
- **Re-discovery goes through the crawler.** `discover_source` now runs the crawler. The previous
  implementation re-created column rows, which silently dropped owner `restricted` tags on every
  re-discovery.

### 3. Token economy (`llm/router.py`, `llm/cache.py`, `agents/common.model_gate`)
- **Per-purpose mode** (`off | auto | always`):
  - `off`: never calls the model. The router raises `LLMDisabled` and the deterministic path runs.
  - `auto`: rules first; the model is called only when the rule result is insufficient. For
    example, the investigator skips the model when carried-forward plus rule hypotheses reach
    `heuristic_hypotheses_sufficient`.
- **Response cache** (Redis plus an in-process LRU). The key covers the purpose, the candidate models
  and the full payload. The payload already carries the workspace's own catalog, so identical keys
  imply identical inputs.
- **Pre-call controls.** Estimated oversize prompts are refused, and the deterministic fallback
  runs. Chat purposes downgrade to `low_cost` when a run has less than
  `downgrade_below_budget_fraction` of its budget left; the platform allowlist and disabled models
  still apply.
- **Prompt compaction.** Catalog prompts rank tables and columns by relevance to the objective and
  cap them.
- **Savings accounting.** Every avoided call is recorded in `model_call`, with status
  `skipped | cache_hit | refused` and `tokens_saved`. `/api/admin/token-savings` reports spent vs
  saved.

### 4. Admin control plane (`contracts/platform.py`, `services/platform_settings.py`)
- **Versioned settings document.** One append-only `platform_setting` document holds routing
  overrides, profile models, disabled models, purpose modes, cache, limits, analysis, crawl,
  monitor and source settings, and feature flags.
- **Writes.** Admins only. Writes are validated against `config/models.yaml`; the settings cannot
  add a model that is not already on the allowlist. Every write is audited as a diff.
- **Merge rules.** A write merges onto the uncached latest version. Maps are replaced whole, which
  is the only way to delete a key.
- **Rollback** is a new version, not a delete.
- **Reads** are cached for 5 s in each process.
- **Precedence.** Workspace policy can only tighten what the platform allows.
- **Presets:** `balanced`, `token_saver`, `max_quality`, `offline`. `offline` runs the whole
  platform with no model calls.

## Consequences
- A platform can now run analyses on MySQL, Oracle, Snowflake, BigQuery, Databricks, Trino,
  ClickHouse, DuckDB and SQLite with no new connector code. Only postgres and tsql push down.
- The hypothesis playbook gains a domain-neutral, role-driven block built from crawler column roles
  (measures by segments, trends on event time). Non-ITSM databases therefore get rule-based
  hypotheses, and `auto` mode can skip the model on them too.
- Settings are the second place, after workspace policy, where behaviour changes without code, so
  both are audited. The effective routing is visible at `/api/admin/models`.
- **Accepted limitations:**
  - Semantic rules are English, keyword-based heuristics.
  - Kinds without a session read-only switch rely on a least-privilege login (stated in each
    kind's docs).
  - Rename detection is candidates-only: a rename is reported, never applied automatically.
