# Product intent — why AnalystOS exists

**One sentence.** A business user selects governed enterprise data, states a business problem, and
receives a validated, explainable, reproducible analysis — findings, KPIs, dashboards and lineage —
without coordinating a team of specialists, and without the system ever acting beyond what it is
allowed to do.

## The job to be done

| Who | Today | With AnalystOS |
|---|---|---|
| Business user | Files a ticket, waits weeks for a dashboard that answers last month's question | States the objective; watches the investigation; approves what gets published |
| Data analyst | Spends most time profiling, joining, re-running and formatting | Reviews hypotheses and evidence, redirects, edits KPIs; the agents do the grind |
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

Replacing ETL platforms, warehouses, MDM or IAM; writing to source systems; unattended
publication; universal BI rendering; training models.
