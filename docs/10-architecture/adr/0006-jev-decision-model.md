# ADR-0006 — JEV (TypeSafe decision model) for bounded decisions

**Status:** accepted (spec v1 note: "use JEV model decisioning")

**Context.** Many agent decisions are choices among known-valid options (which hypothesis first,
which chart, is this request consequential, is the objective answered). Chat completions return
unstructured text and no calibrated confidence.

**Decision.** Route these to `typesafe/jev-1.13` (pinned) via the OpenRouter Decisions API as
purposes of the `decision` profile: `hypothesis_priority` (score), `risk_check` (noul),
`feedback_classification` (choice), `chart_selection` (choice), `rev_second_opinion` (noul),
`stop_check` (noul). Rules: options offered are already platform-valid; JEV can rank, choose or
escalate, never grant or remove a gate; only trusted text enters `state`; every call is logged;
unavailability falls back to deterministic rules recorded as `by: rules`.

**Consequences.** Decisions carry probabilities in the audit trail (e.g. chart override only when
p ≥ 0.6). Cost is small (~$0.00001–0.00002 per call observed).
