# ADR-0023 — Transformation recipes: one typed IR, compiled to the engine that runs it

**Status:** proposed (2026-09-26, spec v4 §9; tracker P6-04..P6-07). Refines the "PipelineSpec"
of the [workspace spec](../../00-intent/03-workspace-data-team-spec.md) §5 and ADR-0014 (ELT on
customer engines). Source: the
[2026-09-26 comparison review](../../70-reviews/2026-09-26-agent-os-comparison-review.md) §16.

**Context.** The platform can already stage sources (`staging/loader.py`), build virtual datasets,
and emit and run dbt projects on a customer engine behind a BuildGateway (`build/`). What it lacks
is a single representation of a transformation that can be validated before it runs, compiled to
more than one engine, tested for fan-out and reconciled afterwards. DataPilot (donor, ADR-0018) has
a join-policy planner and dbt/Dataform emitters (`pipeline_codegen/`), but it builds SQL with
f-strings straight from the planner. The 2026-09-26 audit judged it weaker than `build/`, so only
the Dataform `.sqlx` layout is kept as an optional idea.

**Decision.**

1. **Recipe IR** (`contracts/recipe.py`): an ordered DAG of typed nodes `source(asset, snapshot)`,
   `select`, `filter`, `cast`, `derive(expr)`, `rename`, `dedupe(keys, order)`, `join(left, right,
   on, expected_cardinality)`, `aggregate(keys, measures)`, `window`, `union`, `pivot`, `output(grain,
   keys, schema)`. Expressions are sqlglot trees (the same parser the gateway uses), never strings.
   Every node carries its declared output schema; the IR is rejected if a column is referenced
   before it exists or a type change isn't explicit.
2. **Compilers**, one per target, each with its own test suite: `sql` (per dialect, via
   `skills/sqlbuild` rules → executed through `QueryGateway` for previews and virtual datasets),
   `duckdb`/`polars` (over an immutable snapshot artifact in a `compute-py` worker, ADR-0022) and
   `dbt` (a model per output node, emitted into the existing `build/` project for the customer's
   runner; `build/project.py` already renders dbt with sqlglot and guards hooks and macros, so
   DataPilot's f-string emitters are not ported). `spark` is reserved and not implemented until a pilot needs it.
3. **Pushdown first** (ADR-0004): the planner compiles to the source's SQL when every node is
   supported by that dialect and the inputs share a source; it falls back to a snapshot + worker
   only for cross-source recipes or unsupported nodes, and records why.
4. **Checks are part of the recipe, not afterthoughts.** `join` nodes run a pre-flight
   cardinality test (distinct keys, overlap, row multiplication) and refuse to run when the observed
   cardinality violates the declared one. Each `output` declares data-quality gates
   (`not_null, unique, accepted_values, range, referential, freshness, row_count_delta,
   aggregate_reconciliation`), each with a severity. A failing `fail` gate quarantines the candidate
   output and keeps the previous good version (DataPilot's quarantine idea, rewritten to read through
   the gateway and write through the loader). Gate severity is `fail` (block and quarantine), `warn` (evidence only) or `drop` (remove the
   failing rows into quarantine and continue, counted and shown). A schema policy per output,
   `evolve | warn | strict`, decides what an upstream column change does.
5. **Executed lineage.** Each compiled recipe emits column-level lineage from the IR (inputs →
   outputs per column, not guessed from SQL text) as OpenLineage events (ADR-0013), linked to the
   recipe version and run.
6. **Lifecycle** follows ADR-0021: recipes are drafts until published; schedules run published
   versions; materializing to a destination is a side effect under a hash-bound approval with the
   separate writer identity of ADR-0011 (workspace).
7. **Incremental** follows ADR-0016: a recipe may declare `incremental {watermark, key,
   late_window, deletes: reconcile|soft|ignore}` only when its source declares a reliable
   watermark; otherwise it's a full rebuild. Retries use the fixed-cursor rule from that spike. The new
   watermark is committed **after** the durable load, in the same transaction as the merge, so a
   crash between them re-reads a window instead of skipping one.

**Consequences.** One recipe can preview in-source, run on a snapshot, or ship as dbt, with the
same checks and lineage in each case. The IR is smaller than SQL on purpose; anything it can't
express stays a reviewed hand-written dbt model (imported, lineage from the manifest). Models may
*propose* recipe nodes; the IR validator and compilers decide (CLAUDE.md rule 3).
