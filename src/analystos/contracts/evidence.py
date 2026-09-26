"""Evidence contracts for findings (P4-03, target spec §4, ADR-0011 "Evidence migration").

One overloaded `verified` badge is replaced by dimensions on a versioned `EvidenceBundle`:

* **Claim** — typed `Fact`s (value, unit, group, baseline, direction, query/result hashes, method) and the
  `binding` of every number in the narrative to one of them. A sentence whose numbers, groups, units or
  directions do not match the facts does not bind, and an unbound finding cannot be verified.
* **Data** — the run's `DataManifest` entries for the analysed asset (snapshot version, rows, content
  fingerprint, sampling, time window) plus the governed query receipts.
* **Method** — effect size, uncertainty, sample sizes, multiple-testing family, selection procedure,
  assumptions and power at the practical threshold where the method can state it.
* **Validation** — `state` (exploratory | replicated | confirmed | inconclusive | invalid |
  insufficient_evidence | legacy) with check-level pass / fail / not_applicable, and the confirmation rule
  that promoted a discovery, if any. `confirmed` cannot be constructed without a passing rule.
* **Limits** — population, stale inputs, confounding, untested slices.

The legacy review score stays, labelled uncalibrated: it is not a probability that the claim is true.
Models never write any of this; `analystos.evidence` computes it from the deterministic path.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BUNDLE_VERSION = "evidence.v1"
MANIFEST_VERSION = "data_manifest.v1"

Unit = Literal["fraction", "percent", "points", "ratio", "count", "hours", "days", "minutes", "seconds", "value",
               "coefficient", "probability"]
FactKind = Literal["level", "comparison", "change", "share", "count", "statistic"]
Direction = Literal["higher", "lower", "increase", "decrease", "equal"]
ValidationState = Literal["exploratory", "replicated", "confirmed", "inconclusive", "invalid",
                          "insufficient_evidence", "legacy"]
Label = Literal["discovery", "confirmation", "legacy"]
CheckOutcome = Literal["pass", "fail", "not_applicable"]
CONFIRMATION_RULES = ("holdout_partition", "fresh_snapshot_replication")


class Fact(BaseModel):
    """One number a finding may quote, bound to what it measures. `value` is in `unit` exactly as the
    statistic produced it (a rate is a `fraction` 0..1; its percent rendering is derived)."""

    model_config = ConfigDict(extra="forbid")
    id: str
    role: str  # the statistic key it came from (top_rate, rate_ratio, pct_change, n, ...)
    kind: FactKind
    metric: str | None = None  # what is measured (outcome label, "records", "p-value")
    value: float
    unit: Unit
    subject: str | None = None  # the group / cohort / period the value belongs to
    dimension: str | None = None  # what `subject` is a value of (the segment's label)
    baseline: str | None = None  # comparison and change facts: the other side
    direction: Direction | None = None
    window: str | None = None  # time window / period the value covers, when stated
    method: str | None = None
    query_ids: list[str] = Field(default_factory=list)
    result_hashes: list[str] = Field(default_factory=list)
    primary: bool = False  # the claim's headline comparison / change (direction checks without a number use it)


class Mention(BaseModel):
    """A number in the narrative and the fact it was bound to (None: unbound)."""

    model_config = ConfigDict(extra="forbid")
    text: str
    value: float
    unit: str | None = None
    fact_id: str | None = None
    problem: str | None = None


class Binding(BaseModel):
    ok: bool
    mentions: list[Mention] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)


class ManifestEntry(BaseModel):
    """What one analysed asset was when the run read it."""

    model_config = ConfigDict(extra="forbid")
    asset: str  # schema.table as the gateway sees it
    source_id: str | None = None
    mode: Literal["staged", "pushdown", "unknown"] = "unknown"
    load_id: str | None = None
    rows: int | None = None
    source_total_rows: int | None = None
    content_fingerprint: str | None = None  # order-independent hash of the staged rows
    structure_fingerprint: str | None = None  # the crawler's column-structure fingerprint
    sampling_method: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    staged_at: str | None = None
    observed_at: str | None = None  # pushdown: when the run read the live source (best-effort replay only)
    immutable: bool = False  # True when the version names a fixed staged snapshot
    version: str | None = None  # sha256 of the fields that define the data (None when there is no fixed version)
    version_basis: Literal["content", "load_metadata", "none"] = "none"


class DataManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = MANIFEST_VERSION
    version: str  # sha256 over the entries' versions: identical data -> identical manifest version
    built_at: str
    entries: list[ManifestEntry] = Field(default_factory=list)

    def entry(self, asset: str | None) -> ManifestEntry | None:
        return next((e for e in self.entries if e.asset == asset), None)


class Check(BaseModel):
    model_config = ConfigDict(extra="allow")
    check: str
    outcome: CheckOutcome
    reason: str = ""


class Confirmation(BaseModel):
    """Result of evaluating the confirmation rules for one finding."""

    model_config = ConfigDict(extra="forbid")
    rule: str | None = None  # the rule that passed (one of CONFIRMATION_RULES)
    passed: bool = False
    evaluated: list[dict[str, Any]] = Field(default_factory=list)  # every rule tried, with its reason


class Validation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: ValidationState
    label: Label
    checks: list[Check] = Field(default_factory=list)
    confirmation: Confirmation = Field(default_factory=Confirmation)
    missing_evidence: list[str] = Field(default_factory=list)
    reproducible: bool | None = None
    predictive_evaluated: bool = False  # a model evaluated on unseen rows (e.g. holdout AUC), not a confirmation

    @model_validator(mode="after")
    def _confirmed_needs_a_rule(self) -> Validation:
        if self.state == "confirmed" and not (self.confirmation.passed and self.confirmation.rule in CONFIRMATION_RULES):
            raise ValueError("a finding is 'confirmed' only when a confirmation rule passed")
        if self.state == "confirmed" and self.label != "confirmation":
            raise ValueError("a confirmed finding carries the 'confirmation' label")
        if self.label == "confirmation" and self.state != "confirmed":
            raise ValueError("the 'confirmation' label is reserved for confirmed findings")
        return self


class Freshness(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["current", "stale", "unknown"] = "current"
    since: str | None = None
    reason: str | None = None
    assets: list[str] = Field(default_factory=list)


class ReviewScore(BaseModel):
    """The REV confidence number. Hand-built, not calibrated: never a probability of truth."""

    model_config = ConfigDict(extra="forbid")
    value: float | None = None
    calibrated: Literal[False] = False
    note: str = "heuristic review score from deterministic checks; not a calibrated probability"


class EvidenceBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = BUNDLE_VERSION
    verifier_version: str = "rev.v2"
    data: dict[str, Any] = Field(default_factory=dict)
    claim: dict[str, Any] = Field(default_factory=dict)
    method: dict[str, Any] = Field(default_factory=dict)
    validation: Validation
    limits: dict[str, Any] = Field(default_factory=dict)
    freshness: Freshness = Field(default_factory=Freshness)
    review_score: ReviewScore = Field(default_factory=ReviewScore)
    legacy: dict[str, Any] | None = None  # pre-P4-03 badge values, kept with their verifier version
