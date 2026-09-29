"""Evidence citations (N-8): measured results and documents cited side by side, never as one kind.

A finding or an Ask answer cites two kinds of evidence:

* **quantitative** — a governed query result (query id + result hash) and the values computed from it.
  Only these can back a number: the numbers guard (`skills.result_facts.numbers_bound`,
  `evidence.facts.bind_finding`) binds narrative numbers to them and to nothing else.
* **document** — a knowledge section (document path + anchor) with its document and section sha256
  receipts. A number that appears only in a document is labelled `document`-sourced: it is shown as
  what the document says, never as a verified metric.

A `Conflict` is a document number about the same metric (and subject, when the fact has one) as a
measured fact that it does not match. Measured data wins; the conflict is flagged, not resolved.
Models never write any of this; `analystos.evidence.fusion` computes it deterministically.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CITATIONS_VERSION = "citations.v1"
CitationKind = Literal["quantitative", "document"]
NumberSource = Literal["quantitative", "document", "unbound"]


class QuantitativeCitation(BaseModel):
    """A governed query result the claim stands on."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["quantitative"] = "quantitative"
    id: str  # "q:<query_id>"
    label: str
    query_id: str
    result_hash: str | None = None
    query_hash: str | None = None
    step: int | None = None  # analyst-mode step number, when the answer has steps
    fact_ids: list[str] = Field(default_factory=list)
    values: list[float] = Field(default_factory=list)  # computed values (capped) this result contributed
    verified: bool = True  # a value here passed the deterministic path

    @model_validator(mode="after")
    def _has_receipt(self) -> QuantitativeCitation:
        if not self.query_id:
            raise ValueError("a quantitative citation needs the governed query id")
        return self


class DocumentNumber(BaseModel):
    """A number as a document states it. Always `verified=False`: a document is not a measurement."""

    model_config = ConfigDict(extra="forbid")
    text: str
    value: float
    unit: Literal["percent", "value"] = "value"
    sentence: str
    source: Literal["document"] = "document"
    verified: Literal[False] = False


class DocumentCitation(BaseModel):
    """A knowledge section the claim was grounded on, with the receipts to check it was not edited."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["document"] = "document"
    id: str  # "d:<document_id>#<anchor>"
    document_id: str
    path: str
    anchor: str | None = None
    heading: str | None = None
    pack: str | None = None
    section: str | None = None  # the compiler section it reached the prompt through (glossary, business_rules ...)
    document_sha256: str
    section_sha256: str | None = None
    excerpt: str = ""
    numbers: list[DocumentNumber] = Field(default_factory=list)
    trusted: bool = True


class NarrativeNumber(BaseModel):
    """One number of the rendered text and where it comes from. Only `quantitative` is a verified number."""

    model_config = ConfigDict(extra="forbid")
    text: str
    source: NumberSource
    citation_id: str | None = None

    @property
    def verified(self) -> bool:
        return self.source == "quantitative"


class Conflict(BaseModel):
    """A document number that disagrees with a measured fact about the same metric."""

    model_config = ConfigDict(extra="forbid")
    document_citation_id: str
    path: str
    anchor: str | None = None
    fact_id: str
    metric: str
    subject: str | None = None
    document_text: str
    document_value: float
    measured_value: float
    unit: Literal["fraction", "percent", "value"]
    relative_difference: float
    sentence: str
    resolution: Literal["measured_data_wins"] = "measured_data_wins"

    def caveat(self) -> str:
        shown = (f"{self.measured_value * 100:.4g}%" if self.unit == "fraction" else f"{self.measured_value:.4g}")
        about = f"{self.metric}{f' for {self.subject}' if self.subject else ''}"
        return (f"Document {self.path}{f'#{self.anchor}' if self.anchor else ''} states {self.document_text} for {about}; "
                f"the measured value is {shown}. The measured value is used.")


class Citations(BaseModel):
    """Every citation of one subject, the two kinds kept apart."""

    model_config = ConfigDict(extra="forbid")
    version: str = CITATIONS_VERSION
    subject_type: Literal["insight", "ask_turn"]
    subject_id: str
    quantitative: list[QuantitativeCitation] = Field(default_factory=list)
    documents: list[DocumentCitation] = Field(default_factory=list)
    narrative_numbers: list[NarrativeNumber] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)

    @model_validator(mode="after")
    def _numbers_cite_their_kind(self) -> Citations:
        quant = {c.id for c in self.quantitative}
        docs = {c.id for c in self.documents}
        for n in self.narrative_numbers:
            if n.source == "quantitative" and n.citation_id is not None and n.citation_id not in quant:
                raise ValueError(f"{n.text}: a quantitative number must cite a quantitative citation")
            if n.source == "document" and n.citation_id not in docs:
                raise ValueError(f"{n.text}: a document-sourced number must cite a document citation")
        for c in self.conflicts:
            if c.document_citation_id not in docs:
                raise ValueError("a conflict must name one of the document citations")
        return self

    def summary(self) -> dict[str, object]:
        return {"quantitative": len(self.quantitative), "documents": len(self.documents),
                "conflicts": len(self.conflicts),
                "document_sourced_numbers": sum(1 for n in self.narrative_numbers if n.source == "document"),
                "unbound_numbers": sum(1 for n in self.narrative_numbers if n.source == "unbound")}


__all__ = ["CITATIONS_VERSION", "CitationKind", "Citations", "Conflict", "DocumentCitation", "DocumentNumber",
           "NarrativeNumber", "NumberSource", "QuantitativeCitation"]
