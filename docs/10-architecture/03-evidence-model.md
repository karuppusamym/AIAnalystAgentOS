# Evidence model for findings (P4-03)

Implements target spec §4 ("Evidence that matches the claim") and the evidence part of
[ADR-0011](adr/0011-workspace-workflows-and-evidence.md) for analyst findings. Code:
`src/analystos/contracts/evidence.py` (contracts, exported as `contracts/evidence_bundle.schema.json`,
`data_manifest.schema.json`, `fact.schema.json`) and `src/analystos/evidence/{facts,bundle,confirmation,manifest}.py`.
Everything here is computed on the deterministic path; a model can only propose wording, which must bind.

## 1. Typed facts and narrative binding

`Method.facts(spec, stat)` (default: `evidence.facts.facts_from`) turns a `StatResult` into typed facts:
value, unit (`fraction`, `percent`, `points`, `ratio`, `count`, `hours`/`days`/`minutes`/`seconds`, `value`,
`coefficient`, `probability`), kind (level, comparison, change, share, count, statistic), subject group,
baseline, direction, window, method, query ids and result hashes. The outcome unit comes from the derivation
(`duration_hours` → hours) or a unit-named column (`shipping_days` → days).

`bind(text, facts)` maps every number in the title, finding and action to a fact, and fails with a reason for:

