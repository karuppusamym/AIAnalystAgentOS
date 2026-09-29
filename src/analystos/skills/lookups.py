"""Causes one join away (P8-16): related-table attributes and planned-vs-actual dates for hypotheses.

A fact table's own columns rarely hold the attribute an outcome concentrates in: late deliveries
cluster by the *customer's* region, returns by the *product's* category. An `AnalysisSpec` reaches
such an attribute through a declared join (`joins` + `via`); this module holds the deterministic
parts of that:

* `Lookup` - a validated many-to-one relationship between two in-scope tables of one source (the
  investigator loads them from the catalog; only these may become spec joins);
* `complete_joins` - a proposal (a model's or a template's) names the attribute with `via` only; the
  join itself (its key columns) is filled from the lookups, never taken from the proposal;
* `joined_dimensions` - the related tables' low-cardinality categorical columns, as segments;
* `planned_actual_pairs` - two date columns of one table that read as planned vs actual
  (promised/delivered, due/resolved, expected/shipped), for a `later_than` outcome.

Everything here reads column names, crawler roles and profiles; nothing names a domain.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from analystos.contracts.analysis import Derivation
from analystos.skills import hypothesis_templates as tmpl


@dataclass(frozen=True)
class Lookup:
    """`from_asset.from_column` references the unique `to_asset.to_column` (many-to-one)."""

    from_asset: str
    from_column: str
    to_asset: str
    to_column: str

    def join(self) -> dict[str, str]:
        return {"from_column": self.from_column, "asset": self.to_asset, "to_column": self.to_column}


# ------------------------------------------------------------------------------------ labels
_REF_TAIL = re.compile(r"(^|_)(sys_)?(id|key|code|no|num|number|fk|ref)$", re.IGNORECASE)
_TIME_TOKENS = frozenset({"date", "at", "time", "ts", "dt", "on", "datetime", "timestamp", "day", "utc"})


def ref_stem(column: str) -> str:
    """`customer_id` -> `customer`, `assigned_to` -> `assigned_to`: the entity a reference column names."""
    return _REF_TAIL.sub("", column).strip("_") or column


def joined_label(via: str, column: str, acronyms: dict[str, str] | None = None) -> str:
    """A related table's column named plainly by the reference it is read through: `customer region`."""
    stem = ref_stem(via)
    name = column if column == stem or column.startswith(f"{stem}_") else f"{stem}_{column}"
    return tmpl.humanize(name, acronyms)


