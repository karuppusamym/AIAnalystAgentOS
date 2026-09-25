"""The analyst lifecycle as a versioned, hashed plan (§19, §40, §61).

The step skeleton comes from a playbook (capabilities/playbook.py, P4-X02) and is deterministic (it
*is* the method); the supervisor LLM tailors content (analytical questions, focus, audience) inside
it. Dynamic tasks (the playbook's `expands`) are appended at runtime by the steps that declare them.
Dependencies ending in ':*' match every task with that prefix, so 'insights' waits for every test and
follow-up round, including ones added later.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analystos.core.ids import stable_hash

if TYPE_CHECKING:
    from analystos.capabilities.playbook import Playbook


def base_plan(objective: str, *, autonomy_level: int, questions: list[str] | None = None,
              audience: list[str] | None = None, focus: list[str] | None = None, playbook: Playbook | None = None) -> dict[str, Any]:
    """The plan of a playbook (default: `playbook.investigate` from the registry) for these inputs."""
    if playbook is None:
        from analystos.capabilities import registry
        from analystos.capabilities.playbook import DEFAULT_PLAYBOOK, parse

        playbook = parse(registry.current().get(DEFAULT_PLAYBOOK))
    return playbook.build_plan(objective, autonomy_level=autonomy_level, questions=questions, audience=audience, focus=focus)


def plan_hash(plan: dict[str, Any], *, constraints: dict[str, Any], scope_hash: str, plan_version: int,
              capabilities: list[str] | None = None) -> str:
    """What an approval binds to: plan content + user constraints + authorized scope + version, and the
    `id@version` of every capability the plan binds (spec v3 §3.1), so an approval covers exact versions.
    Without bindings the value is the v1 hash, which `investigate.v1` reproduces for the same inputs."""
    body: dict[str, Any] = {"plan": plan, "constraints": constraints, "scope": scope_hash, "version": plan_version}
    if capabilities:
        body["capabilities"] = sorted(capabilities)
    return stable_hash(body)


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
