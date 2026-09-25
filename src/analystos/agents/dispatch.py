"""Maps a task key to the agent behaviour that executes it."""
from __future__ import annotations

from analystos.runtime.context import RunContext


def dispatch(ctx: RunContext) -> dict:
    key = ctx.task.key
    from analystos.agents import (
        context_agent,
        critic,
        data_scientist,
        insight,
        investigator,
        metadata,
        profiler,
        publisher,
        semantic,
        sql_agent,
        supervisor,
        visualization,
    )

    if key.startswith("test:"):
        return data_scientist.test_hypothesis(ctx)
    if key.startswith("followups:"):
        return investigator.follow_ups(ctx)
    handlers = {
        "plan_approval": supervisor.plan_approved,
        "context": context_agent.load_context,
        "metadata": metadata.collect_metadata,
        "relationships": metadata.discover_relationships,
        "profile": profiler.profile_tables,
        "quality": profiler.check_quality,
        "hypotheses": investigator.generate_hypotheses,
        "insights": insight.build_insights,
        "verify": critic.verify_insights,
        "dataset": sql_agent.build_dataset,
        "semantic": semantic.define_metrics,
        "visualize": visualization.design,
        "publish_request": publisher.request_publication,
        "publish": publisher.publish,
        "finalize": supervisor.finalize,
    }
    return handlers[key](ctx)
