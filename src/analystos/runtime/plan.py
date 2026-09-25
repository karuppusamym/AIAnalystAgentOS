"""The analyst lifecycle as a versioned, hashed plan (§19, §40, §61).

The step skeleton is deterministic (it *is* the method); the supervisor LLM tailors content
(analytical questions, focus, audience) inside it. Dynamic tasks (one per hypothesis, follow-up
rounds) are appended at runtime by the investigator. Dependencies ending in ':*' match every task
with that prefix, so 'insights' waits for every test and follow-up round, including ones added later.
"""
from __future__ import annotations

from typing import Any

from analystos.core.ids import stable_hash

# key, agent, title, depends_on, optional
BASE_STEPS: list[tuple[str, str, str, list[str], bool]] = [
    ("context", "context", "Load business context and resolve terminology", [], False),
    ("metadata", "metadata", "Discover technical metadata of selected tables", [], False),
    ("relationships", "metadata", "Discover and validate relationships", ["metadata"], True),
    ("profile", "profiler", "Profile selected tables", ["metadata"], False),
    ("quality", "data_quality", "Detect data-quality issues", ["profile", "relationships"], True),
    ("hypotheses", "investigator", "Generate analytical questions and prioritised hypotheses", ["context", "profile", "quality"], False),
    ("insights", "insight", "Turn supported results into evidence-backed findings", ["hypotheses", "test:*", "followups:*"], False),
    ("verify", "critic", "REV verification of every finding", ["insights"], False),
    ("dataset", "sql", "Build the reusable analytical dataset", ["verify"], False),
    ("semantic", "semantic", "Define and validate KPIs", ["dataset"], False),
    ("visualize", "visualization", "Design charts and executive + operational dashboards", ["semantic"], False),
    ("publish_request", "publisher", "Governance review and publication approval request", ["visualize"], False),
    ("publish", "publisher", "Publish approved bundle to the BI destination", ["publish_request"], True),
    ("finalize", "supervisor", "Consolidate results, write episode memory and lineage graph", ["publish"], False),
]

REPLAN_RESET = {"hypotheses", "insights", "verify", "dataset", "semantic", "visualize", "publish_request", "publish", "finalize"}
DYNAMIC_PREFIXES = ("test:", "followups:")


def base_plan(objective: str, *, autonomy_level: int, questions: list[str] | None = None,
              audience: list[str] | None = None, focus: list[str] | None = None) -> dict[str, Any]:
    steps = []
    if autonomy_level <= 2:
        steps.append({"key": "plan_approval", "agent": "supervisor", "title": "Wait for plan approval", "depends_on": [], "optional": False})
    for key, agent, title, deps, optional in BASE_STEPS:
        d = list(deps)
        if autonomy_level <= 2 and not d:
            d = ["plan_approval"]
        steps.append({"key": key, "agent": agent, "title": title, "depends_on": d, "optional": optional})
    return {"objective": objective, "questions": questions or [], "audience": audience or ["executive", "operational"],
            "focus": focus or [], "steps": steps}


def plan_hash(plan: dict[str, Any], *, constraints: dict[str, Any], scope_hash: str, plan_version: int) -> str:
    """What an approval binds to: plan content + user constraints + authorized scope + version."""
    return stable_hash({"plan": plan, "constraints": constraints, "scope": scope_hash, "version": plan_version})


def dep_satisfied(dep: str, tasks: dict[str, Any], waiting_key: str | None = None) -> bool:
    """True when the dependency is done. Pattern deps are done when every matching task is terminal;
    matching tasks that themselves wait on `waiting_key` are ignored (no self-cycles)."""
    ok_states = {"COMPLETED", "SKIPPED"}
    if dep.endswith(":*"):
        prefix = dep[:-1]
        return all(t.status in ok_states | {"FAILED", "CANCELLED"} for k, t in tasks.items()
                   if k.startswith(prefix) and k != waiting_key and waiting_key not in (getattr(t, "depends_on", None) or []))
    task = tasks.get(dep)
    if task is None:
        return False
    return task.status in ok_states or (task.status == "FAILED" and task.input.get("optional"))
