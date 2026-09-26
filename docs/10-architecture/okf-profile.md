# AnalystOS OKF v0.2 profile — knowledge packs, index, import/export, providers

Delivered for tracker rows P4-K01, P4-K02, P4-K09 and P4-K10 (ADR-0013; amends ADR-0007), extended
by P4-K05, K07 and K08 (sections at the end).
Code: `src/analystos/knowledge/`. Tests: `tests/unit/test_knowledge_okf.py`,
`tests/integration/test_knowledge_pack.py`, `tests/unit/test_context_k05.py`,
`tests/integration/test_knowledge_k05_k08.py`. Evidence:
[`docs/60-delivery/evidence/2026-09-25-knowledge-k01-k10.md`](../60-delivery/evidence/2026-09-25-knowledge-k01-k10.md),
[`docs/60-delivery/evidence/2026-09-25-knowledge-k05-k08.md`](../60-delivery/evidence/2026-09-25-knowledge-k05-k08.md).

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
relaxed_order` then an exact re-sort. P4-K05 replaced the lexical leg with Okapi BM25 (below); the
K01 `ts_rank_cd` leg stays selectable (`lexical="ts_rank_cd"`) for the benchmark.

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
links and code fences are allowed (Atlas's stricter rules are Atlas's). P4-U04 added the download
route (`GET .../knowledge/packs/{id}/export`, audited as `knowledge.exported`): the zip goes back to
the requesting user's browser, which is not a write outside the platform. Anything that *does*
write outside — a push to a git remote — stays the K01 hash-bound approval (`push/request` → inbox
→ `push`), see "Knowledge studio" below.

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

## Context compiler over the pack (K05)

`context/compiler.py` (`load_knowledge(..., query=)`, `pack_section_items`, `rank_items`) and
`knowledge/index.py` (`retrieve(..., lexical="bm25", hop=True)`):

* **BM25.** Okapi BM25 (k1 = 1.2, b = 0.75, idf = ln(1 + (N − df + 0.5)/(df + 0.5))) computed in
  Postgres over each section's `tsvector` lexemes (english stemming; term frequency = positions;
  document length = lexeme occurrences). Every section containing a query lexeme matches the OR
  query, so document frequencies are counted over that match set; N and the mean length are
  per-pack statistics written to the index state (`pack:<id>.bm25`) when a pack is indexed (computed
  live for older states). The BM25 leg also reports each section's share of the question's
  lexemes (`lexical_share`). Fused with the vector leg by reciprocal rank (k = 60), as before.
* **One hop.** The first section of each document that one of the top 5 hits links to (resolved
  internal links, same pack) gains a quarter of the linking hit's score — added to its own score,
  or as a new hit — marked `via`; a hop never lifts a section above the hit that links to it.
* **Section-level items.** With a query, pack candidates are the index's *section* hits (not whole
  documents): item id `<document_id>#<anchor>`, name `Title § Heading` for non-generic headings,
  excerpts built from the section sentences that mention the question (`focused_excerpt`). Receipts
  carry `document_id`, `path`, `anchor`, the document `sha256`, `section_sha256`, the index rank and
  the fused score, and `via` for a hop. Workspace `context_entry` rows are still whole entries.
