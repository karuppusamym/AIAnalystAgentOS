"""What-if arithmetic and its numbers guard (N-9). Deterministic: no model computes or words a number here.

`apply` takes the observed result of a compiled `SemanticQuery` (and, for a threshold change, the same
query re-measured with the changed filter) and applies the declared adjustments row by row. Every value
it returns is labelled: the baseline `observed`, the scenario value and its change `simulated`.
`guard` is the numbers guard for scenario text: a number that matches only a simulated value must sit
in a sentence that says it is simulated, and in an observed context (anything published) it is refused.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

import sqlglot
from pydantic import ValidationError
from sqlglot import exp

from analystos.contracts.scenario import (
    LabelledValue,
    ScenarioAdjustment,
    ScenarioCell,
    ScenarioRow,
    ScenarioSpec,
)
from analystos.contracts.semantic import SemanticFilter, SemanticQuery
from analystos.core.errors import InvalidInput
from analystos.skills import selfcheck
from analystos.skills.result_facts import _mask, clean, fmt_num, fmt_pct, num

SIMULATED_WORDS = re.compile(r"\b(simulat\w*|scenario|what-if|hypothetical\w*|would)\b", re.I)
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
CAVEAT = ("Changes apply to the observed result only: they do not model knock-on effects on other metrics, "
          "seasonality or behaviour.")


# ------------------------------------------------------------------------------------ validation
def outputs(query: SemanticQuery) -> list[str]:
    """The result's key columns, in the compiler's order: dimensions, then the time grain."""
    return [*query.dimensions, *([query.time.dimension] if query.time and query.time.grain else [])]


def validate(spec: ScenarioSpec) -> None:
    """Every change names something the query returns; refused before anything runs."""
    q = spec.semantic_query
    keys = outputs(q)
    for i, a in enumerate(spec.adjustments, start=1):
        if a.metric not in q.metrics:
            raise InvalidInput(f"Adjustment {i}: {a.metric!r} is not a metric of this query.")
        if a.segment is not None and a.segment.dimension not in keys:
            raise InvalidInput(f"Adjustment {i}: segment dimension {a.segment.dimension!r} is not grouped by this query.")
    fields = [o.field for o in spec.filter_overrides]
    if len(set(fields)) != len(fields):
        raise InvalidInput("Change each filter at most once.")
    for o in spec.filter_overrides:
        if sum(f.field == o.field for f in q.filters) != 1:
            raise InvalidInput(f"Filter override {o.field!r}: the query must have exactly one filter on that field.")


def remeasure_query(spec: ScenarioSpec) -> SemanticQuery | None:
    """The query with its overridden filters (a threshold change), or None when nothing is re-measured.
    It compiles and executes like any governed query: the compiler and gateway still decide."""
    if not spec.filter_overrides:
        return None
    changes = {o.field: o for o in spec.filter_overrides}
    filters = []
    for f in spec.semantic_query.filters:
        o = changes.get(f.field)
        if o is None:
            filters.append(f)
            continue
        try:
            filters.append(SemanticFilter(field=f.field, op=o.op or f.op, value=o.value))
        except ValidationError as exc:
            raise InvalidInput(f"Filter override {f.field!r}: " + exc.errors()[0]["msg"]) from None
    return spec.semantic_query.model_copy(update={"filters": filters})


# ------------------------------------------------------------------------------------ arithmetic
def summable(expression: str) -> bool:
    """A metric whose rows add up to a total: a top-level SUM or non-distinct COUNT (not AVG, ratios, MIN/MAX)."""
    try:
        tree = sqlglot.parse_one(expression)
    except sqlglot.errors.ParseError:
        return False
    return isinstance(tree, exp.Sum) or (isinstance(tree, exp.Count) and tree.find(exp.Distinct) is None)


def _same(a: Any, b: Any) -> bool:
    x, y = num(a), num(b)
    if x is not None and y is not None:
        return x == y
    return str(a) == str(b)


def _applies(adj: ScenarioAdjustment, key: dict[str, Any]) -> bool:
    return adj.segment is None or any(_same(key.get(adj.segment.dimension), v) for v in adj.segment.values)


def _adjust(value: float | None, adj: ScenarioAdjustment) -> float | None:
    if adj.kind == "set":
        return float(adj.amount)
    if value is None:
        return None
    if adj.kind == "scale":
        return value * (1 + adj.percent / 100)
    return value + adj.amount


def _cell(metric: str, observed: float | None, simulated: float | None, adjusted_by: list[int]) -> ScenarioCell:
    change = simulated - observed if observed is not None and simulated is not None else None
    pct = change / observed if change is not None and observed else None
    return ScenarioCell(metric=metric, observed=LabelledValue(value=clean(observed), basis="observed"),
                        simulated=LabelledValue(value=clean(simulated), basis="simulated"),
                        change=LabelledValue(value=clean(change), basis="simulated"),
                        change_pct=LabelledValue(value=clean(pct), basis="simulated"), adjusted_by=adjusted_by)


def _index(columns: Sequence[str], names: Iterable[str], what: str) -> dict[str, int]:
    lower = {str(c).lower(): i for i, c in enumerate(columns)}
    out = {}
    for n in names:
        if n.lower() not in lower:
            raise InvalidInput(f"The {what} result has no column {n!r}.")
        out[n] = lower[n.lower()]
    return out


def _keyed(query: SemanticQuery, columns: Sequence[str], rows: Sequence[Sequence[Any]], what: str
           ) -> tuple[list[tuple], dict[tuple, dict[str, Any]], dict[tuple, dict[str, float | None]]]:
    keys, metrics = _index(columns, outputs(query), what), _index(columns, query.metrics, what)
    order, members, values = [], {}, {}
    for row in rows:
        k = tuple(str(row[i]) if row[i] is not None else None for i in keys.values())
        if k in members:
            raise InvalidInput(f"The {what} result has two rows for the same key {k}.")
        order.append(k)
        members[k] = {name: row[i] for name, i in keys.items()}
        values[k] = {name: num(row[i]) for name, i in metrics.items()}
    return order, members, values


def apply(spec: ScenarioSpec, columns: Sequence[str], rows: Sequence[Sequence[Any]], *,
          remeasured: tuple[Sequence[str], Sequence[Sequence[Any]]] | None = None,
          summable_metrics: Iterable[str] = (), truncated: bool = False) -> tuple[list[ScenarioRow], list[ScenarioCell]]:
    """Rows keyed by the query's dimensions: the observed value, then the scenario value (the re-measured
    value when a threshold changed, else the observed one) with each matching adjustment applied in order.
    Totals only for summable metrics over a complete, grouped result."""
    q = spec.semantic_query
    order, members, observed = _keyed(q, columns, rows, "observed")
    start = observed
    if remeasured is not None:
        alt_order, alt_members, start = _keyed(q, remeasured[0], remeasured[1], "re-measured")
        for k in alt_order:
            if k not in members:
                order.append(k)
                members[k] = alt_members[k]
    out: list[ScenarioRow] = []
    for k in order:
        cells = []
        for metric in q.metrics:
            obs = (observed.get(k) or {}).get(metric)
            value = (start.get(k) or {}).get(metric)
            touched = []
            for i, adj in enumerate(spec.adjustments):
                if adj.metric == metric and _applies(adj, members[k]):
                    value = _adjust(value, adj)
                    touched.append(i)
            cells.append(_cell(metric, obs, value, touched))
        out.append(ScenarioRow(key=members[k], cells=cells))
    totals = []
    if outputs(q) and not truncated:
        for j, metric in enumerate(q.metrics):
            if metric not in set(summable_metrics):
                continue
            obs = [r.cells[j].observed.value for r in out if r.cells[j].observed.value is not None]
            sim = [r.cells[j].simulated.value for r in out if r.cells[j].simulated.value is not None]
            touched = sorted({i for r in out for i in r.cells[j].adjusted_by})
            totals.append(_cell(metric, sum(obs) if obs else None, sum(sim) if sim else None, touched))
    return out, totals


# ------------------------------------------------------------------------------------ words
def _segment(adj: ScenarioAdjustment) -> str:
    if adj.segment is None:
        return "on every row"
    return f"where {adj.segment.dimension} is {' or '.join(str(v) for v in adj.segment.values)}"


def assumption_statements(spec: ScenarioSpec, labels: dict[str, str]) -> list[str]:
    """The person's assumptions, then one plain statement per change and the standing caveat."""
    out = [a.strip() for a in spec.assumptions]
    for adj in spec.adjustments:
        name = labels.get(adj.metric, adj.metric)
        if adj.kind == "scale":
            out.append(f"{name} changes by {fmt_num(adj.percent, signed=True)}% {_segment(adj)}.")
        elif adj.kind == "shift":
            out.append(f"{name} changes by {fmt_num(adj.amount, signed=True)} {_segment(adj)}.")
        else:
            out.append(f"{name} is set to {fmt_num(adj.amount)} {_segment(adj)}.")
    originals = {f.field: f for f in spec.semantic_query.filters}
    for o in spec.filter_overrides:
        before = originals[o.field]
        out.append(f"The filter on {o.field} is {o.op or before.op} {o.value} instead of {before.op} {before.value}; "
                   "the query is re-measured through the query gateway with that change.")
    out.append(CAVEAT)
    return out


