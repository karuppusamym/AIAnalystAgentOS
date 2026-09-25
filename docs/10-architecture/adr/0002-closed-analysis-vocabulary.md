# ADR-0002 — Hypotheses are executable specs in a closed vocabulary

**Status:** accepted

**Context.** Letting the model write SQL for each hypothesis makes findings irreproducible, invites
injection via catalog text, and makes statistical method choice implicit.

**Decision.** The investigator must output `AnalysisSpec` (6 methods × 9 derivations + filters).
Specs are validated against scope and type compatibility; the compiler emits dialect SQL with
aggregation pushdown; the method determines the statistical test. Free-form SQL exists only for
the ad-hoc "Ask" feature, still gateway-validated with a repair loop.

**Consequences.** + deterministic, testable, reproducible; benchmarks with planted effects are
possible. − hypotheses outside the vocabulary cannot be tested yet (extend the vocabulary, or the
sandbox tool for custom numeric code).
