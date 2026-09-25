# Contracts

Source of truth: Pydantic models in `src/analystos/contracts/`. JSON Schemas are generated into
[`/contracts`](../../contracts) by `analystos export-contracts` — regenerate, never hand-edit.

| Contract | Model | Spec |
|---|---|---|
| Agent definition | `registry.AgentSpec` (YAML in `config/agents/`) | v1 §12.1 |
| Skill definition | `registry.SkillSpec` | v1 §14 |
| Tool definition | `registry.ToolSpec` | v1 §15 |
| Workspace policy | `policy.WorkspacePolicyDoc` (versioned) | v1 §45–§46 |
| Authorized scope | `policy.DataScope` | v1 §12.2 |
| Policy decision | `policy.PolicyDecision` (allow/deny/approval_required + reasons) | v1 §46 |
| Analysis spec (executable hypothesis) | `analysis.AnalysisSpec`, `Derivation`, `Filter` | v1 §20, v2 §7 |
| Statistical result | `analysis.StatResult` | v1 §21 |
| Semantic metric | `bi.MetricDef` | v1 §24 |
| Dataset / chart / dashboard | `bi.DatasetDef`, `bi.ChartSpec`, `bi.DashboardSpec` | v1 §32–§34 |
| Publication unit (what an approval binds to) | `bi.PublishBundle` | v1 §39 |
| Events | `events.EVENT_TYPES` | v1 §42 |
| Connector | `connectors.base.Connector` protocol | v1 §16 |
| BI publisher | `publishing.base.BIPublisher` protocol | v1 §32 |
| Model routing | `config/models.yaml` → `llm.config.ModelsConfig` | v1 §27 |