| Problem | Example (facts: P1 41.2 %, P4 13.1 %, ratio 3.14 P1/P4) |
|---|---|
| invented number | "57 %" |
| wrong unit | "41.2" (fraction written without %), "0.4x", "314 %", "12 days" for 12 hours |
| wrong group | "41.2 % for P4" (nearest group label in the clause is not the fact's subject or baseline) |
| wrong direction | "P1 is 3.1x lower than P4", "rose by 44.2 %" for a fall, "concentrates in P4" |
| unstated direction | "changed by 44.2 %" for a signed change |

Unit conversions that are correct pass ("0.5 days" for 12 hours). A model narrative that does not bind
is replaced by the method template; REV re-binds the final wording (`fact_binding` check), so a template that
does not bind fails verification. The binding found a template defect: `trend` printed a fitted change
of −0.4416 as "−0.4 %"; it now reads "fell by 44.2 %".

## 2. Evidence dimensions and missing-evidence refusal

REV (`agents/critic.py`) stores a versioned `EvidenceBundle` (`insight.evidence_bundle`):

* **data** — asset, governed query receipts (query sha256, result hash, gateway fingerprint), filters,
  excluded rows, population check, the run's manifest entry for the asset;
* **claim** — facts, binding, subject/baseline/direction, spec hash, which text was rendered from where;
* **method** — effect size and practical threshold, uncertainty (primary CI, per-group CIs or the second
  method's CI), sample sizes by group, Benjamini-Hochberg family (size, p, q, α), selection procedure
  (origin, round, parent, post-hoc top-group selection, excluded segments), stated assumptions and warnings,
  second method, and power at the materiality threshold where a closed form exists (two proportions);
* **validation** — state, label, check-level pass/fail/not_applicable, confirmation result;
* **limits** — population caveats, unidentified confounding, untested slices, replay exactness;
* **review_score** — the old confidence, `calibrated: false`.

Required for every finding: query receipts with result hashes, a manifest entry, the population check,
facts, a binding, effect size, sample sizes, the method-fit check; the multiple-testing family when there is a
p-value; an uncertainty interval unless the method lists it in `AnalysisMethod.evidence_optional`. Anything
missing → check `evidence_complete` fails, state `insufficient_evidence`, not verified. A finding with no
query records can no longer pass `reproducible_rerun` (it used to, vacuously).

States: `insufficient_evidence` (refused) → `invalid` (fact binding or method fit failed) → `inconclusive`
(any other check failed) → `confirmed` (a rule passed) → `replicated` (pre-registered re-test on the same data
version) → `exploratory`. `verified` still means "all REV checks passed"; it is not confirmation.

## 3. Discovery vs confirmation rules

Every finding of an adaptive round is a **discovery** (`exploratory`), however small its q-value. Only these
rules set `confirmed` (label `confirmation`); `contracts.evidence.Validation` refuses the state otherwise.

1. `holdout_partition` — the claim was locked before an untouched partition was read (`claim_locked_at <
   partition_accessed_at`), and the partition supports it with the same top group and direction. The rule is
   implemented and tested; no built-in method produces a holdout record yet (driver models' holdout AUC is
   recorded as `predictive_evaluated`, not as confirmation).
2. `fresh_snapshot_replication` — a pre-registered claim (hypothesis origin `registry` or `carried`: the spec
   was fixed by an earlier run) is re-verified on a **different immutable data version** than the earlier
   verified finding's manifest entry, with the same top group and direction. Legacy findings (no manifest)
   and pushdown sources (no fixed version) cannot satisfy it. The bundle records the limit that a new
   snapshot usually shares rows with the old one.

A re-test on the same data version is reproducibility (`replicated`), never confirmation. The Monte Carlo
evidence for the rule is in the method evaluation (§5): testing a post-hoc selected group on the selecting
data has a false-positive rate of ~0.22 at α = 0.05; on a held-out half it is ~α.

## 4. Data-version manifest and staleness

`analysis_run.data_manifest` records, per analysed asset: source, mode, load id, rows, source total, content
fingerprint, structure fingerprint, sampling method and window, staged time, and a version. The loader computes
an order-independent content fingerprint of the staged rows (`staging.loader.ContentFingerprint`), so a
re-stage of identical data keeps its version and any changed, added or removed row changes it. Older snapshots
without a fingerprint are versioned by load metadata (every re-stage is a new version). Pushdown sources record
the observation time and have no version (best-effort replay). `insight.data_version` is the manifest version.

REV fails `data_version_stable` when the snapshot changed between drafting and verification. When an asset is
re-staged (source selection or the crawler), `evidence.manifest.mark_stale` sets `insight.stale_since` and
`evidence_bundle.freshness` on findings bound to the old version and emits `insight.stale`. Stale findings keep
their evidence, are shown as "stale: needs re-verification" in reports, and are refused by the learning loop
and Attested Computation export until re-verified.

## 5. Method-specific evaluation

`evaluation/method_checks.py` (`scripts/benchmark_methods.py`, CI test `tests/benchmarks/test_method_evaluation.py`)
measures per method the supported rate under the null, power at 0.5–3× the materiality threshold, the winner's
curse on the quoted top/bottom ratio, and same-data vs held-out testing of a post-hoc group. The V01 component
suite now reports precision, null calibration and FDR under the global null **per method**, and its CI check
applies the α bound per method. Dated evidence: `docs/60-delivery/evidence/2026-09-26-method-evaluation.md`.

## 6. Legacy badges

Migration `0030` maps every existing finding onto the model without upgrading it: `verified` → state `legacy`
(`equivalent: exploratory`; checks, reproducibility and the confidence kept with verifier version `rev.v1`),
`failed_verification` → `inconclusive`, `rejected` → `invalid`, anything else → `legacy` (unverified).
The mapping is a copy of `evidence.bundle.legacy_bundle` (tested equal) so the migration does not drift.

## 7. Verification records that void themselves (P7-01, ADR-0020)

Code: `src/analystos/evidence/verification.py`, tables `verification_record`, `verification_dependency`
(indexed on `kind, ref`) and `verification_sweep` (migration `0032`).

REV (`agents/critic.py`) writes one record per verdict (verified or failed) with the dependencies it saw:

| kind | ref | version |
|---|---|---|
| `query` | the step (today the hypothesis id) | spec hash + sha256 of each primary query's SQL + `sqlbuild.v1` + dialects |
| `data` | `<source_id>/<schema.table>` | the run's manifest entry version (P4-03) |
| `semantic` | `<workspace_id>/<metric>` | the approved version (id + content hash) of each live KPI on the outcome column, or "none approved" |
| `method` | method name | manifest id + version + sha256 of the method module |
| `context` | glossary entry / knowledge document id | its content hash (terms the run resolved onto the claim's columns; glossary, rule and metric sections of the narrative's model calls) |
| `model_call` | model call id (narrative written by a model only) | purpose + model + prompt version |
| `policy` | workspace id | hash of `restricted_columns, pii_columns, pii_access, attribute_rules, max_rows, alpha` |

`fingerprint = sha256(canonical_json(sorted(dependencies)))`. A new verdict on the same subject supersedes
the live one; a replan supersedes the run's verdicts.

**Voiding is event-driven, in the transaction of the change:** `mark_stale` (snapshot re-staged; P4-03's
`stale` is now the `data` case of `VOID`), `semantic.apply_decision` / `deprecate_metric`, `save_policy`,
`knowledge.index.index_pack` (every knowledge revision), and `POST /api/admin/capabilities/reload` (method
versions) call `dependency_changed` / `recheck`. `void_dependents(session, kind, ref, new_version, reason)` is
the public hook for later change paths (editing a step, P7-04). The scheduler runs `sweep` once a day:
it recomputes every live dependency and voids what an event missed; each is a **late void** (the sweep row's
`late_voids`, a `verification.sweep_completed` event and a warning log), a defect in some change path.

**Consumers read the state:** the publish gate (`publisher.build_bundle` → `gate_publish`) refuses a chart
that presents a VOID finding; reports never present a VOID finding as verified (label `VOID: <cause>`, listed
in the executive report too); the Attested Computation export and the learning loop refuse VOID; the insight
APIs and the MCP finding tool return `verification_state`. Presentation: a void carries its cause; earlier
verdicts on the same question (`question_hash` = spec hash) are listed as `prior_verdicts` with
`offered: true, applied: false`; flagging a finding *wrong* (`POST /api/insights/{id}/outcome` with
`signal: wrong`, or run feedback `reject_finding`) requires a reason, stored in the record's `flags` and
drafted as Negative Knowledge.

Migration `0032` maps existing verified findings: stale → `VOID(data)`; with a manifest entry → `ACTIVE`
with the rebuilt `data` dependency; otherwise `LEGACY` (shown without a verification badge).

## 8. "Why this number?" (P7-08)

`GET /api/insights/{id}/why[?number=<text>|fact_id=<id>]` and `GET /api/workspaces/{ws}/analysis/{run}/why`
(`evidence/why.py`). Each displayed number is re-bound to the finding's facts and resolved through six
links, each with its current state (`ok | changed | void | failed | broken | unknown | not_applicable`);
the number's `state` is its worst link. Nothing is dropped: a broken or voided link is returned with a reason.

```json
{"subject": {"type": "insight", "id": "...", "code": "I-2", "run_id": "...", "title": "...", "finding": "...", "status": "verified"},
 "verification_state": {"state": "VOID", "badge": "void", "record_id": "ver_...", "void": {"kind": "data", "reason": "...", "at": "..."}, "...": "..."},
 "state": "void",
 "numbers": [{"text": "41.2%", "value": 41.2, "unit": "percent", "state": "void", "links": [
   {"link": "fact", "state": "ok", "reason": null, "detail": {"fact_id": "...", "role": "top_rate", "value": 0.412, "unit": "fraction", "subject": "P1", "...": "..."}},
   {"link": "step", "state": "ok", "detail": {"hypothesis_id": "...", "code": "H-2", "method": "rate_by_segment", "method_version": "1.0.0", "spec_hash": "...", "experiment_id": "..."}},
   {"link": "query_receipt", "state": "ok", "detail": {"queries": [{"query_id": "...", "kind": "sql", "sql": "...", "query_hash": "...", "result_hash": "...", "recorded_result_hash": "...", "rows": 4, "state": "ok"}]}},
   {"link": "data_version", "state": "changed", "reason": "snapshot of shop.orders changed (...)", "detail": {"asset": "...", "recorded_version": "...", "current_version": "..."}},
   {"link": "semantic_version", "state": "not_applicable", "detail": {"metrics": []}},
   {"link": "verdict", "state": "void", "reason": "data: data snapshot shop.orders changed (...)", "detail": {"record_id": "...", "state": "VOID", "...": "..."}}]}]}
```

The run form returns `{"run_id", "findings": [<the object above per verified finding>], "numbers": n, "by_state": {"ok": n}}`.