def _tokens(column: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", column.lower()) if t]


def _time_stem(column: str) -> str:
    toks = _tokens(column)
    while len(toks) > 1 and toks[-1] in _TIME_TOKENS:
        toks.pop()
    return "_".join(toks)


def later_than_label(planned: str, actual: str, acronyms: dict[str, str] | None = None) -> str:
    """`promised_date`, `delivered_date` -> `delivered later than promised`."""
    return f"{tmpl.humanize(_time_stem(actual), acronyms)} later than {tmpl.humanize(_time_stem(planned), acronyms)}"


# ------------------------------------------------------------------------------------ planned vs actual
PLANNED_WORDS = frozenset({"promised", "due", "expected", "planned", "plan", "target", "scheduled", "estimated", "eta",
                           "committed", "deadline", "sla", "requested", "projected", "forecast"})
# Order is the tie-break when a planned date has several actual candidates (due -> resolved before closed).
ACTUAL_WORDS = ("delivered", "delivery", "resolved", "resolution", "completed", "completion", "fulfilled", "finished",
                "shipped", "shipment", "ship", "arrived", "arrival", "received", "paid", "payment", "closed", "close",
                "done", "actual", "ended", "end")


def planned_actual_pairs(datetimes: Iterable[str], limit: int = 2) -> list[tuple[str, str]]:
    """(planned, actual) date columns of one table: the planned one carries a planned word (promised, due,
    expected ...), the actual one an actual word (delivered, resolved, shipped ...) and no planned word. Each
    planned date pairs with the actual date sharing most other name words, then the earlier actual word."""
    cols = list(dict.fromkeys(datetimes))
    planned = [c for c in cols if PLANNED_WORDS & set(_tokens(c))]
    actual = [c for c in cols if c not in planned and any(t in ACTUAL_WORDS for t in _tokens(c))]
    pairs: list[tuple[str, str]] = []
    for p in planned:
        own = set(_tokens(p)) - PLANNED_WORDS - _TIME_TOKENS

        def rank(a: str, own: set[str] = own) -> tuple[int, int]:
            toks = _tokens(a)
            first = min((ACTUAL_WORDS.index(t) for t in toks if t in ACTUAL_WORDS), default=len(ACTUAL_WORDS))
            return (-len(own & (set(toks) - _TIME_TOKENS)), first)

        if actual:
            pairs.append((p, min(actual, key=rank)))
        if len(pairs) >= limit:
            break
    return pairs


# ------------------------------------------------------------------------------------ joined segments
def joined_dimensions(related: Iterable[tuple[Lookup, list[tmpl.Col]]], *, per_table: int = 2, limit: int = 4,
                      acronyms: dict[str, str] | None = None) -> list[tuple[Derivation, Lookup]]:
    """Low-cardinality categorical columns of the related tables (2-30 values, not an identifier, key, flag or
    free text), as `via` segments with a plain label. Interleaved across tables (each table's first column,
    then each table's second) so a small budget still reaches every related entity."""
    per: list[list[tuple[Derivation, Lookup]]] = []
    for lk, cols in related:
        dims = [Derivation(type="column", column=c.name, via=lk.from_column, label=joined_label(lk.from_column, c.name, acronyms))
                for c in cols
                if c.semantic_type == "categorical" and 2 <= (c.profile.get("distinct") or 0) <= 30 and c.name != lk.to_column
                and c.role not in ("identifier", "foreign_key") and not c.flag_true and not tmpl.TEXTY.search(c.name)]
        per.append([(d, lk) for d in dims[:per_table]])
    out: list[tuple[Derivation, Lookup]] = []
    for i in range(per_table):
        out += [dims[i] for dims in per if i < len(dims)]
    return out[:limit]


# ------------------------------------------------------------------------------------ proposals
def _derivation_dicts(spec: dict[str, Any]) -> list[dict[str, Any]]:
    out = [spec[k] for k in ("outcome", "segment", "time") if isinstance(spec.get(k), dict)]
    return out + [d for d in spec.get("drivers") or [] if isinstance(d, dict)]


def complete_joins(spec: dict[str, Any], lookups: Iterable[Lookup],
                   acronyms: dict[str, str] | None = None) -> tuple[dict[str, Any], list[str]]:
    """Declare the join of every `via` a proposal uses without declaring it, from the validated lookups of the
    spec's asset (the proposal names the attribute; code decides the join), and label joined and later-than
    derivations plainly. Returns the completed spec and the reasons it cannot be completed. A join the
    proposal declares itself is kept as written (`investigator.validate_spec` checks it against the lookups); a
    declared join no derivation or filter reads is dropped, so one test is one spec with or without it."""
    if not isinstance(spec, dict):
        return spec, []
    spec = {**spec, "joins": [dict(j) for j in spec.get("joins") or [] if isinstance(j, dict)]}
    for k in ("outcome", "segment", "time"):
        if isinstance(spec.get(k), dict):
            spec[k] = dict(spec[k])
    spec["drivers"] = [dict(d) if isinstance(d, dict) else d for d in spec.get("drivers") or []]
    spec["filters"] = [dict(f) if isinstance(f, dict) else f for f in spec.get("filters") or []]
    items = _derivation_dicts(spec) + [f for f in spec["filters"] if isinstance(f, dict)]
    declared = {j.get("from_column") for j in spec["joins"]}
    errors: list[str] = []
    candidates = [lk for lk in lookups if lk.from_asset == spec.get("asset")]
    for item in items:
        via = item.get("via")
        if not via or via in declared:
            continue
        hits = [lk for lk in candidates if lk.from_column == via]
        if len(hits) != 1:
            errors.append(f"no validated many-to-one relationship from {spec.get('asset')}.{via}" if not hits else
                          f"{spec.get('asset')}.{via} references several tables; declare the join")
            continue
        spec["joins"].append(hits[0].join())
        declared.add(via)
    used = {item.get("via") for item in items if item.get("via")}
    spec["joins"] = [j for j in spec["joins"] if j.get("from_column") in used]  # a join nothing reads is dropped
    for d in _derivation_dicts(spec):
        if d.get("label") or not isinstance(d.get("column"), str):
            continue
        if d.get("via"):
            d["label"] = joined_label(str(d["via"]), d["column"], acronyms)
        elif d.get("type") == "later_than" and isinstance(d.get("end_column"), str):
            d["label"] = later_than_label(d["column"], d["end_column"], acronyms)
    if not spec["joins"]:
        spec.pop("joins")
    return spec, errors


def for_prompt(related: Iterable[tuple[Lookup, list[tmpl.Col]]], max_columns: int = 12) -> list[dict[str, Any]]:
    """What a model needs to use a related table: the reference column (`via`), the table, and its columns."""
    return [{"asset": lk.from_asset, "via": lk.from_column, "references": f"{lk.to_asset}.{lk.to_column}",
             "columns": [c.name for c in cols if c.name != lk.to_column and c.semantic_type not in ("id", "text")][:max_columns]}
            for lk, cols in related]
