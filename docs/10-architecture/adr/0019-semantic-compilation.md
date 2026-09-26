# ADR-0019 — Semantic compilation: questions about governed metrics compile, they are not prompted

**Status:** proposed (2026-09-26, spec v4 §5; tracker P7-02). Builds on ADR-0013 (Ossie as the
metric format) and the P4-K03 semantic service. Source: the
[2026-09-26 comparison review](../../70-reviews/2026-09-26-agent-os-comparison-review.md) §10.

**Context.** The workspace semantic model is versioned, approval-gated and Ossie-round-trippable
(`semantic/service.py`, `contracts/semantic.py`). The *write* side is governed. The *read* side is
not:

* Ask (`services/ask.py`) sends every question to the `sql_generation` purpose, including questions
  whose answer is an approved metric. The model can restate `mttr_hours` with a different filter,
  grain or denominator; the gateway checks that the SQL is safe, not that it means the approved thing.
* `SemanticRelationship` has columns but no cardinality, so nothing can refuse a join that
  multiplies a measure (the fan-out case). `SemanticMetricDef.expression` is a free SQL string
  executed as-is, and `grain`, `filters` and `dimensions` are descriptive, not enforced.
* No answer says whether it came from an approved definition or was improvised. A correct ad-hoc
  answer and an approved-metric answer look the same in the UI.

**Decision.**

1. **A semantic query IR.** `SemanticQuery {metrics[], dimensions[], filters[], time {dimension,
   grain, window}, order, limit}` names approved metrics and model fields only; it holds no SQL.
   The model's job for a governed question is to *choose* a `SemanticQuery` (a structured proposal
   under a new `semantic_query` purpose with a rules rung first: exact metric/synonym match from
   the glossary). The IR is validated against the approved model version before anything runs.
2. **A deterministic compiler** (`semantic/compiler.py`) turns an IR plus a pinned semantic model
   version into sqlglot trees for the source dialect, reusing `skills/sqlbuild` quoting and dialect
   rules. Row filters and column masks from the workspace policy are applied *in the compiler*, then
   the statement goes through `QueryGateway.execute` as today (one gateway, CLAUDE.md rule 4).
3. **Join safety.** Relationships gain `cardinality` (`one_to_one | many_to_one | one_to_many |
   many_to_many`) and `validated_at`/`validated_by` (Ossie custom extension, like the other
   AnalystOS-only fields). The compiler refuses a query in which an additive measure crosses a
   `one_to_many` or `many_to_many` edge without a declared pre-aggregation, and refuses joins over
   unvalidated relationships for approved metrics. Cardinality is validated from data (distinctness
   and overlap through the gateway, ported from Atlas relationship review — tracker P7-09), never
   taken from a model.
4. **Governed vs ad hoc is a visible label, not a tone.** Every Ask answer, finding and chart
   records `governance: governed | ad_hoc`. `governed` requires a compiled `SemanticQuery` against
   an approved model version and carries `semantic_model_version` and `compiler_version`. When the
   question cannot be expressed in the IR, the existing model-SQL path still answers, labelled
   `ad_hoc`, and the UI shows no certified badge. An `ad_hoc` answer can be promoted to a metric
   through the existing proposal/approval path (`PROMOTE_TARGETS`), never automatically.
5. **Metric expressions stay SQL for now** (Ossie's `expressions`), parsed once at approval into a
   sqlglot tree that must reference only its declared `dataset` columns; approval refuses an
   expression that doesn't parse for the declared dialect.

**Consequences.** The same question about an approved metric returns the same SQL for the same
model version, so it can be cached (L0) and scheduled without drift. Relationship cardinality
becomes a required review step before multi-table metrics are approvable. The fan-out refusal will
reject some queries users expect to work; the refusal names the edge and the fix (pre-aggregate or
a declared distinct measure). Ad-hoc answers are no longer mistaken for governed ones. The Ask
benchmark (P4-V02) gains a governed-question slice scored on SQL equivalence to the compiler output.

**Not chosen.** Adopting an external semantic engine (WrenAI MDL engine, dbt MetricFlow, Cube) as
the runtime: each would make the governed path opaque to our gateway, policy and evidence code.
Their definitions stay importable (dbt already is, `semantic/dbt.py`).
