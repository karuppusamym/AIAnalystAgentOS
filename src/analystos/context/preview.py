"""What the agents see (Stream D): the prompt context of one model purpose, built exactly as the call would build
it — the same scope, catalog, compiler, cache lookup and message layout (`agents.common.prompt_layout`) — without
calling any model. Ask's SQL generation goes through `sql_agent.generation_context`, the path Ask itself uses.

Nothing is recorded as a model call, a skip or a cache hit: the preview reads the shared context cache with its
counters off (`context.cache.unrecorded`), so the reuse figures stay about calls that were made."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from analystos.core.errors import InvalidInput, NotFound
from analystos.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class PreviewPurpose:
    label: str
    description: str
    prompt: str
    agent: str


# The purposes whose prompts carry compiled workspace context (catalog and knowledge sections).
PURPOSES: dict[str, PreviewPurpose] = {
    "sql_generation": PreviewPurpose("Writing SQL for Ask", "What the model sees when Ask writes new SQL for a question.",
                                     "sql_generation.v1", "sql"),
    "planning": PreviewPurpose("Framing an investigation", "What the supervisor sees when it turns an objective into questions.",
                               "planning.v1", "supervisor"),
    "hypothesis_generation": PreviewPurpose("Proposing hypotheses", "What the investigator sees when it proposes what to test.",
                                            "hypothesis_generation.v1", "investigator"),
    "follow_up_generation": PreviewPurpose("Suggesting follow-ups", "What the investigator sees when it proposes follow-up tests.",
                                           "follow_up_generation.v1", "investigator"),
    "semantic_modeling": PreviewPurpose("Proposing metric definitions", "What the semantic agent sees when it proposes KPIs.",
                                        "semantic_modeling.v2", "semantic"),
}
DEFAULT_QUESTION = "What changed recently, and why?"


def purposes() -> list[dict[str, str]]:
    return [{"purpose": k, "label": p.label, "description": p.description} for k, p in PURPOSES.items()]


def _required(purpose: str, question: str) -> dict[str, Any]:
    """The mandatory inputs each purpose's caller passes, with the run-only parts empty (no run exists)."""
    if purpose == "planning":
        return {"objective": question, "user_instructions": []}
    if purpose == "follow_up_generation":
        return {"objective": question, "results": [], "constraints": {}}
    if purpose == "semantic_modeling":
        return {"objective": question, "dataset_columns": [], "existing": []}
    return {"objective": question}


def _context(session: Session, user: Any, workspace_id: str, spec: PreviewPurpose, source_id: str | None) -> Any:
    """The same context object Ask uses (`services.ask.AdhocContext`), for this viewer's authorized scope, with the
    agent contract of the purpose's own agent (its knowledge sections and budget apply)."""
    from analystos.governance.policy import get_workspace, load_policy, resolve_scope
    from analystos.services.ask import AdhocContext
    from analystos.tools.registry import get_agent_spec

    scope = resolve_scope(session, user, workspace_id, source_ids=[source_id] if source_id else None, minimum_role="viewer")
    ws = get_workspace(session, workspace_id)
    try:
        agent = get_agent_spec(session, spec.agent)
    except NotFound:  # a disabled agent: the purpose profile as configured
        agent = None
    return AdhocContext(user=user, workspace=ws, scope=scope, policy=load_policy(session, ws), agent=agent, services=None)


def _section(name: str, value: Any, part: str) -> dict[str, Any]:
    from analystos.agents.common import compact_json
    from analystos.context.compiler import NO_MATCH

    no_match = value == NO_MATCH
    items = 0 if no_match or value in (None, "", [], {}) else (len(value) if isinstance(value, list | dict) else 1)
    return {"name": name, "part": part, "items": items, "chars": len(compact_json(value)), "no_match": no_match}


