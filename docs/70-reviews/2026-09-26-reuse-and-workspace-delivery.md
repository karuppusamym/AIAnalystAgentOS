# Reuse and configurable workspace delivery

Date: 2026-09-26. Owner direction: shorten development through selective reuse while delivering a useful, understandable, robust product for analysis, engineering and ML. This is a delivery design; implementation status remains in `docs/60-delivery/01-tracker.md`.

## 1. What to reuse and where

| Component inspected | Disposition | AnalystOS destination / work avoided |
|---|---|---|
| AnalystOS `capabilities/enablement.py`, Agent manifests, capability input/output schemas | Reuse existing code | Workspace configuration and agent forms; avoid another permissions/configuration store |
| AnalystOS recipe compilers, gates, ingestion and lineage | Reuse existing code | Pipeline editor and previews; avoid rewriting transformation execution |
| AnalystOS definition pins, work orders, playbooks and orchestrator | Reuse existing code; extend contracts only for missing semantics | Workflow authoring and published runs; avoid a second workflow engine |
| React Flow / XYFlow upstream library | Use official package when building the canvas, retaining MIT notices | Node selection, connection, layout interactions; avoid implementing canvas mechanics. Verify selected version compatibility before installation |
| AgentSwarms `components/etl/SourcePickers.tsx`, `TransformFields.tsx`, `lib/etlGraphEdit.ts` | Redevelop focused catalog-driven pickers and transform forms against AnalystOS APIs | P6-03 workbench; schema-aware column choices, validation and preview |
| AgentSwarms `components/workflows/StepInspector.tsx`, `inspectorFields.tsx`, `lib/workflows.ts` | Redevelop inspector from capability schemas; adopt dependency/trigger requirements | Work item workflow panel; typed inputs and explicit dependency results |
| AgentSwarms `components/swarms/NodeInspector.tsx`, `agentVersions.ts`, `swarmPublish.ts` | Redevelop forms and version comparison on Agent manifests and definitions | Agent authoring; transactional version history, tested publication; no draft fallback for scheduled runs |
| AgentSwarms `components/dashboard/FirstRun.tsx` | Independently implement state-derived checklist | P7-18; next actionable step and honest readiness |
| AgentSwarms ETL checkpoint/backfill/preview behavior | Use as design requirements; implement on the managed loader and pipeline contract | P6-01..03; no preview writes or cursor movement; checkpoint only after durable load |
| AgentSwarms Supabase schema, browser/server swarm executors, ETL-generated credential-bearing runtime | Do not transplant | Existing identity, gateway, approvals and isolated-compute design remain the integration boundary |

No AgentSwarms source is copied by this document. Its upstream ELv2 permits conditional reuse; deleting local notices does not change those conditions. A direct port requires a deployment-compatible license basis and preserved provenance/notices. Under the current project's patterns-only decision, build independent implementations. The independently licensed React Flow library can be sourced directly: https://github.com/xyflow/xyflow/blob/main/LICENSE. AgentSwarms terms: https://github.com/AgentSwarms-fyi/agentswarms/blob/main/LICENSE.md.

The earlier study's statement that TypeScript leaves nothing to port is too broad: both applications have React frontends. Some UI adaptation is technically possible; licensing, Supabase/router coupling and product fit determine whether it actually saves effort.

## 2. Configurable workspaces

One workspace can enable several disciplines. Presets initialize capability selections and defaults; they are not roles or new execution engines. Effective access remains the intersection of user permissions, workspace enablement, certification and runtime readiness. Presets never expand source access or silently authorize writes.

| Preset | Primary work | Outputs | Readiness required |
|---|---|---|---|
| Analysis | Ask, compare, explain changes, monitor | Findings, charts, reports | Authorized data and applicable definitions/methods |
| Data engineering | Ingest, prepare, test, publish datasets | Datasets, recipes, quality and lineage records | Approved targets; loader/build identity; required compute profile |
| ML and experiments | Forecast, train, compare, batch score | Experiments, model cards, versioned models and predictions | Implemented method, isolated compute where required, split/holdout and budget validation |
| Custom / combined | Any explicitly selected available capabilities | Shared output catalog | All relevant requirements above |

Workspace settings should expose: enabled disciplines and job kinds; default domain pack and sources; budget limits; approval policy; compute availability; publication targets. Use existing workspace policy and capability APIs wherever fields exist. Add persisted fields only for choices not representable there, with migration and audit evidence.

Changing a preset previews the capability diff. Explicit disabling wins over dependency defaults. Disabling blocks new starts without deleting data, artifacts or historical runs. Existing schedules must recheck enablement and show a blocked reason. In-flight cancellation is a separate explicit action. Cross-workspace copies rebind resources and never carry secrets or grants.

## 3. Simple product surface

Implement the existing v4 navigation: Overview, Data, Work, Outputs, Operate, plus admin Settings. No separate application for each discipline.

- Overview: readiness and items requiring attention; one Start work action.
- Start work: Ask a question, Explain a change, Prepare data, Train a model, Forecast, Monitor. Show applicable actions by configuration. Selected but unavailable actions explain the missing requirement and cannot run.
- Work: a common job shell with purpose, inputs, steps, results, approval and history. Specialist panels appear only within their job: recipe graph, experiment comparison, or model card.
- Builder: begin with a template and a step list; the canvas is an optional view of the same definition. Agent configuration is a form. Advanced prompt/code settings are collapsed.
- Outputs: shared list with type filters. Metrics have one editing home in Data; models and datasets retain links to their producing work.

Agent authoring fields: purpose, allowed capabilities/tools, knowledge scope, input/output schema, model purpose, budget, test cases and version. Save a draft, test in bounded conditions, inspect results/diff, publish. No free-form agent text can override permissions or approval requirements.

## 4. Delivery order and acceptance

1. **Workspace configuration + common shell (P7-18).** Reuse enablement; add reversible presets and readiness. Verify Analysis-only, Engineering-only, ML-only and combined configurations; direct API calls obey the same gates; viewer cannot reach admin actions. Keep the screen budget. Document unavailable job kinds honestly.
2. **Engineering vertical slice (P6-01..03, existing P6-04..07).** Pick an authorized source, apply a recipe, preview, run quality checks, approve and materialize. Verify preview has no writes; retry creates no duplicates; incremental output matches a full rebuild; crash cannot advance the cursor without data; failed output preserves last good; lineage and destination catalog update.
3. **Agent/workflow authoring (P7-18 authoring scope; P6-08 engineering agent).** Forms emit validated manifests/definitions; compose existing capabilities. Verify missing dependencies and cycles are rejected, independent steps obey concurrency budgets, failed prerequisites block consumers, approvals resume after restart, and schedules retain published versions. Add new control-flow primitives only with explicit runtime tests.
4. **ML vertical slice (P5-01..06 and P7-06).** Dataset selection through split, baseline, bounded training, evaluation, model card and batch scoring. Verify leakage rejection, identical evaluation splits, reproducible seed, resource limits and lineage. Existing forecast/driver skills do not establish completion of the ML lifecycle.
5. **End-to-end usability and recovery.** An analyst gets an explainable answer; an engineer recovers a failed load; an ML practitioner compares a model with its baseline. Run browser journeys on real APIs and representative data. Record completion, failures, latency and cost before claiming readiness or development-time savings.

The tracker owns these existing rows. This plan introduces no duplicate task queue and promotes no row to Done. The scope of agent/workflow authoring is explicit in P7-18; runtime additions must be identified from contract gaps before implementation.