def summary(rows: list[ScenarioRow], totals: list[ScenarioCell], labels: dict[str, str]) -> str:
    """A template, not a model: observed sentences say observed, simulated sentences say simulated."""
    lines = []
    cells = rows[0].cells if len(rows) == 1 and not totals else totals
    for c in cells:
        name = labels.get(c.metric, c.metric)
        scope = "" if len(rows) == 1 else " (total over the rows shown)"
        lines.append(f"Observed {name}{scope}: {fmt_num(c.observed.value)}.")
        change = ""
        if c.change.value is not None:
            change = f" ({fmt_num(c.change.value, signed=True)}"
            change += f", {fmt_pct(c.change_pct.value, signed=True)})" if c.change_pct.value is not None else ")"
        lines.append(f"Simulated {name} under this scenario: {fmt_num(c.simulated.value)}{change}.")
    if len(rows) > 1:
        changed = {c.metric for r in rows for c in r.cells if c.adjusted_by}
        for metric in sorted(changed - {c.metric for c in totals}):
            lines.append(f"Simulated {labels.get(metric, metric)} is shown row by row below; it has no meaningful total.")
    if not lines:
        lines.append("The scenario returned no rows.")
    return " ".join(lines)


def values(rows: list[ScenarioRow], totals: list[ScenarioCell]) -> tuple[list[float], list[float]]:
    """(observed, simulated) numbers of a result, for the guard."""
    observed, simulated = [], []
    for c in [*(c for r in rows for c in r.cells), *totals]:
        if c.observed.value is not None:
            observed.append(c.observed.value)
        simulated += [v.value for v in (c.simulated, c.change, c.change_pct) if v.value is not None]
    return observed, simulated


