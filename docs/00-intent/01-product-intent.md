# Product intent — why AnalystOS exists

**One sentence.** A user brings governed data and a business objective into a workspace, and
receives reproducible analysis, prepared datasets or evaluated ML outputs that reflect that
workspace's definitions, with evidence, operational ownership and control over external actions.

The current implementation proves bounded analytics workflows. The broader data engineering and
ML ambition is specified in the [workspace data-team target](03-workspace-data-team-spec.md),
not claimed as delivered. Success means completing defined specialist tasks with independently
accepted quality and less human effort, elapsed time and total cost; the
[evaluation plan](../60-delivery/05-evaluation-plan.md) defines how to measure it.

## The job to be done

| Who | Today | With AnalystOS |
|---|---|---|
| Business user | Files a ticket, waits weeks for a dashboard that answers last month's question | States the objective; watches the investigation; approves what gets published |
| Data analyst | Spends most time profiling, joining, re-running and formatting | Reviews hypotheses and evidence, redirects, edits KPIs; the agents do the grind |
| Data engineer (target) | Builds and repairs joins, transforms, contracts and refresh jobs | Reviews a tested pipeline, reconciliation, backfill and recovery plan |
| Data scientist / ML practitioner (target) | Rebuilds splits, baselines, experiments and deployment checks | Reviews leakage-safe evaluation, model cards, approved scoring and performance monitoring |
| Data steward | Finds out afterwards what data was used where | Sees every query, column, approval and publication with lineage; PII/restricted columns never leave scope |
| Platform admin | Cannot see or cap LLM spend and behaviour | Per-purpose model routing, budgets, allowlists, full call log |

## What "good" looks like

1. **Trustworthy before clever.** A finding the user cannot reproduce is worse than no finding.
   Every number is computed by deterministic code from a logged query and survives a re-run and
   a second method.
2. **Controllable.** Pause, redirect ("ignore network incidents"), reject a finding, edit a KPI,
   approve or reject publication — at any time, with the plan re-derived from the instruction.
3. **Honest about limits.** When a model, a destination or a permission is unavailable the
   system says so and falls back to a labelled deterministic path; it never pretends.
4. **Cheap by default.** Push computation to the data; small models for prose; a typed decision
   model (JEV) for bounded choices; budgets that stop spend rather than report it afterwards.

## North-star demo

> "Analyze Incident and Change data for the last 12 months. Identify the drivers of SLA breaches,
> recurring operational problems, teams with unusually high reassignment, and applications
> generating repeated critical incidents. Create an executive dashboard and an operations dashboard."

The system should visibly: discover the source, retrieve context, profile, find data-quality
problems, propose and prioritise hypotheses, test them (SQL + statistics), verify findings,
define KPIs, design dashboards, ask for approval, publish to Superset, and show lineage from each
dashboard tile back to the query and table it came from.

## Non-goals (this release)

Replacing warehouses, MDM or IAM; writing to source systems; unattended publication; universal
BI rendering. General model training and managed engineering outputs are outside the current
prototype, but are explicit future increments in the target specification. Online serving,
deep learning and streaming infrastructure remain deferred. Do not promise replacement of every
specialist on arbitrary data before demonstrating the supported tasks and their limits.
