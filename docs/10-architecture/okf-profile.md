# AnalystOS OKF v0.2 profile — knowledge packs, index, import/export, providers

Delivered for tracker rows P4-K01, P4-K02, P4-K09 and P4-K10 (ADR-0013; amends ADR-0007).
Code: `src/analystos/knowledge/`. Tests: `tests/unit/test_knowledge_okf.py`,
`tests/integration/test_knowledge_pack.py`. Evidence:
[`docs/60-delivery/evidence/2026-09-25-knowledge-k01-k10.md`](../60-delivery/evidence/2026-09-25-knowledge-k01-k10.md).

## The pinned specification, and what is claimed

| Field | Value |
|---|---|
| Format | Open Knowledge Format, version `0.2` |
| Upstream | `https://github.com/GoogleCloudPlatform/open-knowledge-format`, file `SPEC.md` |
| Pinned revision | `0b87c52c6ef999286c745e19998fdfcd03d5dbee` (the pin Atlas uses, so bundles interoperate — spec v3 §12 question 1) |
| SHA-256 of the pinned `SPEC.md` | `26aa5da029278939f914e578107242d9607d4f2dc5fe153272b82f9ed1030101` — fetched from upstream at that revision on 2026-09-25 and matched |
| Conformance status | `SELF_CHECKED_AGAINST_PINNED_SPEC_CLAUSES` |

The spec text is not vendored. **No conformance certification is claimed**: there is no upstream
conformance suite. `okf.check_conformance` implements the three §11 clauses as this module reads
them (parseable frontmatter on every non-reserved `.md`, a non-empty `type`, `index.md`/`log.md`
per §8/§9) and tolerates what §11 says must be tolerated (unknown types and keys, missing optional
families, broken links, missing indexes; a bare `verified` mapping is a one-element list).

## Packs (system of record)

A pack is a versioned, content-addressed bundle in the control plane:

* `knowledge_object` — file bytes keyed by `(pack, sha256)`;
* `knowledge_revision` — immutable: number, parent, author, reason, origin, the full
  `{path: sha256}` map and its content digest, plus the conformance report;
* `knowledge_pack` — scope, pin, `okf_root`, head revision, optional git remote.

| Kind | Scope | Writable by | Content |
|---|---|---|---|
| `platform` | exactly one, no workspace | system writers only (seed, migration) | the installed domain packs' knowledge (`domain/<pack>/*.md`) and anything migrated from the old NULL-workspace `context_entry` rows |
| `workspace` | one per workspace | the workspace (through `store.commit`) | the workspace's own knowledge |
| `imported` | per workspace, by slug | re-import only | an Atlas or other OKF bundle, bytes unchanged |

`context_entry.workspace_id` is now NOT NULL: nothing is shared across tenants except through the
explicit read-only platform pack. Workspace `context_entry` rows (terms, notes, episodes) are
unchanged in this increment; they move into the workspace pack with K05–K08 (see open items).

**Optional remote.** `analystos knowledge remote --workspace WS --url URL [--branch B]` sets a
workspace pack's repository. A push writes outside the platform, so it is a hash-bound approval
(`knowledge.push`, payload = pack, revision, content digest, remote, branch):
`request-push` → an approver decides → `push --approval ID` runs `verify_for_execution`
immediately before pushing, consumes the approval, and pushes the revision's exact tree as one
commit. The publish policy must pass. No credentials in URLs; the host's git credentials apply.

## Documents and the `analystos` extension

