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
    "model.called", "budget.warning", "monitor.evaluated", "alert.raised", "notification.created",
    "crawl.started", "crawl.completed", "crawl.failed", "schema.changed", "settings.updated",
    "mcp.server.refreshed", "mcp.tool_called", "agent.action",
}

RUN_TERMINAL = {"COMPLETED", "FAILED", "REJECTED", "CANCELLED"}
RUN_STATUSES = ["NEW", "PLANNING", "READY", "RUNNING", "WAITING_TOOL", "WAITING_USER", "PAUSED",
                "EVALUATING", "VERIFYING", *sorted(RUN_TERMINAL)]
TASK_STATUSES = ["NEW", "READY", "RUNNING", "WAITING_USER", "COMPLETED", "FAILED", "SKIPPED", "CANCELLED", "INVALIDATED"]
