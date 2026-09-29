"""Structured + unstructured evidence fusion (N-8): pure functions, no DB access.

A claim cites measured results (`QuantitativeCitation`) and knowledge sections (`DocumentCitation`)
as separate kinds. This module builds both from what the deterministic path already recorded:

* quantitative citations from governed query receipts (query id, result hash) and the typed facts or
  result cells computed from them;
* document citations from the context compiler's receipts of pack sections (document id, path,
  anchor, document and section sha256) plus the section text, whose numbers are extracted and
  labelled `document` — never verified;
* the source of each number in the rendered text: `quantitative` when it binds to a measured value
  with the numbers guard's rule (`skills.selfcheck.numbers`), `document` when it matches only a
  cited document's number, else `unbound`. The guard itself is unchanged: a document number never
  makes `numbers_bound` or `bind_finding` pass;
* conflicts: a document sentence that names a measured fact's metric (and its subject, when the fact
  has one) and quotes a number of the same unit that none of the measured values matches.

Conflicts are flagged with measured data winning; they are not REV checks, because a stale document
does not make a correct measurement wrong.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from analystos.context.compiler import terms
from analystos.contracts.citations import (
    Citations,
    Conflict,
    DocumentCitation,
    DocumentNumber,
    NarrativeNumber,
    QuantitativeCitation,
)
from analystos.skills import selfcheck
from analystos.skills.result_facts import _mask, num

# Sections of the compiled context whose receipts are knowledge documents a claim may cite.
DOCUMENT_SECTIONS = ("glossary", "business_rules", "metrics", "external")
MAX_VALUES = 200
MAX_EXCERPT = 600
DEFAULT_TOLERANCE = 0.05  # relative difference below which a document number agrees with a measurement

_NUMBER = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?(?:\s?%|\s+percent\b)?", re.I)
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
_LIST_MARKER = re.compile(r"^\s*(?:#+\s*)?\d+[.)]\s")
_RATE_WORDS = {"rate", "share", "pct", "percent", "percentage", "ratio", "proportion"}

Unit = Literal["fraction", "percent", "value"]


@dataclass(frozen=True)
class Measured:
    """A measured value a document may contradict: the fact id, what it measures and in which unit."""

    id: str
    metric: str
    value: float
    unit: Unit
    subject: str | None = None


# ------------------------------------------------------------------------------ numbers
def _parse(token: str) -> tuple[float, Unit] | None:
    t = token.strip()
    percent = t.endswith("%") or t.lower().endswith("percent")
    core = re.sub(r"\s*(%|percent)$", "", t, flags=re.I).replace(",", "")
    try:
        return float(core), ("percent" if percent else "value")
    except ValueError:
        return None


def _year(value: float, unit: Unit, token: str) -> bool:
    return unit == "value" and "." not in token and 1900 <= value <= 2100


def document_numbers(text: str) -> list[DocumentNumber]:
    """Every number a section states, with its sentence; years and list markers are not claims."""
    out: list[DocumentNumber] = []
    lines = (_LIST_MARKER.sub("", line) for line in (text or "").splitlines())
    for sentence in (s.strip() for line in lines for s in _SENTENCES.split(line) if s.strip()):
        for m in _NUMBER.finditer(sentence):
            parsed = _parse(m.group(0))
            if parsed is None or _year(parsed[0], parsed[1], m.group(0)):
                continue
            out.append(DocumentNumber(text=m.group(0).strip(), value=parsed[0], unit=parsed[1], sentence=sentence[:400]))
    return out


def _binds(token: str, values: Sequence[float]) -> bool:
    """The numbers guard's own rule: equal within the precision written; a percentage may quote a fraction."""
    vals = [float(v) for v in values] + [abs(float(v)) for v in values if float(v) < 0]
    clean = re.sub(r"\s+percent$", "%", token.strip(), flags=re.I).replace(" %", "%")
    return selfcheck.numbers(selfcheck.Observation(kind="claim", text=clean.lstrip("+-"), upstream_values=vals)).passed


# ------------------------------------------------------------------------------ citations
def quantitative_from_facts(facts: Sequence[Any], receipts: Sequence[Mapping[str, Any]]) -> list[QuantitativeCitation]:
    """One citation per governed query receipt of a finding, with the typed facts computed from it
    (a fact that names no query belongs to every query of the finding)."""
    out = []
    for r in receipts:
        qid = str(r.get("query_id") or "")
        if not qid:
            continue
        mine = [f for f in facts if not getattr(f, "query_ids", None) or qid in f.query_ids]
        out.append(QuantitativeCitation(id=f"q:{qid}", label=str(r.get("role") or "evidence query"), query_id=qid,
                                        result_hash=r.get("result_hash"), query_hash=r.get("query_hash"),
                                        fact_ids=[f.id for f in mine], values=[float(f.value) for f in mine][:MAX_VALUES]))
    return out