* **Ranking and gate.** Within the compiler an item's rank is the reciprocal-rank fusion of its
  term-overlap rank (T03's `item_score`) and its index rank. It reaches a prompt when its term
  overlap with the focus terms (generic table-name words excluded) passes `min_relevance`, or it
  was reached by one hop from an item that passed; the index rank orders, it never admits on its
  own (a first version admitted top-3 lexical matches and, in the token measurement, a section
  matched only on the table word `incident` pulled a second table into the SQL prompt).
  `NO_MATCH` otherwise, as in T03.
* **Memory and external knowledge in their own sections.** `external` (other providers' results,
  K09: from the run's context package, never a second provider call per prompt; untrusted and
  marked `trusted: false`), `prior_findings`, `negative_knowledge` (rejected hypotheses and review
  rejections) and `episodes` are *supplementary*: filled after the primary sections (glossary,
  business rules, metrics) whatever the profile order, each capped at `supplementary_share`
  (default 12%) of the budget, and given back first when the omitted note needs room. The
  planning and hypothesis-generation profiles now include `external` and `episodes` (memory was
  write-only before). Tested: episodes listed first, plentiful and maximally relevant, cannot
  displace a glossary term (`test_episodes_cannot_crowd_out_glossary_terms`).
* **API.** `POST /api/workspaces/{id}/knowledge/context` previews the compiled knowledge for a
  purpose and question (no model call); `GET /api/runs/{id}/context-receipts` lists each model
  call's receipts. The UI (P4-U02/U04) renders them.

## Review queue (K07) and the learning loop (K08)

`knowledge/suggestions.py`, `knowledge/learning.py`, table `knowledge_suggestion` (migration 0024).
A draft has a `kind` (`term`, `definition`, `metric`, `rule`, `note`, `negative`,
`attested_computation`, `table_description`), a `subject` (`asset:<id>`, `insight:<id>`,
`metric:<name>@v<n>`, `feedback:<id>`), the workspace-pack `path` an approval writes, and
**per-field** `{value, confidence, provenance}` (the row's `confidence` is its least confident
field). Drafts never reach a prompt.

* **Sources.** Crawler enrichment (the model's descriptions still fill placeholders as in
  increment 3; each is also queued with the model id, purpose, prompt version and crawl run as
  provenance, the model's own confidence clamped and capped at 0.9 — 0.5 when it gives none — and
  the rule-derived table role at the rules' confidence). The learning loop: an accepted verified
  finding (`POST /api/insights/{id}/outcome` accept) → a draft Attested Computation; an approved
  semantic metric → a draft `Metric`; run feedback `add_context` → a `Note`, `redirect` /
  `deeper_analysis` → a `Note` on the analysts' focus, `reject_finding` → a `Negative Knowledge`
  draft. Each draft has a lineage edge `knowledge_suggestion —derived_from→ <subject>` and a
  `knowledge.suggestion_proposed` event.
* **Review.** `GET /api/workspaces/{id}/knowledge/suggestions` (viewer) lists the queue;
  `POST .../suggestions/review` (editor) takes a batch of `approve`, `edit` (edit-then-approve:
  edited fields become `provenance.source: human`, confidence 1.0) and `reject` decisions. All
  accepted decisions land in **one** workspace-pack revision; approved documents are OKF v0.2 with
  `verified: [{by: human:<user>}]` (human-reviewed tier) and the draft's provenance under
  `analystos.review`. A rejection writes a `Negative Knowledge` document (what was proposed, that a
  reviewer rejected it, why) under `negative/`, which later prompts see in `negative_knowledge`.
* **Invariants.** A draft never replaces a document the queue did not write (owner or migrated
  content): proposing is skipped, approving is refused (`owner_content_exists`). Identical content
  already decided is never proposed again; a subject's rejected primary value is never proposed
  again, and the crawler sends a table's rejected descriptions to the model as `rejected` and drops
  a repeat. Catalog side of a table description: approving sets the description (origin `model`,
  or `user` when edited) and marks the asset reviewed — only while it is not reviewed and not
  owner/source text; rejecting restores the previous placeholder if the model text is still there.
  Writes stay inside the platform (the pack), so no approval object is involved; pushing the pack
  out is the K01 hash-bound approval.

## Attested Computation (K08 minimal shape; P4-K04 extends it)

`knowledge/attested.py` writes and validates:

```yaml
type: Attested Computation
title: <finding title>
status: draft | stable            # stable once a human approved it in the review queue
stale_after: <ISO instant>        # §5.5; default 90 days after drafting
verified:                         # §5.2
  - {by: process:analystos-rev, at: ...}   # REV's deterministic checks passed
  - {by: human:<user id>, at: ...}         # the reviewer
analystos:
  kind: attested_computation
  computation:
    query_hash: <sha256 of the primary query's SQL as executed>
    result_hash: <the gateway's result hash of that query>
    q_value: <BH-adjusted p-value of the primary test>
    effect_size: {value: <number>, label: <cramers_v | hedges_g | ...>}
    verified_by: process:analystos-rev
    method, n, run_id, insight_id, experiment_id, queries: [{query_id, query_hash, result_hash}]
```

Required: `query_hash`, `result_hash`, `q_value`, `effect_size.value`, `verified_by` and a parseable
`stale_after`; everything else is optional so K04 can add ODCS/OpenLineage references. A draft
missing a required field cannot be approved (`incomplete_computation`). Nothing is executed.

## Knowledge studio (P4-U04)

`knowledge/studio.py`, routes in `api/routers/knowledge.py`, UI on the Knowledge journey screen
(tabs Catalog / Documents / Review queue / Semantic graph / Metrics / Import & export; still 19 of 20
screens). Tests: `tests/integration/test_knowledge_studio.py`, `tests/unit/test_knowledge_studio_rules.py`,
`test_ask_threads_api.py::test_an_approved_ai_suggestion_is_a_receipt_in_the_ask_inspector`,
`web/src/test/knowledge.test.tsx`, Playwright `knowledge studio (P4-U04)`.

| Route (under `/api/workspaces/{id}/knowledge`) | Role | What |
|---|---|---|
| `GET packs` | viewer | visible packs; `writable` only for the workspace pack and an editor |
| `GET packs/{pack}/documents[?revision=]` | viewer | files with type, trust tier, status, staleness, `authorship` (`review_queue` / `owner`) |
| `GET packs/{pack}/document?path=[&revision=]` | viewer | text, frontmatter, body, trust fields, sections, links (dangling flagged) |
| `PUT packs/{pack}/document` | editor | a human edit → one revision (`origin: studio`) |
| `DELETE packs/{pack}/document?path=&base_sha256=` | editor | a deletion → one revision |
| `GET packs/{pack}/revisions[?path=]` | viewer | history with added / changed / removed paths |
| `GET locate?document_id=` | viewer | a receipt's document → pack and path |
| `POST import` (multipart `file`, `slug`) | editor | `bundle.import_bundle` into a read-only imported pack |
| `GET packs/{pack}/export` | viewer | deterministic zip to the caller (publish policy enforced) |
| `POST packs/{pack}/push/request`, `POST packs/{pack}/push` | editor | the K01 approval flow over HTTP |
| `GET graph` | viewer | nodes and edges, each `governed` or inferred, with the reason |

Save rules, enforced by the server: only the workspace pack is writable (platform and imported packs
refuse, owners included); the request names the `base_sha256` it edited (a stale or missing base is
409, so two editors cannot overwrite each other); `type` is required, `stale_after` must parse;
`verified` may keep or drop entries but gains only the saving user's own `human:<id>` entry,
server-stamped (`mark_reviewed`); `analystos.review` is moved to `analystos.reviewed_draft`, so a
document a person edited becomes owner content that no later draft replaces (`suggestions.protected`);
reserved `index.md`/`log.md` are not edited here. Agents have no route to these writes.

Graph edges are governed when an approved semantic model or metric, a validated or user-declared
join, or a human-reviewed document stands behind them; proposed metrics, discovered joins, links and
mappings of unreviewed documents and pending AI suggestions are inferred (dashed in the UI).