A document is OKF v0.2: YAML frontmatter + Markdown body. Frontmatter is loaded with
`okf.safe_yaml` (no anchors/aliases, no explicit tags, depth 16, 8,000 events, one document).
Types are producer-chosen: this platform writes `Glossary Term`, `Metric`, `Business Rule`,
`Definition`, `Note`. Its producer extension (§4.1) is the `analystos` mapping:
`kind`, `synonyms`, `mapped_columns`, `domain_pack`, `origin`, `trusted`. Imported documents keep
their own extensions (e.g. Atlas's `atlas` mapping) untouched.

`verified`, `status` and `stale_after` are read and indexed (trust tier per §5.3, staleness per
§5.5). They are advisory signals, never access control; an imported `verified` claim is counted
in the import report as a claim, not treated as an AnalystOS approval.

## Index (rebuildable)

`analystos knowledge reindex` drops `knowledge_document`, `knowledge_section`, `knowledge_link`
and `knowledge_index_state` and rebuilds them from each pack's head revision:

* sections are cut at top-level `# ` headings outside code fences (Atlas's `okf_context` rule);
  text before the first heading is `preamble`; anchors are slugs, de-duplicated with `-1`, `-2`;
* each section has a generated `tsvector` (GIN) over title, synonyms, heading and text, and an
  embedding in a pgvector **HNSW** index (`vector_cosine_ops`);
* links (§6.1) are resolved (bundle-relative `/x`, relative, `dir/` → `dir/index.md`, schemes and
  `//` are external) and stored with `resolved` so dangling links are visible.

Ids are derived from `(pack, path, anchor)`, rows are written in path order and every ranking
breaks ties on stable keys, so a drop-and-rebuild gives identical retrieval results (test and
evidence). A commit indexes its pack immediately; `analystos migrate` indexes packs whose head is
not yet indexed.

**Retrieval** (`index.retrieve`, the API the context compiler calls): scope is always the packs the
workspace may see (platform + its own); `pack_ids` can only narrow it. Two legs, fused by
reciprocal rank (k = 60): a lexical leg — Postgres full text (`to_tsquery` OR over the question's
words, `ts_rank_cd`) — and a vector leg — HNSW nearest neighbours with `hnsw.iterative_scan =
relaxed_order` then an exact re-sort. **Deviation:** the lexical leg is Postgres full-text ranking,
not Okapi BM25 (spec v3 §4.3/§6.1 say BM25); a true BM25 needs corpus term statistics and is left
to P4-K05, which owns ranking.

## Import and export (K02)

`analystos knowledge import --workspace WS --slug S <dir|zip>` / `bundle.import_bundle`:

* **Atlas bundles** are detected by `atlas-manifest.json` beside a `bundle/` root; the pack's
  `okf_root` is `bundle`. The manifest is checked (`atlas_extension`, OKF version, SPEC pin, the
  per-file SHA-256 list) and differences are reported as warnings.
* **Lossless**: every accepted file is stored byte for byte, the manifest included, so export
  reproduces the same members with the same bytes (tested on the Atlas sample bundle, a real
  Atlas export — `tests/fixtures/okf/README.md`). Re-importing identical bytes writes no revision.
* **Hostile archives** are refused before anything is stored: traversal, absolute or unsafe
  names, symlinks and special files, encryption, unsupported compression, non-`.md/.json/.yaml`
  members, case-insensitive duplicates, member/archive/expansion size caps and a 100:1 ratio cap
  (members ≥ 64 KiB). OS metadata (`__MACOSX/`, `.DS_Store`, …) is ignored and listed.
* Nothing is executed: an `Attested Computation` is counted, never run.

**Export** (`analystos knowledge export`, `bundle.export_zip`) enforces the **publish policy**
(`okf.check_publish_policy`): conformance, safe paths (`[A-Za-z0-9][A-Za-z0-9._-]*` segments), no
dangling internal links, no links outside the bundle, document ≤ 256 KiB, bundle ≤ 64 MiB,
≤ 20,000 files. The archive is deterministic (sorted, fixed timestamps and attributes). External
links and code fences are allowed (Atlas's stricter rules are Atlas's). A download route in the
API is not added here: an export that leaves the platform through the UI must be an approval
(CLAUDE.md rule 5) — for the P4-U rows.

## Context providers (K09)

Configured per workspace in policy `context_providers` (default `[{kind: local}]`):

| Kind | Reads | Notes |
|---|---|---|
| `local` | platform + workspace packs, through the index | |
| `okf_import` | one `imported` pack, through the index | `sync` imports from a location below the upload directory, or from bytes |
| `mcp` | Atlas `atlas__get_knowledge_context` on a registered MCP server | through `mcp.client.invoke_tool`: allowlist, classification, policy, run budget, screening, `tool_execution` record. Register the server with `config: {lift_json_blocks: true, result_max_chars: 48000}` so Atlas's ```json selection is parsed and screened value by value (screening otherwise drops fenced blocks). Returns Atlas's receipts (path, anchor, sha256). |

The speculative REST adapter (`Context2AIClient`, `context2ai_url`) is removed. Provider output is
data in the context package's `external` list; it reaches no prompt until P4-K05. Tested against a
recorded **mock** (`tests/fixtures/okf/atlas-mcp-get-knowledge-context.mock.json`) — not a live
Atlas instance.

## Embeddings (K10, amends ADR-0007)

`knowledge_embedding_provider`: `auto` (default: a local sentence-transformer when the optional
`embeddings` extra and the model are installed, hashing otherwise), `hashing`,
`sentence_transformers`. `knowledge_embedding_model` (default `BAAI/bge-small-en-v1.5`),
`knowledge_embedding_dim` (hashing: any 1–2000; a model: truncation + re-normalisation, only
meaningful for Matryoshka-trained models), `knowledge_embedding_allow_download` (default false:
air-gapped, `local_files_only`). The index records its provider; queries embed with it, so a
configuration change never mixes vector spaces. `analystos knowledge reembed [--provider]
[--dim]` moves the index, retyping `vector(n)` and rebuilding the HNSW index when the dimension
changes. `context_entry.embedding` stays 256-d hashing.

## Findings as Attested Computations (K04)

Code: `knowledge/attested.py` (`AttestedComputation`, `attested_from_insight`, `write_findings`,
`check_attested`). A **verified** insight becomes `findings/<insight-id>.md`,
`type: Attested Computation` (OKF §10). An unverified one has nothing to attest (`InvalidInput`).

| Frontmatter | Value |
|---|---|
| `runtime` (§10.2, required) | the gateway dialect of the primary query's source |
| `parameters` | `[]` — a finding is a fixed computation; its AnalysisSpec is in `analystos.attestation.params` |
| `executor` | `analystos://gateway/QueryGateway.execute`, receipt `[query_id, executed_sql, result_hash]` |
| `attester` | `analystos://rev/reproducible_rerun` (the REV re-run compares result hashes) |
| `generated` | `process:analystos-rev` at verification time |
| `verified` | `process:analystos-rev` (machine-confirmed); a `human:<id>` approver makes it human-reviewed and `stable` |
| `stale_after` (§5.5) | verification time + 90 days (`stale_days`) |
| `sources` | one entry per governed query (`analystos://query/<id>`) and per asset read |
| `analystos.attestation` | `method`, `params`, `spec_hash` (hypothesis registry hash), `plan_hash`, `run_id`, `query_hash` (`query_execution.fingerprint`), `result_hash`, `queries[]` (primary and verification, each with both hashes), `statistics` (`test`, `n`, `p_value`, `q_value` = BH-adjusted p, `effect_size`, `effect_label`), `q_value`, `effect_size`, `confidence`, `checks[]`, `verified_by[]`, `approved_by[]` |

The body has `# Claim`, `# Computation` (one fenced SQL block, the executed statement), `# Evidence`
and `# Caveats`. `AttestedComputation.from_document(render())` is lossless (tested). There is no
upstream JSON Schema for OKF, so `check_attested` is this profile, self-checked.

`POST /api/workspaces/{ws}/analysis/{run}/findings/attest` writes a run's verified findings into the
workspace pack as drafts (curated documents kept, below); `GET /api/insights/{id}/attested` returns
one without writing. Turning approved findings into drafts automatically is P4-K08.

## Machine-written documents: the crawler invariants (K04, K06)

`knowledge/drafts.write_drafts` is the one way crawlers, ingesters and the REV write into a
workspace pack (one merge revision per call):

* a document is **curated** when `generated.by` or any `verified[].by` is `human:…`, or the
  `analystos` extension says `origin: user` / `reviewed: true` — it is kept byte for byte and
  reported as `kept_curated`;
* otherwise it is replaced, with `tags` = old ∪ new (tags only tighten);
* identical content is not rewritten; machine documents carry no `generated.at` and no statistics,
  so re-crawling unchanged metadata writes no revision.

## Crawler output and sources (K06)

Layout (spec v3 §6.1) and types written:

| Path | `type` | From |
|---|---|---|
| `tables/<schema.table>.md` (+ `.columns-N.md` beyond 100 columns) | `Table` | every crawl (replaces the crawler's `context_entry` table rows) |
| `sources/<source id>.md` | `Source` | every crawl |
| `sources/<source id>.query-patterns.md` | `Query Patterns` | every crawl (facet `query_history`), or `POST …/knowledge/crawl/query-history` |
| `dbt/<project>/{models,seeds,snapshots,sources}/<name>.md` | `dbt Model` / `dbt Source` / … | `POST …/knowledge/crawl/dbt-manifest` (manifest v12+) |
| `bi/superset/{datasets,charts,dashboards}/<id>.md` | `BI Dataset` / `Chart` / `Dashboard` | `POST …/knowledge/crawl/superset` (GET-only) |
| `documents/<name>.md` (+ `.part-N.md`) | `Document` | `POST …/knowledge/documents` (upload) |

Everything is written `status: draft` (a reviewed table is `stable`, a deprecated one `deprecated`)
with `analystos.origin` naming the source. Links between these documents always resolve inside the
bundle (only documents written in the same call, or known to exist, are linked), so the pack still
passes the publish policy.

* **Value-free query history** (`skills/query_history.py`): statements from `query_execution`
  (status `ok`, not the crawler's own) are parsed in memory with sqlglot; kept are tables read,
  join paths (`ON` and implicit `WHERE a.x = b.y`), filtered columns with an operator class
  (`=`, `<>`, `range`, `in`, `like`, `is null`), groupings and ordering. No literal is kept; the
  audit is the OKF source with `usage_count` and `usage_window` (§5.1). Source-side query logs
  (e.g. `pg_stat_statements`) are not read: that would be a data-touching step outside the
  selected assets.
* **dbt manifest**: descriptions, columns, tests (kind and column only — test arguments such as
  `accepted_values` lists are values and are dropped), `depends_on`/`child_map` lineage as links and
  as `table → transformed_into → table` lineage edges. Descriptions fill the catalog only where the
  crawler precedence allows (never over user, model, source or reviewed text); a dbt `pii` tag or
  `meta.contains_pii` adds `pii` to the column (tags only tighten).
* **Superset metadata**: datasets (columns, metrics), charts (type, dataset, dashboards) and
  dashboards (charts). Objects AnalystOS published for *other* workspaces (`aos_<ws>_`,
  `aos-<ws>-`, `AnalystOS Analytics (<ws>)`) are never read in; `include` globs narrow further.
  Chart params and virtual-dataset SQL are not copied (adhoc filters hold values).
* **Documents**: `.md`, `.markdown`, `.txt`, `.pdf`, ≤ 10 MiB, type checked by extension and
  content; an uploaded Markdown file's own frontmatter is dropped; credentials/e-mails/card numbers
  redacted; instruction-like lines replaced; bundle-internal links reduced to text; PDFs through
  `pypdf` when installed (`documents` extra, not added to the base install) or a built-in extractor
  for simple-font PDFs (no CID/Type0 fonts; such a PDF is refused with a message).

**Facet-level failure** (`services/facets.py`): after connect/discover/diff/apply, every crawl stage
is a facet — `profile`, `relationships`, `glossary`, `enrich`, `knowledge`, `query_history`,
`graph` (and `superset.datasets|charts|dashboards`, `dbt.documents|catalog|lineage` for the other
sources). A failing facet is recorded in `crawl_run.stats.facets` (`status`, `code`, `error`) and
`stats.failed_facets`, logged on the crawl, emitted as `crawl.facet_failed`, and the crawl still
succeeds. Catalog-writing facets run in a savepoint so a half-done facet leaves nothing behind.