def quantitative_from_results(results: Sequence[Mapping[str, Any]]) -> list[QuantitativeCitation]:
    """One citation per stored query result (an Ask answer or each answered analyst step): every
    numeric cell is a value it contributes."""
    out: list[QuantitativeCitation] = []
    seen: set[str] = set()
    for r in results:
        qid = str(r.get("query_id") or "")
        if not qid or qid in seen:
            continue
        seen.add(qid)
        values = [float(v) for row in r.get("rows") or [] for v in (row if isinstance(row, list | tuple) else [])
                  if not isinstance(v, bool) and num(v) is not None]
        step = r.get("step")
        out.append(QuantitativeCitation(id=f"q:{qid}", label=f"step {step} result" if step else "answer result",
                                        query_id=qid, result_hash=r.get("result_hash"), step=step,
                                        values=values[:MAX_VALUES]))
    return out


def document_citation(receipt: Mapping[str, Any], text: str | None = None, *, heading: str | None = None) -> DocumentCitation | None:
    """A citation for a compiler receipt of a pack section (P4-K05 receipt: document id, path, anchor,
    sha256s). Receipts without a document path (catalog, context entries) are not documents."""
    doc, path, sha = receipt.get("document_id"), receipt.get("path"), receipt.get("sha256")
    if not doc or not path or not sha:
        return None
    anchor = receipt.get("anchor")
    body = " ".join((text or "").split())
    return DocumentCitation(id=f"d:{doc}#{anchor or ''}", document_id=str(doc), path=str(path), anchor=anchor,
                            heading=heading, pack=receipt.get("pack"), section=receipt.get("section"),
                            document_sha256=str(sha), section_sha256=receipt.get("section_sha256"),
                            excerpt=body[:MAX_EXCERPT], numbers=document_numbers(text or ""),
                            trusted=bool(receipt.get("trusted", True)))


def document_citations(receipts: Iterable[Mapping[str, Any]], sections: Mapping[tuple[str, str | None], Mapping[str, Any]]
                       ) -> list[DocumentCitation]:
    """Document citations for the receipts of document sections, deduplicated by document + anchor.
    `sections` maps (document_id, anchor) to the stored section ({text, heading})."""
    out: dict[str, DocumentCitation] = {}
    for r in receipts:
        if r.get("section") not in DOCUMENT_SECTIONS:
            continue
        sec = sections.get((str(r.get("document_id")), r.get("anchor"))) or {}
        c = document_citation(r, sec.get("text"), heading=sec.get("heading"))
        if c is not None and c.id not in out:
            out[c.id] = c
    return list(out.values())


# ------------------------------------------------------------------------------ labelling
def label_numbers(text: str, quantitative: Sequence[QuantitativeCitation], documents: Sequence[DocumentCitation], *,
                  labels: Iterable[str] = ()) -> list[NarrativeNumber]:
    """The source of every number in `text`. Quantitative first: a number both measured and in a
    document is a measured number. Labels holding digits and step citations are masked as the guard does."""
    out: list[NarrativeNumber] = []
    masked = _mask(text or "", labels)
    for m in selfcheck._NUMBER.finditer(masked):
        token = m.group(0)
        q = next((c for c in quantitative if c.values and _binds(token, c.values)), None)
        if q is not None:
            out.append(NarrativeNumber(text=token, source="quantitative", citation_id=q.id))
            continue
        d = next((c for c in documents if any(_binds(token, [n.value / 100 if n.unit == "percent" else n.value])
                                              or _binds(token, [n.value]) for n in c.numbers)), None)
        out.append(NarrativeNumber(text=token, source="document", citation_id=d.id) if d is not None
                   else NarrativeNumber(text=token, source="unbound"))
    return out


# ------------------------------------------------------------------------------ conflicts
def measured_from_facts(facts: Sequence[Any]) -> list[Measured]:
    """Typed facts a document can contradict: levels, comparisons, changes, shares and counts with a
    metric (test statistics and p-values are not business claims)."""
    out = []
    for f in facts:
        if getattr(f, "kind", None) == "statistic" or not getattr(f, "metric", None):
            continue
        unit: Unit = "fraction" if f.unit == "fraction" else "percent" if f.unit in ("percent", "points") else "value"
        out.append(Measured(id=f.id, metric=str(f.metric), value=float(f.value), unit=unit, subject=f.subject))
    return out


