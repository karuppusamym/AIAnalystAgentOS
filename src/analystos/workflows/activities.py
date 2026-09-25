"""Temporal activities: thin wrappers over the orchestrator-agnostic engine (sync, thread pool)."""
from __future__ import annotations

from temporalio import activity

from analystos.runtime import engine


@activity.defn(name="plan_run")
def plan_run(run_id: str) -> dict:
    return engine.plan_run(run_id)


@activity.defn(name="get_state")
def get_state(run_id: str) -> dict:
    return engine.get_state(run_id)


@activity.defn(name="execute_task")
def execute_task(run_id: str, key: str) -> dict:
    return engine.execute_task(run_id, key)


@activity.defn(name="finish_run")
def finish_run(run_id: str, outcome: str, error: str | None) -> None:
    engine.finish_run(run_id, outcome, error)


ACTIVITIES = [plan_run, get_state, execute_task, finish_run]
