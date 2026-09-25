"""Capability manifest (spec v3 §3.1, ADR-0011): one contract for every pluggable thing.

Agents, skills, tools, analysis methods, connectors, engines, publishers, decision purposes,
detectors, crawlers, knowledge packs, playbooks and renderers are all described by a manifest.
Governance reads the manifest, never the implementation: side effect, cost class, determinism,
permissions and certification decide what may run where. Kind-specific fields live in `spec`.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

CapabilityKind = Literal["Agent", "Skill", "Tool", "Method", "Connector", "Engine", "Publisher", "DecisionPurpose",
                         "Detector", "Crawler", "KnowledgePack", "Playbook", "Renderer"]
Determinism = Literal["deterministic", "seeded", "model"]
SideEffect = Literal["none", "read_source", "write_internal", "write_external"]
CostClass = Literal["free", "query", "compute", "llm_small", "llm_large"]
CertStatus = Literal["draft", "tested", "certified", "deprecated"]

# Order used to compare and to default: anything unknown is treated as the most dangerous.
SIDE_EFFECT_ORDER: dict[str, int] = {"none": 0, "read_source": 1, "write_internal": 2, "write_external": 3}
KIND_PREFIX: dict[str, str] = {"Agent": "agent", "Skill": "skill", "Tool": "tool", "Method": "method",
                               "Connector": "connector", "Engine": "engine", "Publisher": "publisher",
                               "DecisionPurpose": "decision", "Detector": "detector", "Crawler": "crawler",
                               "KnowledgePack": "pack", "Playbook": "playbook", "Renderer": "renderer"}
ENTRY_SCHEMES = ("python:", "mcp://", "http:", "sql-template:", "skill-md:", "builtin:")
_ID = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_*-]+)+$")
_SEMVER = re.compile(r"^\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]+)?$")


class Certification(BaseModel):
    status: CertStatus = "draft"
    evidence: str | None = None  # test path or dated evidence file that justifies the status


class UiHints(BaseModel):
    form: Literal["auto", "none", "custom"] = "auto"  # auto: a JSON-Schema form is generated
    renderer: str | None = None  # renderer capability id for the output


class CapabilityManifest(BaseModel):
    """What a capability is and what it may do. Validated at load; unknown references fail the load."""

    apiVersion: Literal["analystos/v1"] = "analystos/v1"
    kind: CapabilityKind
    id: str
    version: str = "1.0.0"
    summary: str
    entry: str | None = None  # python:module:attr | mcp://server/tool | http:<tool-id> | sql-template:<file> | skill-md:<path> | builtin:<name>
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    determinism: Determinism = "deterministic"
    # Unclassified (e.g. an imported MCP tool) defaults to the most dangerous class: it needs approval.
    side_effect: SideEffect = "write_external"
    cost_class: CostClass = "free"
    permissions: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)  # capability ids (or "engine:<feature>") it needs
    certification: Certification = Field(default_factory=Certification)
    ui: UiHints = Field(default_factory=UiHints)
    tags: list[str] = Field(default_factory=list)
    spec: dict[str, Any] = Field(default_factory=dict)  # kind-specific body (agent role/goal/budget, playbook steps, ...)
    source: str = "builtin"  # builtin | pack:<name> | entrypoint:<dist> | mcp:<server> — set by the loader

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID.match(v):
            raise ValueError(f"capability id {v!r} must look like '<kind>.<name>' in lower snake case")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        if not _SEMVER.match(v):
            raise ValueError(f"version {v!r} is not a semantic version")
        return v

    @field_validator("entry")
    @classmethod
    def _entry(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(ENTRY_SCHEMES):
            raise ValueError(f"entry {v!r} must start with one of {', '.join(ENTRY_SCHEMES)}")
        return v

    @model_validator(mode="after")
    def _prefix(self) -> CapabilityManifest:
        prefix = KIND_PREFIX[self.kind]
        if not self.id.startswith(prefix + "."):
            raise ValueError(f"a {self.kind} id must start with '{prefix}.' (got {self.id!r})")
        return self

    @property
    def ref(self) -> str:
        """id@version — what a plan binds and an approval covers."""
        return f"{self.id}@{self.version}"

    @property
    def autonomous_ok(self) -> bool:
        """Only certified capabilities run under autonomy >= 3 or on a schedule (spec v3 §3.1)."""
        return self.certification.status == "certified"

    @property
    def needs_approval(self) -> bool:
        return self.side_effect == "write_external"