def measured_from_results(results: Sequence[Mapping[str, Any]], *, cap: int = MAX_VALUES) -> list[Measured]:
    """Result cells as measured facts: the column is the metric, the row's first text cell the subject.
    A 0..1 value in a rate/share column is a fraction."""
    out: list[Measured] = []
    for r in results:
        cols = [str(c) for c in r.get("columns") or []]
        key = r.get("query_id") or "result"
        for i, row in enumerate(r.get("rows") or []):
            cells = list(row) if isinstance(row, list | tuple) else []
            subject = next((str(v) for v in cells if isinstance(v, str) and v.strip()), None)
            for c, v in zip(cols, cells, strict=False):
                if isinstance(v, bool) or num(v) is None:
                    continue
                value = float(v)
                unit: Unit = "fraction" if terms(c) & _RATE_WORDS and 0 <= value <= 1 else \
                    "percent" if terms(c) & {"pct", "percent", "percentage"} else "value"
                out.append(Measured(id=f"{key}:r{i}:{c}", metric=c, value=value, unit=unit, subject=subject))
                if len(out) >= cap:
                    return out
    return out


def _comparable(doc: DocumentNumber, fact: Measured) -> tuple[float, float] | None:
    """(document value, measured value) in one unit, or None when the units cannot be compared."""
    if doc.unit == "percent":
        if fact.unit == "fraction":
            return doc.value / 100, fact.value
        if fact.unit == "percent":
            return doc.value, fact.value
        return None
    if fact.unit == "value":
        return doc.value, fact.value
    if fact.unit == "fraction" and 0 <= doc.value <= 1 and "." in doc.text:
        return doc.value, fact.value
    return None


def _about(sentence_terms: set[str], fact: Measured) -> bool:
    metric = terms(fact.metric.replace("_", " "))
    if not metric or not metric <= sentence_terms:
        return False
    return not fact.subject or bool(terms(fact.subject) & sentence_terms)


def conflicts(measured: Sequence[Measured], documents: Sequence[DocumentCitation], *,
              tolerance: float = DEFAULT_TOLERANCE) -> list[Conflict]:
    """Document numbers about a measured fact's metric (and subject) that no measured value matches.
    A sentence that also quotes a matching number is not a conflict ("rose from 40% to 45%")."""
    out: dict[tuple[str, str, str], Conflict] = {}
    for doc in documents:
        by_sentence: dict[str, list[DocumentNumber]] = {}
        for n in doc.numbers:
            by_sentence.setdefault(n.sentence, []).append(n)
        for sentence, numbers in by_sentence.items():
            st = terms(sentence)
            for fact in measured:
                if not _about(st, fact):
                    continue
                pairs = [(n, p) for n in numbers if (p := _comparable(n, fact)) is not None]
                if not pairs or any(_agrees(n, p, tolerance) for n, p in pairs):
                    continue
                n, (dv, mv) = pairs[0]
                key = (doc.id, fact.id, n.text)
                out.setdefault(key, Conflict(
                    document_citation_id=doc.id, path=doc.path, anchor=doc.anchor, fact_id=fact.id,
                    metric=fact.metric, subject=fact.subject, document_text=n.text, document_value=n.value,
                    measured_value=fact.value, unit=fact.unit, relative_difference=round(_relative(dv, mv), 4),
                    sentence=sentence))
    return list(out.values())


def _relative(a: float, b: float) -> float:
    return abs(a - b) / max(abs(b), 1e-9)


def _agrees(n: DocumentNumber, pair: tuple[float, float], tolerance: float) -> bool:
    """Equal within the precision the document writes (in the compared unit), or within `tolerance`."""
    dv, mv = pair
    digits = re.sub(r"[^\d.]", "", n.text)
    decimals = len(digits.split(".", 1)[1]) if "." in digits else 0
    precision = 0.5 * 10 ** (-decimals) * (dv / n.value if n.value else 1.0)
    return abs(dv - mv) <= abs(precision) or _relative(dv, mv) <= tolerance


# ------------------------------------------------------------------------------ fuse
def fuse(subject_type: Literal["insight", "ask_turn"], subject_id: str, *, text: str,
         quantitative: Sequence[QuantitativeCitation], documents: Sequence[DocumentCitation],
         measured: Sequence[Measured], labels: Iterable[str] = (), tolerance: float = DEFAULT_TOLERANCE) -> Citations:
    return Citations(subject_type=subject_type, subject_id=subject_id, quantitative=list(quantitative),
                     documents=list(documents), narrative_numbers=label_numbers(text, quantitative, documents, labels=labels),
                     conflicts=conflicts(measured, documents, tolerance=tolerance))


__all__ = ["DEFAULT_TOLERANCE", "DOCUMENT_SECTIONS", "Measured", "conflicts", "document_citation", "document_citations",
           "document_numbers", "fuse", "label_numbers", "measured_from_facts", "measured_from_results",
           "quantitative_from_facts", "quantitative_from_results"]
