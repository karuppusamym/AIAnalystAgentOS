"""Hypothesis templates: AnalysisSpec patterns bound to a table's columns (spec v3 §3.6).

Domain packs describe their analyst playbooks as data (packs/<name>/templates.yaml); this engine
binds them to a concrete table and names no domain. Binding is by semantic type, crawler role, name
pattern and profile bounds, never by a hard-coded column name, so a template applies to every table
shaped like the one it was written for. Output proposals have the same shape as a model's, and go
through the same validation (investigator.validate_spec), so a template can never widen what a run may
touch. The document format:

  slots:      name -> selector. A selector filters the table's columns in order:
                type (semantic type or list), role (crawler role or list), not_role, name (regex or list,
                all must match), not_name, exclude_text (drop free-text-named columns), distinct [lo, hi],
                max_value (profile max <=), from (filter another slot's result instead of the table),
                prefer (regex: matching columns first), limit, fallback (another slot when empty)
  outcomes:   name -> {kind: flag_rate | duration_hours | column, ...}
                flag_rate: slot, success_name (regex: a success-named flag is analysed as its failure),
                           success_prefix (regex stripped for the label)
                duration_hours: start, end (slots), label
                column: slot, limit
  segments:   name -> list of items {slot, as: column | bucket | after_hours | hour_of_day | day_of_week,
                edges, label, label_style: humanize | plain, limit}
  templates:  list of {id, method, outcome, segment, time: {slot, grain}, drivers: {group, limit, min},
                filters: [{slot, op, value | value: {lowest_top_value: true, default}}], priority
                (str | {default, by_segment: {<derivation type>: str}}), question, statement,
                when_filtered: {question, statement}}. A segment reference is {group, types, limit} or an
                inline item, or a list of them. Text placeholders: {outcome} {Outcome} {segment}.
  display_name: regex of display-name columns preferred in drill-downs
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from analystos.contracts.analysis import Derivation

TEXTY = re.compile(r"(description|comment|note|summary|title|text|message)")
CORE_ACRONYMS = {"id": "ID"}


@dataclass(frozen=True)
class Col:
    name: str
    semantic_type: str | None
    role: str | None = None
    profile: dict[str, Any] = field(default_factory=dict)


def _list(v: Any) -> list[Any]:
    return [] if v is None else list(v) if isinstance(v, (list, tuple)) else [v]


def humanize(column: str, acronyms: dict[str, str] | None = None) -> str:
    acr = {**CORE_ACRONYMS, **(acronyms or {})}
    return " ".join(acr.get(w, w) for w in column.split("_"))


def flag_label(column: str, breach: bool, strip: str | None, acronyms: dict[str, str] | None = None) -> str:
    """A success-named flag analysed as its failure reads 'missed <stem>'; any other flag reads as itself."""
    if breach:
        stem = re.sub(strip, "", column) if strip else column
        return f"missed {humanize(stem, acronyms)}" if stem != column else f"not {humanize(column, acronyms)}"
    return humanize(re.sub(r"^is_", "", column), acronyms)


# ------------------------------------------------------------------------------------ slots
def _matches(c: Col, sel: dict[str, Any]) -> bool:
    if "type" in sel and c.semantic_type not in _list(sel["type"]):
        return False
    if "role" in sel and c.role not in _list(sel["role"]):
        return False
    if "not_role" in sel and c.role in _list(sel["not_role"]):
        return False
    if "name" in sel and not all(re.search(p, c.name) for p in _list(sel["name"])):
        return False
    if "not_name" in sel and any(re.search(p, c.name) for p in _list(sel["not_name"])):
        return False
    if sel.get("exclude_text") and TEXTY.search(c.name):
        return False
    if "distinct" in sel:
        lo, hi = sel["distinct"]
        if not lo <= (c.profile.get("distinct") or 0) <= hi:
            return False
    if "max_value" in sel:
        mx = c.profile.get("max")
        try:
            if mx is None or float(mx) > float(sel["max_value"]):
                return False
        except (TypeError, ValueError):
            return False
    return True


def resolve_slots(cols: list[Col], slots: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    by_name = {c.name: c for c in cols}
    out: dict[str, list[str]] = {}

    def get(name: str, stack: tuple[str, ...] = ()) -> list[str]:
        if name in out:
            return out[name]
        if name in stack or name not in slots:
            return []
        sel = slots[name]
        base = get(sel["from"], stack + (name,)) if "from" in sel else [c.name for c in cols]
        found = [n for n in base if _matches(by_name[n], sel)]
        if "prefer" in sel:
            rx = re.compile(sel["prefer"])
            found = [n for n in found if rx.search(n)] + [n for n in found if not rx.search(n)]
        if not found and "fallback" in sel:
            found = get(sel["fallback"], stack + (name,))
        out[name] = found[: sel["limit"]] if sel.get("limit") else found
        return out[name]

    for name in slots:
        get(name)
    return out


# ------------------------------------------------------------------------------------ derivations
class _Binder:
    def __init__(self, cols: list[Col], doc: dict[str, Any], acronyms: dict[str, str] | None):
        self.cols = {c.name: c for c in cols}
        self.doc = doc
        self.acr = acronyms or {}
        self.slots = resolve_slots(cols, doc.get("slots") or {})

    def outcomes(self, name: str | None) -> list[Derivation]:
        if not name:
            return []
        o = (self.doc.get("outcomes") or {}).get(name)
        if o is None:
            return []
        kind = o.get("kind", "column")
        if kind == "flag_rate":
            out = []
            for col in self.slots.get(o["slot"], [])[: o.get("limit", 1)]:
                breach = bool(o.get("success_name") and re.search(o["success_name"], col))
                out.append(Derivation(type="equals", column=col, value=not breach,
                                      label=o.get("label") or flag_label(col, breach, o.get("success_prefix"), self.acr)))
            return out
        if kind == "duration_hours":
            starts, ends = self.slots.get(o["start"], []), self.slots.get(o["end"], [])
            if not starts or not ends:
                return []
            return [Derivation(type="duration_hours", column=starts[0], end_column=ends[0], label=o.get("label") or "duration hours")]
        return [Derivation(type="column", column=c, label=o.get("label") or humanize(c, self.acr))
                for c in self.slots.get(o["slot"], [])[: o.get("limit", 1)]]

    def _label(self, item: dict[str, Any], col: str) -> str:
        if item.get("label"):
            return item["label"]
        return col.replace("_", " ") if item.get("label_style") == "plain" else humanize(col, self.acr)

    def item(self, item: dict[str, Any]) -> list[Derivation]:
        cols = self.slots.get(item["slot"], [])
        cols = cols[: item["limit"]] if item.get("limit") else cols
        kind = item.get("as", "column")
        out = []
        for c in cols:
            if kind == "bucket":
                out.append(Derivation(type="bucket", column=c, edges=item.get("edges") or [0, 1, 2, 3], label=self._label(item, c)))
            else:
                out.append(Derivation(type=kind, column=c, label=self._label(item, c)))
        return out

    def group(self, name: str) -> list[Derivation]:
        return [d for it in (self.doc.get("segments") or {}).get(name, []) for d in self.item(it)]

    def segments(self, ref: Any) -> list[Derivation]:
        out: list[Derivation] = []
        for r in _list(ref):
            ds = self.group(r["group"]) if "group" in r else self.item(r)
            if r.get("types"):
                ds = [d for d in ds if d.type in r["types"]]
            out += ds[: r["limit"]] if "group" in r and r.get("limit") else ds
        return out

    def filters(self, specs: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """Bound filters, or None when any filter's slot is unbound (the template runs unfiltered)."""
        out = []
        for f in specs:
            cols = self.slots.get(f["slot"], [])
            if not cols:
                return None
            value = f.get("value")
            if isinstance(value, dict) and value.get("lowest_top_value"):
                tops = [t.get("value") for t in (self.cols[cols[0]].profile.get("top_values") or [])]
                value = min(tops, key=lambda v: str(v)) if tops else value.get("default")
            out.append({"column": cols[0], "op": f.get("op", "="), "value": value})
        return out


