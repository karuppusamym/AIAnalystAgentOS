"""Kind-specific validation at load (spec v3 §3.2): a reference to something that does not exist
fails the load instead of drifting silently. Returns problems; the registry decides whether to raise."""
from __future__ import annotations

import fnmatch
from typing import Any

from pydantic import ValidationError

from analystos.contracts.capability import CapabilityManifest

EXECUTABLE_KINDS = ("Skill", "Tool", "Method")


def _known_tools() -> set[str]:
    from analystos.tools.registry import TOOLS

    return set(TOOLS)


def _purposes() -> set[str]:
    from analystos.llm.config import load_models_config

    return set(load_models_config().routing)


def _errors(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'spec'}: {e['msg']}" for e in exc.errors()[:5])


def _matches(ref: str, manifests: dict[str, CapabilityManifest]) -> bool:
    return any(fnmatch.fnmatchcase(i, ref) for i in manifests) if any(c in ref for c in "*?[") else ref in manifests


def validate_kinds(manifests: dict[str, CapabilityManifest], *, references: bool = True) -> list[str]:
    from analystos.artifacts.registry import ARTIFACT_TYPES
    from analystos.capabilities.agents import GENERIC_ENTRY, AgentBody
    from analystos.capabilities.playbook import PlaybookBody

    problems: list[str] = []
    tools, purposes = _known_tools(), _purposes()
    for m in manifests.values():
        where = f"{m.source}: {m.id}"
        if m.kind == "Agent":
            try:
                body = AgentBody.model_validate(m.spec)
            except ValidationError as exc:
                problems.append(f"{where}: {_errors(exc)}")
                continue
            if m.entry is not None and not (m.entry.startswith("python:") or m.entry == GENERIC_ENTRY):
                problems.append(f"{where}: an agent entry must be python:<module>:<attr> or {GENERIC_ENTRY}")
            for ref in body.capabilities if references else ():
                if not _matches(ref, manifests):
                    problems.append(f"{where}: unknown capability {ref}")
            problems += [f"{where}: unknown tool {t}" for t in body.tools if t not in tools]
            problems += [f"{where}: unknown model purpose {p} (config/models.yaml routing)" for p in body.purposes if p not in purposes]
            for a in body.default_actions:
                if not any(fnmatch.fnmatchcase(a.capability, ref) for ref in body.capabilities):
                    problems.append(f"{where}: default action {a.capability} is not one of the agent's capabilities")
            if body.output and body.output.artifact_type not in ARTIFACT_TYPES:
                problems.append(f"{where}: unknown artifact type {body.output.artifact_type}")
        elif m.kind == "Playbook":
            try:
                body_pb = PlaybookBody.model_validate(m.spec)
            except ValidationError as exc:
                problems.append(f"{where}: {_errors(exc)}")
                continue
            uses = [(s.key, s.use, s.behaviour) for s in body_pb.steps] + \
                   [(f"{e.prefix}*", e.use, e.behaviour) for s in body_pb.steps for e in s.expands]
            for key, use, behaviour in uses if references else ():
                target = manifests.get(use or "")
                if target is None or target.kind != "Agent":
                    problems.append(f"{where}: step {key} uses {use}, which is not a registered agent")
                    continue
                if behaviour is None and target.entry is None:
                    problems.append(f"{where}: step {key}: agent {use} has no default entry")
                if behaviour is not None and behaviour not in (target.spec.get("behaviours") or {}):
                    problems.append(f"{where}: step {key}: agent {use} has no behaviour {behaviour}")
        elif m.kind in EXECUTABLE_KINDS and isinstance(m.spec, dict) and m.spec.get("call") == "context":
            tool = m.spec.get("tool")
            if tool is not None and tool not in tools:
                problems.append(f"{where}: unknown tool {tool}")
            if not (m.entry or "").startswith("python:"):
                problems.append(f"{where}: a context-call capability needs a python entry")
            problems += _schema_problems(where, m.input_schema)
    return problems


def _schema_problems(where: str, schema: dict[str, Any]) -> list[str]:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    try:
        Draft202012Validator.check_schema(schema or {"type": "object"})
    except SchemaError as exc:
        return [f"{where}: input_schema is not a valid JSON Schema: {exc.message}"]
    return []
