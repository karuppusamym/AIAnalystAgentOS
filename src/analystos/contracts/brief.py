"""Workspace brief, readiness assessment and job kinds (workspace spec §2, P4-04).

The brief is a versioned set of *assertions*. Each carries its origin (`source`: read from source
metadata or an approved definition; `rule`: inferred by deterministic code; `model`: proposed by a
model; `user`: stated by a person), evidence references, a review state and its own version. An
inferred grain, key or business term stays a `suggested` assertion — never used as fact — until a
person reviews it or a deterministic check validates it. The brief never carries permissions: scope
comes from policy and is only ever narrowed by `constraints`.

A `ReadinessAssessment` answers `ready | needs_input | blocked | unsupported` for one job kind over its
inputs, with every check's own status, reason and remediation. There is no averaged score: one failed
required check decides the status.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BRIEF_SCHEMA_VERSION = 1
Origin = Literal["source", "rule", "model", "user"]
ReviewState = Literal["suggested", "reviewed", "validated", "rejected"]
BriefGroup = Literal["decision", "domain", "data_semantics", "time_measures", "ml_objective", "constraints", "knowledge"]
EFFECTIVE_STATES = ("reviewed", "validated")

# The fields each group may hold (workspace spec §2). Anything else is refused: a brief cannot grow a
# field that some check would then have to guess the meaning of.
GROUP_FIELDS: dict[str, tuple[str, ...]] = {
    "decision": ("question", "owner", "intended_action", "audience", "acceptance_rubric"),
    "domain": ("term", "entity", "alias", "prohibited_interpretation"),
    "data_semantics": ("grain", "entity_key", "join_cardinality", "dedup_rule", "exclusion_rule"),
    "time_measures": ("event_time", "availability_time", "timezone", "fiscal_calendar", "unit", "currency", "numerator",
                      "denominator", "aggregation"),
    "ml_objective": ("target", "prediction_moment", "horizon", "label_availability", "error_costs", "evaluation_metric"),
    "constraints": ("source_scope", "destination_scope", "freshness_tolerance_hours", "residency", "retention",
                    "compute_budget", "query_budget"),
    "knowledge": ("context_ids", "metric_versions", "accepted_corrections"),
}
# Inferred assertions of these fields start as suggestions whatever their origin (except `user`).
SUGGESTION_FIELDS = frozenset({"grain", "entity_key", "join_cardinality", "term", "entity", "alias", "unit", "currency",
                               "event_time", "timezone", "target"})


class EvidenceRef(BaseModel):
    """What an assertion rests on: an asset, a column profile, a relationship, a context entry, a check."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["asset", "column", "profile", "relationship", "context", "metric", "query", "check", "user_note"]
    ref: str
    detail: dict[str, Any] | None = None


class Assertion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str  # "<group>.<field>" or "<group>.<field>:<subject>", stable across versions
    group: BriefGroup
    field: str
    subject: str | None = None  # an asset (schema.table), column (schema.table.column) or relationship
    value: Any
    origin: Origin
    evidence: list[EvidenceRef] = Field(default_factory=list)
    review_state: ReviewState = "suggested"
    version: int = 1
    confidence: float | None = Field(default=None, ge=0, le=1)
    note: str | None = Field(default=None, max_length=2000)
    updated_by: str | None = None
    updated_at: str | None = None

    @model_validator(mode="after")
    def _field_in_group(self) -> Assertion:
        if self.field not in GROUP_FIELDS[self.group]:
            raise ValueError(f"{self.group} has no field {self.field!r}; one of {', '.join(GROUP_FIELDS[self.group])}")
        return self

    @property
    def effective(self) -> bool:
        """Only reviewed or validated assertions are used as facts by readiness and planning."""
        return self.review_state in EFFECTIVE_STATES


def assertion_key(group: str, field: str, subject: str | None = None) -> str:
    return f"{group}.{field}" + (f":{subject}" if subject else "")


class WorkspaceBriefDoc(BaseModel):
    """One version of a workspace's brief (the API and contract view)."""

    schema_version: Literal[1] = BRIEF_SCHEMA_VERSION
    workspace_id: str
    version: int
    content_hash: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    reason: str | None = None
    assertions: list[Assertion] = Field(default_factory=list)


class AssertionIn(BaseModel):
    """A person states an assertion (origin `user`, reviewed by them)."""

    model_config = ConfigDict(extra="forbid")
    group: BriefGroup
    field: str
    subject: str | None = Field(default=None, max_length=400)
    value: Any
    evidence: list[EvidenceRef] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=2000)


class BriefOp(BaseModel):
    """One change in a brief PATCH. `set` states a user assertion; `review` accepts a suggestion as it is;
    `reject` keeps it visible as rejected; `remove` drops an assertion."""

    model_config = ConfigDict(extra="forbid")
    op: Literal["set", "review", "reject", "remove"]
    key: str | None = None  # review / reject / remove
    assertion: AssertionIn | None = None  # set
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _shape(self) -> BriefOp:
        if self.op == "set" and self.assertion is None:
            raise ValueError("a set operation needs an assertion")
        if self.op != "set" and not self.key:
            raise ValueError(f"a {self.op} operation needs the assertion key")
        return self


class BriefPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ops: list[BriefOp] = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=2000)


# ------------------------------------------------------------------------------------ readiness
ReadinessStatus = Literal["ready", "needs_input", "blocked", "unsupported"]
CheckStatus = Literal["pass", "fail", "needs_input", "warn", "not_applicable", "unsupported"]
CHECKS = ("capability", "scope", "freshness", "schema_drift", "grain", "key_uniqueness", "join_fanout", "coverage",
          "missingness", "label_availability")


class ReadinessCheck(BaseModel):
    check: str
    status: CheckStatus
    required: bool = True  # an advisory check reports `warn`/`fail` but never changes the verdict
    reason: str
    remediation: str | None = None
    subject: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)


class ReadinessIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_kind: str
    assets: list[str] = Field(default_factory=list, max_length=50)  # schema.table; default: the brief's / scope's
    target: str | None = None  # predict: the label column (schema.table.column or column)
    time_column: str | None = None
    horizon: int | None = Field(default=None, ge=1)
    measures: list[str] = Field(default_factory=list, max_length=50)


class ReadinessAssessmentDoc(BaseModel):
    id: str | None = None
    workspace_id: str
    job_kind: str
    status: ReadinessStatus
    brief_version: int
    work_order_id: str | None = None
    work_order_revision: int | None = None
    checks: list[ReadinessCheck]
    inputs: dict[str, Any] = Field(default_factory=dict)
    inputs_hash: str | None = None
    alternatives: list[dict[str, Any]] = Field(default_factory=list)  # never applied: an explicit choice is required
    created_at: str | None = None


class JobKindAvailability(BaseModel):
    """One **Start work** choice and whether it can start here, with every reason it cannot."""

    key: Literal["explain", "compare", "forecast", "predict", "prepare", "monitor"]
    label: str
    mode: Literal["analysis", "engineering", "ml"]
    work_order_kind: str
    available: bool
    reasons: list[dict[str, str]] = Field(default_factory=list)  # {code, message, remediation}
    capabilities: list[dict[str, Any]] = Field(default_factory=list)
    entry: dict[str, Any] = Field(default_factory=dict)  # where the UI starts it (route, payload type)
    readiness_checks: list[str] = Field(default_factory=list)  # the required checks of an assessment
    min_role: str = "analyst"
