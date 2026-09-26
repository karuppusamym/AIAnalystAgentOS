# ADR-0020 — Verdicts carry a dependency fingerprint and void themselves when it changes

**Status:** proposed (2026-09-26, spec v4 §6; tracker P7-01). Extends ADR-0008 (what "verified"
means) and P4-03 (evidence bundle, data-version manifest). Source: the
[2026-09-26 comparison review](../../70-reviews/2026-09-26-agent-os-comparison-review.md) §11.

**Context.** ADR-0008 makes a verdict deterministic; P4-03 binds each finding to a data-version
manifest and marks it **stale** when the snapshot it read changes (`evidence/manifest.py`). Data is
one dependency of a verdict. The others are not tracked, so a green badge can outlive a change to:

* the SQL or `AnalysisSpec` that produced the numbers (a user edits and re-runs one step);
* the approved semantic definition it used (a new metric version is approved, or the old one deprecated);
* the method or skill code (a new `methods/*.yaml` version with a different threshold);
* the context it was grounded on (a glossary term or business rule the step cited is edited);
* the model/prompt version of any model call whose output survived into the claim.

Replanning already supersedes a run's hypotheses and insights and invalidates its approvals
(`runtime/engine.py`), but only for that run and only on replan.

**Decision.**

1. **`VerificationRecord`** (new table, one row per verdict): `subject` (finding, insight, chart,
   KPI tile, report section), `verdict`, `checks[]` (the ADR-0008 checks with values),
   `verifier` (`rev.v<version>` or a named person), `evidence_bundle_id`, `fingerprint`,
   `dependencies[]`, `state`, `created_at`, `voided_at`, `void_reason`.
2. **The fingerprint** is `sha256(canonical_json(sorted(dependencies)))` where each dependency is
   a typed pair `{kind, ref, version_hash}`:
   `query` (normalized SQL or `SemanticQuery` hash + compiler version + dialect) ·
   `data` (the manifest entry's version) · `semantic` (model version, each metric version) ·
   `method` (method id + version + code digest) · `context` (receipt ids + section hashes from the
   context compiler) · `model_call` (purpose, model id, prompt template version) — only for calls
   whose output is part of the claim · `policy` (policy version that shaped scope or masking).
   Canonical JSON and SHA-256 follow the approval-hash format (ADR-0017, `contracts/approval_hash.md`).
3. **States:** `PENDING → ACTIVE`; `ACTIVE → VOID` when any dependency's current version differs
   from the recorded one; `ACTIVE → SUPERSEDED` when a newer verdict exists for the same subject.
   A voided verdict is never carried forward; it stays readable for comparison.
4. **Invalidation is event-driven and conservative.** The events that change a dependency
   (`semantic.metric_approved|deprecated`, `snapshot.version_changed`, `step.edited`,
   `method.version_changed`, `knowledge.section_changed`, `policy.changed`) look up records by an
   indexed `(kind, ref)` dependency table and void them in the same transaction as the change.
   A nightly sweep recomputes current versions for every `ACTIVE` record and voids any that the
   events missed; the sweep's count of late voids is a monitored defect metric.
5. **Consumers check state, not the old boolean.** The publish gate, report generation, the
   "verified" badge, monitors that compare to a baseline, and export refuse or relabel a `VOID`
   subject. Re-verification is an explicit action (or a scheduled replay, ADR-0021) that creates a
   new record; it never edits the old one.
6. P4-03's `stale` becomes the `data`-kind case of `VOID`; migration maps existing stale findings
   to `VOID(reason=data)` and existing verified findings to `ACTIVE` records with the dependencies
   that can be reconstructed. Records whose dependencies can't be reconstructed are labelled
   `legacy` and shown without a verification badge.

7. **Presentation rules (patterns seen in AgentSwarms, re-implemented).**
   * **A void is shown, never hidden:** the badge turns *void* with its cause and stays visible.
   * **Offered, not applied:** when the same question is asked again, a prior matching verdict is
     offered for comparison and never attached to the new answer.
   * **A reason is required:** marking a finding *wrong* needs one, which is stored with the record
     and feeds negative knowledge.

**Consequences.** "Change the SQL or the metric and watch the badge go away" becomes a test,
not a promise. More verdicts will void than users expect (a glossary edit voids findings that cited
it); the UI groups voids by cause and offers one-click re-verification. Pushdown sources with no
data version (P4-03 limit) produce records whose `data` dependency is an observation interval; they
can be `ACTIVE` but never claim exact replay. Storage grows by one row per verdict plus its
dependency rows.
