"""Executable definitions and their versions (ADR-0021, spec v4 §8, P7-03).

One generic shape for everything a trigger can run: a `kind` (playbook, saved analysis, recipe,
ML spec, query tool, ...) plus a JSON `spec`. Editing works on a *draft*; **Publish** freezes an
immutable version identified by `(key, version, content_hash)`; **Retire** stops it running.
Built-in and pack YAML is published by definition: its version is the manifest's `version` and its
hash the manifest's content digest, so a run can name either with the same `DefinitionRef`.

Kinds register a validator (`services/definitions.register_kind`), so later rows (recipes P6-04,
ML specs P5, query tools P7-11) add a kind without a schema change.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DefinitionStatus = Literal["draft", "published", "deprecated", "retired"]
PinState = Literal["current", "upgrade_available", "deprecated", "blocked", "unpinned"]
PinItemState = Literal["current", "newer", "deprecated", "retired", "rejected"]
_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,119}$")
_KIND = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

RUNNABLE_STATUSES = ("published", "deprecated")  # deprecated keeps running, with a warning
BLOCKING_ITEM_STATES = ("retired", "rejected")


class DefinitionRef(BaseModel):
    """What a trigger names and a run records. `version` None means the newest published version."""

    model_config = ConfigDict(extra="forbid")
    kind: str = "playbook"
    key: str = ""  # empty only when `id` names the version row
    version: int | str | None = None  # int for workspace definitions; the manifest semver for built-in YAML
    content_hash: str | None = None
    source: Literal["workspace", "builtin"] = "workspace"
    status: DefinitionStatus | None = None
    id: str | None = None  # the version row, for workspace definitions

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.key}@{self.version}"


class DefinitionDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    key: str
    title: str | None = Field(default=None, max_length=300)
    spec: dict[str, Any]

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if not _KIND.match(v):
            raise ValueError("kind must be lower_snake_case")
        return v

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        if not _KEY.match(v):
            raise ValueError("key must start with a letter: lowercase letters, digits, '.', '_' or '-'")
        return v


class DefinitionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=300)
    spec: dict[str, Any] | None = None


class PinItem(BaseModel):
    """One pinned dependency of a schedule and how it compares with what is current now."""

    type: Literal["definition", "capability", "method", "metric", "semantic_model"]
    id: str
    pinned: str | None
    current: str | None = None
    state: PinItemState = "current"
    reason: str | None = None
    diff: list[dict[str, Any]] = Field(default_factory=list)


class PinStatus(BaseModel):
    """Upgrade-available / deprecated / blocked, computed in code from the pins and the registries."""

    state: PinState
    revision: int = 0
    items: list[PinItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=list)
    upgrade_hash: str | None = None  # binds an accept to the upgrade the owner saw
    checked_at: str | None = None
