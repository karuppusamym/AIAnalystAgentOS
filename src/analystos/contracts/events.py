"""Event model (§42, FND-010)."""
from __future__ import annotations

EVENT_TYPES = {
    "workspace.created", "workspace.updated", "source.connected", "metadata.collected", "context.loaded",
    "profile.completed", "quality.issue.detected", "relationship.discovered", "hypothesis.created",
    "hypothesis.updated", "query.executed", "query.rejected", "analysis.completed", "insight.created",
    "insight.verified", "dataset.created", "metric.created", "chart.created", "dashboard.created",
    "dashboard.published", "report.generated", "schedule.executed", "agent.started", "agent.completed",
    "agent.failed", "agent.message", "task.updated", "run.created", "run.status", "run.replanned",
    "approval.requested", "approval.completed", "approval.invalidated", "policy.denied", "feedback.received",
    "model.called", "budget.warning", "budget.cap_reached", "monitor.evaluated", "alert.raised", "notification.created",
    "crawl.started", "crawl.completed", "crawl.failed", "schema.changed", "settings.updated",
    "mcp.server.refreshed", "mcp.tool_called", "agent.action", "verified_query.promoted", "verified_query.updated",
    "hypothesis_registry.updated",
    # Write path (P4-E04/E06): build targets and dbt build jobs
    "build_target.provisioned", "build.planned", "build.started", "build.completed", "build.failed", "build.refused",
    "semantic.model.updated", "semantic.metric.proposed", "semantic.metric.approved", "semantic.metric.rejected",
    "semantic.metric.deprecated", "semantic.conflict.detected", "semantic.imported",
    "ask.answered", "ask.promoted", "capability.invoked",
    "knowledge.revision_committed", "knowledge.imported", "knowledge.pushed",
    # Review queue (P4-K07/K08): drafts proposed by a model or the learning loop, decided in batches
    "knowledge.suggestion_proposed", "knowledge.suggestions_reviewed",
    # Crawler sources and facet-level failure (P4-K06)
    "crawl.facet_failed", "knowledge.ingested",
    # Typed evidence (P4-03): a newer snapshot of an analysed asset made a finding stale
    "insight.stale",
    # Verification records (P7-01, ADR-0020): a verdict bound to its dependency fingerprint; voided when a
    # dependency changes (with its cause), swept nightly; a finding flagged wrong carries its reason
    "verification.recorded", "verification.voided", "verification.sweep_completed", "insight.flagged_wrong",
    # Definition lifecycle and pinned schedules (P7-03, ADR-0021)
    "definition.draft_saved", "definition.published", "definition.deprecated", "definition.retired",
    "schedule.upgrade_available", "schedule.upgraded", "schedule.pin_warning", "schedule.blocked", "narrative.stale",
    # Typed work orders and the dispatch outbox (P4-06)
    "work_order.created", "work_order.updated", "run.dispatched", "run.dispatch_failed",
    # Transformation recipes (P6-04..07) and file ingestion (P6-06)
    "recipe.saved", "recipe.published", "recipe.run.started", "recipe.run.completed", "recipe.run.blocked",
    "recipe.run.refused", "recipe.run.failed", "file.ingested",
}

RUN_TERMINAL = {"COMPLETED", "FAILED", "REJECTED", "CANCELLED"}
RUN_STATUSES = ["NEW", "PLANNING", "READY", "RUNNING", "WAITING_TOOL", "WAITING_USER", "PAUSED",
                "EVALUATING", "VERIFYING", *sorted(RUN_TERMINAL)]
TASK_STATUSES = ["NEW", "READY", "RUNNING", "WAITING_USER", "COMPLETED", "FAILED", "SKIPPED", "CANCELLED", "INVALIDATED"]
