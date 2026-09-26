# ADR-0021 — Published workflow versions; triggers run pinned versions, never the draft

**Status:** proposed (2026-09-26, spec v4 §8; tracker P7-03). Extends ADR-0011 (capabilities and
playbooks) and ADR-0009 (scheduling). Source: the
[2026-09-26 comparison review](../../70-reviews/2026-09-26-agent-os-comparison-review.md) §12–13.

**Context.** Playbooks are versioned data (`capabilities/playbook.py`), the registry swaps immutable
snapshots so in-flight runs keep their versions (`capabilities/registry.py`), and scheduled
re-analysis replays the hypothesis registry without a model call by default (P4-T05,
`services/schedules.py::_reanalysis`). Three gaps remain:

* A run binds exact capability versions when its plan materializes (`capabilities/binding.py`),
  but a schedule stores only the playbook **id** and each fire creates a new run
  (`services/schedules.py::_reanalysis` → `services/runs.py`), which binds whatever version is
  current. A fire after a pack upgrade silently runs the new version.
* Nothing pins the semantic metric versions, method versions or `AnalysisSpec` a scheduled run
  used, so an approval of `mttr_hours v4` changes what next Monday's "same" report measures.
* Workspace-authored playbooks, pipelines (`PipelineSpec`) and ML specs (`MLSpec`) are coming
  (P4-X03, P5, P6) and have no draft/published distinction: whatever is being edited is what runs.

**Decision.**

1. **Every executable definition has draft and published versions.** Playbooks, pipeline recipes
   (ADR-0023), ML specs (ADR-0024) and saved analyses are stored as `definition` rows with
   `(id, version, status: draft|published|retired, content_hash, published_by, published_at)`.
   Editing creates or updates a draft; **Publish** freezes an immutable version with the exact
   step configs, capability versions, model-policy purposes, semantic model version and
   input/output contracts. Built-in and pack YAML is published by definition (its version is the
   manifest's `version` plus content digest).
2. **Triggers run published versions only.** API calls, schedules, monitors and MCP invocations
   name `(id, version)` and are refused for a draft unless the workspace is marked
   `environment: dev`. Runs already record `id@version` refs for everything they bind
   (`capabilities/binding.py`); this adds the `content_hash` and extends binding to semantic model,
   metric and method versions.
3. **Schedules pin a frozen analysis.** A schedule stores the published definition version plus
   the pinned semantic model version, metric versions and the compiled `AnalysisSpec`/`SemanticQuery`
   set of its baseline run. A fire re-executes that frozen set on new data (the existing replay
   path). It does not send the objective back to a planner. Novelty (new hypotheses) stays opt-in
   and is reported as *new*, never merged silently into the pinned set.
   The fire computes deltas against the previous fire in code: new, persisting, changed and
   resolved findings (already produced by `services/changes.py`). It says "nothing changed"
   explicitly when that's true. A narrative written for an earlier fire is marked *stale* until it
   is regenerated from the new bound facts.
4. **Upgrades are explicit.** When a newer version of anything a schedule pins is published or
   approved, the schedule shows *upgrade available* with a diff (definition diff, metric
   definition diff). The owner accepts, which creates a new schedule revision and a new baseline;
   until then the schedule keeps running the pinned versions. A pinned version that is deprecated
   keeps running with a warning; one that is **retired or rejected for correctness** blocks the
   schedule and notifies the owner.
5. **Promotion across environments** (dev → test → prod workspaces) copies the published
   definition by content hash; connections and secrets are bound per environment
   (`secret_ref: env:NAME` already), so the definition is identical and only its bindings differ.
6. **Durable execution stays Temporal's job** (ADR-0005). Idempotency keys are
   `(run, step, operation)`; side-effect steps keep hash-bound approvals (CLAUDE.md rule 5). Nothing
   here adds a second orchestrator.

**Consequences.** A dashboard fed by a schedule measures the same definition every week until
someone changes it on purpose; the change is visible and reviewable. Verdicts from pinned runs are
comparable across weeks because their fingerprints (ADR-0020) differ only in the `data` dependency.
The cost is an upgrade step owners must take, and storage for definition versions.