def guard(text: str, observed: Iterable[float], simulated: Iterable[float], *, labels: Iterable[str] = (),
          allow_simulated: bool = True) -> dict[str, Any]:
    """Numbers guard with provenance labels. Every number must match an observed or simulated value
    within its shown precision. A number matching only a simulated value needs a simulated marker in its
    sentence; with `allow_simulated=False` (an observed context: publishing, a report, a finding) it is
    refused outright."""
    obs = [float(v) for v in observed if num(v) is not None]
    both = obs + [float(v) for v in simulated if num(v) is not None]
    problems: list[str] = []
    labels = list(labels)
    for sentence in (s for s in _SENTENCES.split(text or "") if s.strip()):
        masked = _mask(sentence, labels)
        against_obs = selfcheck.numbers(selfcheck.Observation(kind="claim", text=masked, upstream_values=obs))
        if not against_obs.evidence.get("numbers"):
            continue
        unbound_obs = against_obs.evidence.get("unbound") or []
        unbound = selfcheck.numbers(selfcheck.Observation(kind="claim", text=masked, upstream_values=both)).evidence.get("unbound") or []
        problems += [f"{t} matches no observed or simulated value" for t in unbound]
        sim_only = [t for t in unbound_obs if t not in unbound]
        if not sim_only:
            continue
        where = f"'{sentence.strip()[:120]}'"
        if not allow_simulated:
            problems.append(f"{where} quotes simulated {', '.join(sim_only)}: a simulated number cannot be presented as observed")
        elif not SIMULATED_WORDS.search(masked):
            problems.append(f"{where} quotes simulated {', '.join(sim_only)} without saying it is simulated")
    return {"ok": not problems, "problems": problems}


__all__ = ["CAVEAT", "apply", "assumption_statements", "guard", "outputs", "remeasure_query", "summable", "summary",
           "validate", "values"]