def build(session: Session, user: Any, workspace_id: str, *, purpose: str, question: str | None = None,
          source_id: str | None = None) -> dict[str, Any]:
    from analystos.agents import common
    from analystos.agents.prompts import prompt
    from analystos.context import cache as context_cache
    from analystos.llm.cache import estimate_tokens
    from analystos.services.platform_settings import get as platform

    spec = PURPOSES.get(purpose)
    settings = platform()
    if spec is None or purpose not in settings.context.profiles:
        raise InvalidInput(f"purpose must be one of {', '.join(p for p in PURPOSES if p in settings.context.profiles)}")
    text = " ".join((question or "").split())[:4000] or DEFAULT_QUESTION
    ctx = _context(session, user, workspace_id, spec, source_id)
    prompt_vars: dict[str, str] = {}
    with context_cache.unrecorded():
        if purpose == "sql_generation":
            from analystos.agents.sql_agent import ask_dialect, generation_context
            from analystos.llm.redaction import redact_question

            rq = redact_question(text)
            key = common.knowledge_retrieval_key(ctx, purpose, objective=rq.text, reference_text=rq.text)
            hit = context_cache.peek(key)
            compiled, _, _ = generation_context(ctx, rq.text, redacted=bool(rq.redacted))
            prompt_vars = {"dialect": ask_dialect(ctx)}
        else:
            key = common.knowledge_retrieval_key(ctx, purpose, objective=text)
            hit = context_cache.peek(key)
            compiled = common.compile_for(ctx, purpose, _required(purpose, text))
    system = prompt(spec.prompt, **prompt_vars)
    refused = compiled.refused
    layout = common.prompt_layout(ctx, compiled, system=system, prompt_vars=prompt_vars)
    stable_tokens = estimate_tokens(system) + (estimate_tokens(layout.preamble) if layout.preamble else 0)
    volatile_tokens = estimate_tokens(layout.volatile_text)
    sections = [{"name": "system", "part": "stable", "items": 1, "chars": len(system), "no_match": False}]
    if layout.header:
        sections.append(_section("header", layout.header, "stable"))
    sections += [_section(k, v, "stable") for k, v in layout.stable.items()]
    sections += [_section(k, v, "volatile") for k, v in layout.volatile.items()]
    return {
        "purpose": purpose, "label": spec.label, "question": text,
        "scope": {"assets": list(ctx.scope.assets), "source_ids": list(ctx.scope.source_ids),
                  "denied_columns": len(ctx.scope.denied_columns), "source_id": source_id},
        "system_prompt": spec.prompt, "system_text": system,
        "preamble_text": layout.preamble, "volatile_text": layout.volatile_text,
        "stable_keys": list(layout.stable), "omitted": list(compiled.omitted), "no_match": list(compiled.no_match),
        "refused": refused, "trimmed": layout.trimmed,
        "receipts": len(compiled.receipts),
        "estimated_tokens": {"stable": stable_tokens, "volatile": volatile_tokens, "total": stable_tokens + volatile_tokens},
        "budget_chars": compiled.budget_chars,
        "cache": {"key": key, "kind": "retrieval", "hit": hit, "shared": context_cache.store().shared,
                  "enabled": settings.context.cache_enabled, "ttl_seconds": settings.context.cache_ttl_seconds},
        "knowledge_version": common.knowledge_version_for(ctx),
        "sections": sections,
    }


def as_text(preview: dict[str, Any]) -> str:
    """The preview as one plain-text file: the three messages in the order they are sent."""
    t = preview["estimated_tokens"]
    head = [f"AnalystOS context preview: {preview['label']} ({preview['purpose']})",
            f"Question: {preview['question']}",
            f"Tables in scope: {len(preview['scope']['assets'])}; knowledge version {preview['knowledge_version'] or '-'}",
            f"Estimated tokens: {t['stable']} stable (cached prefix) + {t['volatile']} per call = {t['total']}",
            f"Context cache ({preview['cache']['kind']}): {'hit' if preview['cache']['hit'] else 'miss'}"
            f"{' (shared)' if preview['cache']['shared'] else ' (this process)'}",
            "Sections: " + ", ".join(f"{s['name']} {s['chars']} chars" for s in preview["sections"])]
    if preview.get("refused"):
        head.append(f"Refused: {preview['refused']}")
    return "\n".join([*head, "", "===== 1. SYSTEM (cached) =====", preview["system_text"], "",
                      "===== 2. WORKSPACE PREAMBLE (cached) =====", preview["preamble_text"] or "(none)", "",
                      "===== 3. PER-CALL INPUTS =====", preview["volatile_text"], ""])


__all__ = ["PURPOSES", "as_text", "build", "purposes"]