def _priority(p: Any, seg: Derivation | None) -> str:
    if isinstance(p, dict):
        return (p.get("by_segment") or {}).get(seg.type if seg else "", p.get("default", "medium"))
    return p or "medium"


def _text(t: str, outcome: Derivation | None, seg: Derivation | None) -> str:
    o = (outcome.label or outcome.column) if outcome else ""
    return t.format(outcome=o, Outcome=o[:1].upper() + o[1:], segment=(seg.label or seg.column) if seg else "")


def propose(fq: str, cols: list[Col], doc: dict[str, Any], acronyms: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Every proposal the templates of one pack make for table `fq`, in template order."""
    b = _Binder(cols, doc, acronyms)
    out: list[dict[str, Any]] = []
    for t in doc.get("templates") or []:
        method = t["method"]
        outcomes: list[Derivation | None] = b.outcomes(t.get("outcome")) if t.get("outcome") else [None]
        if not outcomes:
            continue
        segs: list[Derivation | None] = b.segments(t["segment"]) if t.get("segment") else [None]
        if not segs:
            continue
        base: dict[str, Any] = {"method": method, "asset": fq}
        if t.get("time"):
            times = b.slots.get(t["time"]["slot"], [])
            if not times:
                continue
            base["time"] = {"type": "date_trunc", "column": times[0], "grain": t["time"].get("grain", "month")}
        if t.get("drivers"):
            drivers = b.segments({k: v for k, v in t["drivers"].items() if k != "min"})
            if len(b.segments({k: v for k, v in t["drivers"].items() if k not in ("min", "limit")})) < t["drivers"].get("min", 1):
                continue
            base["drivers"] = [d.model_dump() for d in drivers]
        filters = b.filters(t.get("filters") or [])
        texts = t.get("when_filtered") if filters and t.get("when_filtered") else t
        if filters:
            base["filters"] = filters
        for outcome in outcomes:
            for seg in segs:
                spec = dict(base)
                if outcome is not None:
                    spec["outcome"] = outcome.model_dump()
                if seg is not None:
                    spec["segment"] = seg.model_dump()
                out.append({"question": _text(texts.get("question", t.get("question", "")), outcome, seg),
                            "statement": _text(texts.get("statement", t.get("statement", "")), outcome, seg),
                            "priority": _priority(t.get("priority"), seg), "template": t.get("id"), "spec": spec})
    return out


def bind_kpis(cols: list[Col], doc: dict[str, Any], kpis: Iterable[dict[str, Any]],
              acronyms: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Starter KPIs of a pack bound to a table: each names an outcome of the templates document and an
    aggregate (rate | mean | median | sum | count); KPIs whose outcome does not bind are left out."""
    b = _Binder(cols, doc, acronyms)
    out = []
    for k in kpis:
        if k.get("aggregate") == "count":
            out.append({**k, "derivation": None})
            continue
        ds = b.outcomes(k.get("outcome"))
        if ds:
            out.append({**k, "derivation": ds[0].model_dump()})
    return out


def prefer_display(columns: list[str], patterns: Iterable[str]) -> list[str]:
    """Display-name columns (as the enabled packs define them) first, order otherwise kept."""
    rxs = [re.compile(p) for p in patterns]
    if not rxs:
        return list(columns)
    hit = [c for c in columns if any(r.search(c) for r in rxs)]
    return hit + [c for c in columns if c not in hit]
