"""Field-level diff between two versions of a semantic object (P7-02: the diff is shown before approval).

Ported from AIDataAnalyst@8b48fd9cf1d5ff1fcf4c05968f11b973b1cf9fdb:src/aida/semantic_diff.py (Atlas SM-7,
"reviewers see version deltas") with its tests (`tests/unit/test_semantic_diff.py`). The algorithm is
unchanged: plain mapping snapshots in, a sorted list of `added` / `removed` / `changed` entries out;
nested mappings recurse with dotted paths, any other differing value (lists included) is one `changed`
entry, unchanged fields are omitted. What is AnalystOS's: `metric_snapshot` / `model_snapshot` shape our
Ossie definitions for it (metrics and datasets keyed by name so each is its own entry), and the diff is
recorded in the metric approval's evidence and served by the semantic API before anyone decides.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ChangeKind = Literal["added", "removed", "changed"]


@dataclass(frozen=True, slots=True)
class FieldDelta:
    """One field-level difference; `field` is a dotted path inside nested mappings."""

    field: str
    change: ChangeKind
    before: Any = None
    after: Any = None


@dataclass(frozen=True, slots=True)
class SemanticDiff:
    entries: list[FieldDelta] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.entries)

    @property
    def changed_fields(self) -> list[str]:
        return [entry.field for entry in self.entries]

    def as_dict(self) -> dict[str, Any]:
        return {"has_changes": self.has_changes, "entries": [asdict(e) for e in self.entries]}


def diff_semantic_object(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None, *,
                         ignore_fields: frozenset[str] = frozenset()) -> SemanticDiff:
    """`before` is the approved (published) content, `after` the proposal; either may be None or {}
    (a first proposal has no predecessor: every field is `added`)."""
    return SemanticDiff(entries=_diff_mapping(before or {}, after or {}, ignore_fields, prefix=""))


def _diff_mapping(before: Mapping[str, Any], after: Mapping[str, Any], ignore_fields: frozenset[str], *,
                  prefix: str) -> list[FieldDelta]:
    entries: list[FieldDelta] = []
    for key in sorted(set(before.keys()) | set(after.keys())):
        if key in ignore_fields:
            continue
        path = f"{prefix}.{key}" if prefix else key
        if key in before and key not in after:
            entries.append(FieldDelta(field=path, change="removed", before=before[key], after=None))
            continue
        if key in after and key not in before:
            entries.append(FieldDelta(field=path, change="added", before=None, after=after[key]))
            continue
        before_val, after_val = before[key], after[key]
        if before_val == after_val:
            continue
        if isinstance(before_val, Mapping) and isinstance(after_val, Mapping):
            entries.extend(_diff_mapping(before_val, after_val, ignore_fields, prefix=path))
            continue
        entries.append(FieldDelta(field=path, change="changed", before=before_val, after=after_val))
    return entries


# ------------------------------------------------------------------------------------ AnalystOS snapshots
def metric_snapshot(definition: Mapping[str, Any] | None) -> dict[str, Any]:
    """A metric definition (SemanticMetricDef JSON) as a diffable snapshot: the primary expression and
    dialect as their own fields, other dialects keyed by dialect, empty optionals dropped."""
    if not definition:
        return {}
    d = dict(definition)
    exprs = d.pop("expressions", None) or []
    out = {k: v for k, v in d.items() if v not in (None, [], {}, "")}
    if exprs:
        out["expression"], out["dialect"] = exprs[0].get("expression"), exprs[0].get("dialect", "ANSI_SQL")
        others = {e.get("dialect", "ANSI_SQL"): e.get("expression") for e in exprs[1:]}
        if others:
            out["other_dialects"] = others
    return out


def model_snapshot(datasets: list[Mapping[str, Any]] | None, relationships: list[Mapping[str, Any]] | None) -> dict[str, Any]:
    """Model structure keyed by name: `datasets.<name>.fields.<field>` and `relationships.<name>`."""
    def ds(d: Mapping[str, Any]) -> dict[str, Any]:
        body = {k: v for k, v in d.items() if k not in ("name", "fields") and v not in (None, [], {}, "")}
        body["fields"] = {f["name"]: {k: v for k, v in f.items() if k != "name" and v not in (None, [], {}, "")}
                          for f in d.get("fields") or []}
        return body

    return {"datasets": {d["name"]: ds(d) for d in datasets or []},
            "relationships": {r["name"]: {k: v for k, v in r.items() if k != "name" and v not in (None, [], {}, "")}
                              for r in relationships or []}}
