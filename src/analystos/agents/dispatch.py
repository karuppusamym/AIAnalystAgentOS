"""Resolves a task to the behaviour that executes it, through the run's bound playbook and agent
manifests (P4-X02/X03): step -> `use` agent -> `behaviour` (or the agent's entry) -> python callable,
or the generic propose -> validate -> execute runtime for a declarative agent. There is no key map
and no fallback: a task the playbook does not declare is an error."""
from __future__ import annotations

from analystos.capabilities import registry
from analystos.capabilities.agents import GENERIC_ENTRY, entry_for
from analystos.capabilities.binding import resolve, run_playbook
from analystos.core.errors import NotFound
from analystos.runtime.context import RunContext


def dispatch(ctx: RunContext) -> dict:
    key = ctx.task.key
    playbook = run_playbook(ctx.run)
    step = playbook.step(key)
    if step is not None:
        use, behaviour, side_effect = step.use, step.behaviour, step.type == "side_effect"
    else:
        expansion = playbook.expansion(key)
        if expansion is None:
            raise NotFound(f"task {key} is not a step of {playbook.ref}")
        use, behaviour, side_effect = expansion.use, expansion.behaviour, False
    manifest = ctx.manifest if ctx.manifest is not None and ctx.manifest.id == use else resolve(ctx.run, use)
    entry = entry_for(manifest, behaviour)
    if side_effect:
        ctx.check_control()  # a cancel that arrived while waiting for approval stops the side effect
    if entry == GENERIC_ENTRY:
        from analystos.agents.generic import run_agent

        return run_agent(ctx, manifest)
    if entry is None:
        raise NotFound(f"{manifest.ref} has no behaviour to run for task {key}")
    return registry.resolve_python(entry)(ctx)
