"""Facts an analyst-mode Ask step's result supports, computed in code (never by a model).

A step's result (columns, rows, true row count) becomes:

* ``step_facts``: per numeric measure column the count, total, average, minimum and maximum, with the
  label of the min/max row. Identifier-like columns (``*_id``, ``*_key``, ``*_number`` ...) get only a
  distinct count. Pre-aggregated columns (``avg_*``, ``mean_*``, ``*_rate``, ``pct``, ``percent``,
  ``share``, ``ratio``, ``min_*``/``max_*``) get no total: a sum of averages or rates means nothing.
  A truncated result is stated first, since every figure then covers the returned rows only.
* ``series_analysis``: with a time-like column and at least 5 points (one row per period), the OLS
  slope per period (`stats.linear_trend`) and as a share of the mean (flat when |slope| < 1% of the
  mean), robust anomalies (`stats.robust_anomalies`: z = 0.6745·(x − median)/MAD, flagged at |z| >= 3)
  and the periods missing from a regular grain.
* ``two_period``: previous/current values, as columns (prev/previous/before/baseline/last/prior vs
  curr/current/after/now/this/latest) or as a period column with exactly two values. The total change
  and, with a label column, per member: change, % change (null when previous is 0), share of the total
  change (not clamped: an offset pushes other members past 100%), ranked by |change| and split into
  drivers (same sign as the total) and offsets, plus members that appeared or disappeared. Nothing
  moved: no drivers.
* ``step_checks``: the P7-04 self-checks (`skills/selfcheck.py`) plus analyst-step checks (a one-row
  breakdown, a share outside 0..100%, missing periods, a comparison with one period). pass | suspect.
* ``numbers_bound``: a text may quote only computed numbers, each within the precision it is written
  with (`selfcheck.numbers`), and every sentence with a number cites a step ``(step N)``.

Fractions are stored as fractions (0.125 = 12.5%); `fmt_pct` writes them as percentages.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from analystos.skills import selfcheck

MAX_MEMBERS = 20
MIN_SERIES_POINTS = 5
FLAT_SHARE_OF_MEAN = 0.01
ANOMALY_Z = 3.0
MAX_MISSING = 12
LABEL_CHARS = 80

_ID_WORDS = frozenset({"id", "uuid", "guid", "key", "code", "number", "no"})
_PRE_AGG_WORDS = frozenset({"avg", "mean", "average", "median", "min", "max", "minimum", "maximum", "rate", "pct",
                            "percent", "percentage", "ratio", "share"})
_TIME_WORDS = frozenset({"date", "time", "timestamp", "day", "week", "month", "quarter", "year", "period", "dt", "ts",
                         "yyyymm"})
# an integer column is a period only when its name says so and its values fit: 2025, month 1..12, 202506
_NUMERIC_TIME_WORDS = {"year": (1900, 2200), "month": (1, 12), "quarter": (1, 4), "week": (1, 53),
                       "yyyymm": (190001, 220012), "period": (190001, 220012)}
_CYCLIC_WORDS = frozenset({"hour", "dow", "weekday", "hod"})
_PREV_WORDS = frozenset({"prev", "previous", "before", "baseline", "last", "prior"})
_CURR_WORDS = frozenset({"curr", "current", "after", "now", "this", "latest"})
_SHARE_WORDS = frozenset({"share", "pct", "percent", "percentage"})
_BREAKDOWN_KINDS = frozenset({"breakdown", "ranking", "drivers"})
_ISO = re.compile(r"^\d{4}-\d{2}(?:-\d{2})?(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?$")


# ------------------------------------------------------------------------------------ values
def num(v: Any) -> float | None:
    """A finite number, or None (booleans and strings are not numbers here)."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int | float | Decimal):
        f = float(v)
        return f if math.isfinite(f) else None
    return None


def clean(x: float | None) -> float | int | None:
    """Stored form: rounded to 6 decimals, an integral value as int."""
    if x is None or not math.isfinite(x):
        return None
    x = round(float(x), 6)
    return int(x) if x.is_integer() and abs(x) < 1e15 else x


def as_time(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    if isinstance(v, str) and _ISO.match(v.strip()):
        s = v.strip().replace("Z", "+00:00")
        if len(s) == 7:
            s += "-01"
        try:
            return datetime.fromisoformat(s).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def tokens(name: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1_\2", str(name)).lower())


def fmt_num(v: Any, *, signed: bool = False) -> str:
    """A number as the synthesis writes it; the numbers guard accepts it within the precision shown."""
    x = num(v)
    if x is None:
        return "n/a"
    a = abs(x)
    if x.is_integer() and a < 1e15:
        text = f"{int(x):,}"
    elif a >= 1000:
        text = f"{x:,.0f}"
    elif a >= 100:
        text = f"{x:,.1f}"
    elif a >= 1:
        text = f"{x:,.2f}".rstrip("0").rstrip(".")
    else:
        text = f"{x:.4f}".rstrip("0").rstrip(".")
        if text in ("0", "-0"):
            text = f"{x:.8f}".rstrip("0").rstrip(".")
    return ("+" if signed and x > 0 else "") + text


def fmt_pct(v: Any, *, signed: bool = False) -> str:
    x = num(v)
    if x is None:
        return "n/a"
    return ("+" if signed and x > 0 else "") + f"{x * 100:.1f}%"


# ------------------------------------------------------------------------------------ columns
def _is_identifier(toks: list[str]) -> bool:
    return bool(toks) and toks[-1] in _ID_WORDS


def _is_cyclic(toks: list[str]) -> bool:
    return bool(set(toks) & _CYCLIC_WORDS) or ("of" in toks and bool(set(toks) & {"day", "week", "hour"}))


def pre_aggregated(name: str) -> bool:
    return bool(set(tokens(name)) & _PRE_AGG_WORDS)


def grouped_columns(sql: str | None, dialect: str = "postgres") -> set[str]:
    """Output names of the columns a statement groups by (a numeric grouping such as priority 1..5 is a
    dimension, not a measure). Empty when the SQL cannot be read."""
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(sql or "", read=dialect)
    except Exception:  # noqa: BLE001 - unreadable SQL: fall back to the column heuristics
        return set()
    select = tree.find(exp.Select) if tree is not None else None
    group = select.args.get("group") if select is not None else None
    if group is None:
        return set()
    keys = [g for g in group.expressions]
    out = set()
    for pos, e in enumerate(select.expressions, start=1):
        inner = e.this if isinstance(e, exp.Alias) else e
        for g in keys:
            if (isinstance(g, exp.Literal) and not g.is_string and str(g.this) == str(pos)) or \
                    (isinstance(g, exp.Column) and g.name.lower() == e.alias_or_name.lower()) or g == inner:
                out.add(e.alias_or_name.lower())
    return out


def classify(columns: Sequence[str], rows: Sequence[Sequence[Any]], dimensions: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Each column's kind: measure | identifier | time | label. `dimensions` (grouped-by output names)
    are never measures."""
    grouped = {d.lower() for d in dimensions}
    out = []
    for i, name in enumerate(columns):
        vals = [r[i] for r in rows if i < len(r) and r[i] is not None]
        toks = tokens(name)
        numeric = bool(vals) and all(num(v) is not None for v in vals)
        integral = numeric and all(float(num(v) or 0).is_integer() for v in vals)
        if numeric and str(name).lower() in grouped and not _is_identifier(toks):
            span = _NUMERIC_TIME_WORDS.get(toks[-1]) if toks else None
            kind = "time" if integral and span and all(span[0] <= float(num(v) or 0) <= span[1] for v in vals) else "label"
        elif _is_identifier(toks):
            kind = "identifier"
        elif _is_cyclic(toks):
            kind = "label"
        elif numeric:
            span = _NUMERIC_TIME_WORDS.get(toks[-1]) if toks and not set(toks) & (_PREV_WORDS | _CURR_WORDS) else None
            kind = "time" if integral and span and all(span[0] <= float(num(v) or 0) <= span[1] for v in vals) else "measure"
        elif (vals and all(as_time(v) is not None for v in vals)) or (not vals and set(toks) & _TIME_WORDS):
            kind = "time"
        else:
            kind = "label"
        out.append({"name": name, "index": i, "kind": kind, "pre_aggregated": kind == "measure" and pre_aggregated(name)})
    return out


def _label_indexes(cols: list[dict[str, Any]]) -> list[int]:
    labels = [c["index"] for c in cols if c["kind"] in ("label", "time")]
    return labels or [c["index"] for c in cols if c["kind"] == "identifier"]


def row_label(row: Sequence[Any], idx: Sequence[int], names: Sequence[str] = ()) -> str | None:
    """A row's label; a numeric value is named by its column ("priority 4"), so it never reads as a quantity."""
    parts = []
    for i in idx:
        if i >= len(row) or row[i] is None:
            continue
        v = row[i]
        plain = str(v)
        numeric = num(v) is not None or bool(re.fullmatch(r"[-+]?\d+(?:\.\d+)?", plain))
        parts.append(f"{names[i]} {plain}" if numeric and i < len(names) else plain)
    return " / ".join(parts)[:LABEL_CHARS] if parts else None


# ------------------------------------------------------------------------------------ facts
def step_facts(columns: Sequence[str], rows: Sequence[Sequence[Any]], *, row_count: int | None = None,
               truncated: bool = False, dimensions: Iterable[str] = ()) -> dict[str, Any]:
    cols = classify(columns, rows, dimensions)
    shown = len(rows)
    total_rows = row_count if row_count is not None else shown
    cut = bool(truncated) or total_rows > shown
    labels_at = _label_indexes(cols)
    measures, identifiers, labels = [], [], []
    for c in cols:
        i = c["index"]
        if c["kind"] == "measure":
            pairs = [(num(r[i]), row_label(r, labels_at, columns)) for r in rows if i < len(r) and num(r[i]) is not None]
            if not pairs:
                continue
            values = [v for v, _ in pairs]
            lo = min(range(len(pairs)), key=lambda k: pairs[k][0])
            hi = max(range(len(pairs)), key=lambda k: pairs[k][0])
            m: dict[str, Any] = {"column": c["name"], "count": len(values), "avg": clean(sum(values) / len(values)),
                                 "min": clean(values[lo]), "min_label": pairs[lo][1], "max": clean(values[hi]),
                                 "max_label": pairs[hi][1], "pre_aggregated": c["pre_aggregated"]}
            if not c["pre_aggregated"]:
                m["total"] = clean(sum(values))
            measures.append(m)
        elif c["kind"] == "identifier":
            identifiers.append({"column": c["name"], "distinct": len({str(r[i]) for r in rows if i < len(r) and r[i] is not None})})
        else:
            labels.append({"column": c["name"], "kind": c["kind"],
                           "distinct": len({str(r[i]) for r in rows if i < len(r) and r[i] is not None})})
    facts: dict[str, Any] = {"row_count": total_rows, "rows_shown": shown, "truncated": cut, "measures": measures,
                             "identifiers": identifiers, "labels": labels,
                             "label_columns": [columns[i] for i in labels_at],
                             "time_column": next((c["name"] for c in cols if c["kind"] == "time"), None)}
    if shown == 1 and measures:
        facts["values"] = {m["column"]: m["max"] for m in measures}
    facts["statements"] = statements(facts)
    return facts


def statements(f: Mapping[str, Any]) -> list[str]:
    """The facts as plain sentences, truncation first."""
    out = []
    if f["truncated"]:
        out.append(f"The result is truncated: {fmt_num(f['rows_shown'])} of at least {fmt_num(f['row_count'])} rows "
                   "were returned, so every figure covers the returned rows only.")
    if not f["rows_shown"]:
        out.append("No rows were returned.")
        return out
    if f.get("values"):
        out += [f"{col}: {fmt_num(v)}." for col, v in f["values"].items()]
    else:
        for m in f["measures"][:4]:
            parts = [f"total {fmt_num(m['total'])}"] if "total" in m else []
            parts += [f"average {fmt_num(m['avg'])}", f"highest {fmt_num(m['max'])} ({m['max_label']})",
                      f"lowest {fmt_num(m['min'])} ({m['min_label']})"]
            out.append(f"{m['column']} over {fmt_num(m['count'])} rows: " + ", ".join(parts) + ".")
    out += [f"{d['column']}: {fmt_num(d['distinct'])} distinct values." for d in f["identifiers"]]
    return out


# ------------------------------------------------------------------------------------ series
def _grain(times: list[datetime]) -> str | None:
    diffs = sorted((b - a).days for a, b in zip(times, times[1:], strict=False))
    if not diffs:
        return None
    d = diffs[len(diffs) // 2]
    for grain, lo, hi in (("day", 1, 1), ("week", 7, 7), ("month", 28, 31), ("quarter", 89, 92), ("year", 365, 366)):
        if lo <= d <= hi:
            return grain
    return None


def _step(t: datetime, grain: str) -> datetime:
    if grain == "day":
        return t + timedelta(days=1)
    if grain == "week":
        return t + timedelta(days=7)
    months = {"month": 1, "quarter": 3, "year": 12}[grain]
    m = t.month - 1 + months
    return t.replace(year=t.year + m // 12, month=m % 12 + 1, day=1)


def _missing(periods: list[Any]) -> tuple[str | None, list[str]]:
    times = [as_time(p) for p in periods]
    if all(t is not None for t in times):
        grain = _grain(times)  # type: ignore[arg-type]
        if grain is None:
            return None, []
        present = {t.date() for t in times}  # type: ignore[union-attr]
        start = times[0] if grain in ("day", "week") else times[0].replace(day=1)  # type: ignore[union-attr]
        if grain not in ("day", "week"):
            present = {d.replace(day=1) for d in present}
        out, t = [], start
        while t <= times[-1] and len(out) <= MAX_MISSING:  # type: ignore[operator]
            if t.date() not in present:
                out.append(t.date().isoformat())
            t = _step(t, grain)
        return grain, out[:MAX_MISSING]
    ints = [num(p) for p in periods]
    if all(v is not None and float(v).is_integer() for v in ints):
        have = {int(v) for v in ints}  # type: ignore[arg-type]
        return "year", [str(y) for y in range(min(have), max(have) + 1) if y not in have][:MAX_MISSING]
    return None, []


def series_analysis(columns: Sequence[str], rows: Sequence[Sequence[Any]], dimensions: Iterable[str] = ()) -> dict[str, Any] | None:
    """Trend, anomalies and gaps of one series (one row per period, >= 5 points); None otherwise."""
    from analystos.skills.stats import linear_trend, robust_anomalies

    cols = classify(columns, rows, dimensions)
    time_col = next((c for c in cols if c["kind"] == "time"), None)
    measure = next((c for c in cols if c["kind"] == "measure"), None)
    if time_col is None or measure is None:
        return None
    ti, mi = time_col["index"], measure["index"]
    points = [(r[ti], num(r[mi])) for r in rows if r[ti] is not None and num(r[mi]) is not None]
    if len(points) < MIN_SERIES_POINTS or len({str(p) for p, _ in points}) != len(points):
        return None
    points.sort(key=lambda p: (as_time(p[0]) or datetime.min, num(p[0]) or 0, str(p[0])))
    periods = [p for p, _ in points]
    values = [v for _, v in points]
    import numpy as np

    with np.errstate(invalid="ignore", divide="ignore"):  # a perfect line has zero residuals (Durbin-Watson 0/0)
        trend = linear_trend(values, periods)
    slope = float(trend.statistic or 0.0)
    mean = sum(values) / len(values)
    share = slope / abs(mean) if mean else None
    if slope == 0 or (mean and abs(slope) < FLAT_SHARE_OF_MEAN * abs(mean)):
        direction = "flat"
    else:
        direction = "increasing" if slope > 0 else "decreasing"
    anomalies = robust_anomalies(values, [str(p) for p in periods], z_threshold=ANOMALY_Z)
    flagged = [{"period": g["label"], "value": clean(g["value"]), "z": g["robust_z"], "direction": g["direction"]}
               for g in anomalies.groups if g.get("z_outlier")]
    grain, missing = _missing(periods)
    return {"time_column": time_col["name"], "column": measure["name"], "points": len(points), "grain": grain,
            "first_period": str(periods[0]), "last_period": str(periods[-1]), "first_value": clean(values[0]),
            "last_value": clean(values[-1]), "mean": clean(mean), "slope_per_period": clean(slope),
            "slope_share_of_mean": clean(share), "direction": direction, "p_value": trend.p_value,
            "anomalies": flagged, "missing_periods": missing, "warnings": list(anomalies.warnings)}


# ------------------------------------------------------------------------------------ two periods
def _period_columns(cols: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    measures = [c for c in cols if c["kind"] == "measure"]
    prev = [c for c in measures if set(tokens(c["name"])) & _PREV_WORDS and not set(tokens(c["name"])) & _CURR_WORDS]
    curr = [c for c in measures if set(tokens(c["name"])) & _CURR_WORDS and not set(tokens(c["name"])) & _PREV_WORDS]
    return (prev[0], curr[0]) if prev and curr else None


def _period_order(values: list[Any]) -> list[Any]:
    def key(v: Any) -> tuple:
        t = as_time(v)
        toks = set(tokens(str(v)))
        rank = 0 if toks & _PREV_WORDS else 1 if toks & _CURR_WORDS else None
        return (t or datetime.min, num(v) if num(v) is not None else 0, rank if rank is not None else 0, str(v))
    return sorted(values, key=key)


def two_period(columns: Sequence[str], rows: Sequence[Sequence[Any]], dimensions: Iterable[str] = ()) -> dict[str, Any] | None:
    """The change between two periods and, per member, what drove it (see the module doc)."""
    cols = classify(columns, rows, dimensions)
    pair = _period_columns(cols)
    members: dict[str, list[float | None]] = {}
    out: dict[str, Any]
    label_idx = [c["index"] for c in cols if c["kind"] in ("label", "identifier") and c["kind"] != "time"]
    if pair is not None:
        p, c = pair
        if p["pre_aggregated"] or c["pre_aggregated"]:
            return None
        out = {"measure": None, "previous_column": p["name"], "current_column": c["name"]}
        for i, r in enumerate(rows):
            key = row_label(r, label_idx, columns) or f"row {i + 1}"
            members[key] = [num(r[p["index"]]), num(r[c["index"]])]
    else:
        measure = next((c for c in cols if c["kind"] == "measure"), None)
        if measure is None or measure["pre_aggregated"]:
            return None
        period_col = next((c for c in cols if c["kind"] == "time"), None)
        if period_col is None:  # a label column whose two values say previous / current
            period_col = next((c for c in cols if c["kind"] == "label" and len({str(r[c["index"]]) for r in rows}) == 2
                               and all(set(tokens(str(r[c["index"]]))) & (_PREV_WORDS | _CURR_WORDS) for r in rows)), None)
        if period_col is None:
            return None
        values = list({str(r[period_col["index"]]): r[period_col["index"]] for r in rows}.values())
        if len(values) != 2:
            return None
        before, after = _period_order(values)
        label_idx = [c["index"] for c in cols if c["kind"] in ("label", "identifier") and c is not period_col]
        out = {"measure": measure["name"], "period_column": period_col["name"], "previous_period": str(before),
               "current_period": str(after)}
        for r in rows:
            key = row_label(r, label_idx, columns) or "total"
            slot = members.setdefault(key, [None, None])
            v = num(r[measure["index"]])
            k = 0 if str(r[period_col["index"]]) == str(before) else 1
            slot[k] = (slot[k] or 0.0) + v if v is not None else slot[k]
    if not members:
        return None
    prev_total = sum(v[0] or 0.0 for v in members.values())
    curr_total = sum(v[1] or 0.0 for v in members.values())
    change = curr_total - prev_total
    out.update({"previous": clean(prev_total), "current": clean(curr_total), "change": clean(change),
                "pct_change": clean(change / prev_total) if prev_total else None})
    if len(members) < 2 or not label_idx:
        return out
    rows_out = []
    for name, (pv, cv) in members.items():
        d = (cv or 0.0) - (pv or 0.0)
        rows_out.append({"member": name, "previous": clean(pv or 0.0), "current": clean(cv or 0.0), "change": clean(d),
                         "pct_change": clean(d / pv) if pv else None,
                         "share_of_change": clean(d / change) if change else None,
                         "appeared": not pv and bool(cv), "disappeared": bool(pv) and not cv})
    if all(r["change"] == 0 for r in rows_out):
        return out
    rows_out.sort(key=lambda r: (-abs(r["change"]), r["member"]))
    sign = 1 if change > 0 else -1 if change < 0 else 0
    if sign:
        drivers = [r for r in rows_out if r["change"] * sign > 0]
        offsets = [r for r in rows_out if r["change"] * sign < 0]
    else:
        drivers = [r for r in rows_out if r["change"] > 0]
        offsets = [r for r in rows_out if r["change"] < 0]
    out.update({"members": len(rows_out), "drivers": drivers[:MAX_MEMBERS], "offsets": offsets[:MAX_MEMBERS],
                "appeared": [r["member"] for r in rows_out if r["appeared"]][:MAX_MEMBERS],
                "disappeared": [r["member"] for r in rows_out if r["disappeared"]][:MAX_MEMBERS]})
    return out


# ------------------------------------------------------------------------------------ checks
def _check(code: str, ok: bool, note: str) -> dict[str, str]:
    return {"code": code, "status": "pass" if ok else "suspect", "note": note}


def step_checks(kind: str, *, sql: str | None, dialect: str, columns: Sequence[str], rows: Sequence[Sequence[Any]],
                row_count: int | None, truncated: bool, series: Mapping[str, Any] | None = None,
                comparison: Mapping[str, Any] | None = None, history: Sequence[float] = (),
                dimensions: Iterable[str] = ()) -> list[dict[str, str]]:
    """Deterministic checks of one answered step; a suspect check is shown, it never blocks the answer."""
    obs = selfcheck.Observation(sql=sql, dialect=dialect, columns=list(columns), rows=[list(r) for r in rows],
                                row_count=row_count, truncated=truncated, history=list(history))
    out = [_check(r.check, r.passed, r.detail) for r in
           (selfcheck.empty_result(obs), selfcheck.truncation(obs), selfcheck.grouping(obs), selfcheck.magnitude(obs))]
    n = row_count if row_count is not None else len(rows)
    if kind in _BREAKDOWN_KINDS:
        out.append(_check("single_row_breakdown", n != 1, "a breakdown returned a single row: the grouping may be "
                          "missing or a filter too narrow" if n == 1 else "more than one group"))
    problems = []
    for c in classify(columns, rows, dimensions):
        if c["kind"] != "measure" or not set(tokens(c["name"])) & _SHARE_WORDS:
            continue
        vals = [num(r[c["index"]]) for r in rows if num(r[c["index"]]) is not None]
        if any(v < 0 for v in vals):  # type: ignore[operator]
            problems.append(f"{c['name']} has a share below 0%")
        if any(v > 100 for v in vals):  # type: ignore[operator]
            problems.append(f"{c['name']} has a share above 100%")
        elif "share" in tokens(c["name"]) and vals and len(vals) > 1:
            total, scale = sum(vals), (1.0 if max(vals) <= 1 else 100.0)  # type: ignore[type-var,arg-type]
            if total > scale * 1.001:
                problems.append(f"the shares in {c['name']} add up to more than 100%")
    out.append(_check("share_range", not problems, "; ".join(problems) or "shares within 0..100%"))
    if series is not None:
        missing = list(series.get("missing_periods") or [])
        out.append(_check("missing_periods", not missing, f"missing periods in the series: {', '.join(missing)}"
                          if missing else "no gap in the series"))
    if kind in ("comparison", "drivers") and n:
        both = comparison is not None
        out.append(_check("two_periods", both, "both periods present" if both
                          else "the result does not show two periods to compare"))
    return out


def headline_value(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> float | None:
    return selfcheck.headline(list(columns), [list(r) for r in rows])


# ------------------------------------------------------------------------------------ numbers guard
_CITATION = re.compile(r"\(\s*steps?\s+(\d+(?:\s*(?:,|and|&)\s*\d+)*)\s*\)", re.I)
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
_DOWN = re.compile(r"\b(lower|less|fewer|below|decreas\w*|fell|fall\w*|drop\w*|declin\w*|down|reduc\w*|shr[ai]nk\w*|"
                   r"negative|offset\w*|lost|loss)\b", re.I)


def citations(text: str) -> list[int]:
    return sorted({int(n) for m in _CITATION.finditer(text) for n in re.findall(r"\d+", m.group(1))})


def _mask(text: str, labels: Iterable[str]) -> str:
    masked = _CITATION.sub(lambda m: " " * len(m.group(0)), text)
    for label in sorted({str(x) for x in labels if x and any(ch.isdigit() for ch in str(x))}, key=len, reverse=True):
        pre = r"(?<![\w.,])" if label[:1].isalnum() else ""
        post = r"(?![\w%]|[.,]\d)" if label[-1:].isalnum() else ""
        masked = re.sub(pre + re.escape(label) + post, lambda m: "§" * len(m.group(0)), masked, flags=re.I)
    return masked


def numbers_bound(text: str, values: Iterable[float], *, labels: Iterable[str] = (),
                  steps: Iterable[int] | None = None) -> dict[str, Any]:
    """Every number in `text` equals a computed value within its stated precision (a percentage may
    quote a fraction), and every sentence with a number cites a step; labels holding digits (periods,
    members, column names) and the citations themselves are masked first. A number that matches only
    a negative value's magnitude needs a downward word in its sentence."""
    vals = [float(v) for v in values if num(v) is not None]
    negatives = [abs(v) for v in vals if v < 0]
    known = set(steps) if steps is not None else None
    problems: list[str] = []
    count = 0
    labels = list(labels)
    for sentence in (s for s in _SENTENCES.split(text or "") if s.strip()):
        masked = _mask(sentence, labels)
        res = selfcheck.numbers(selfcheck.Observation(kind="claim", text=masked, upstream_values=vals))
        found = res.evidence.get("numbers") or []
        count += len(found)
        if not found:
            continue
        cited = citations(sentence)
        if not cited:
            problems.append(f"'{sentence.strip()[:120]}' quotes a number but cites no step")
        elif known is not None and set(cited) - known:
            problems.append(f"'{sentence.strip()[:120]}' cites a step that has no answer: {sorted(set(cited) - known)}")
        for token in res.evidence.get("unbound") or []:
            flipped = selfcheck.numbers(selfcheck.Observation(kind="claim", text=token.lstrip("+-"), upstream_values=negatives))
            if flipped.passed and _DOWN.search(masked):
                continue
            problems.append(f"{token} matches no computed fact" if not flipped.passed
                            else f"{token} is a decrease, but its sentence does not say so")
    return {"ok": not problems, "problems": problems, "numbers": count}


def fact_values(*parts: Any) -> list[float]:
    """Every number inside facts/series/two-period dicts (and lists of them), for the numbers guard."""
    out: list[float] = []

    def walk(v: Any) -> None:
        if isinstance(v, Mapping):
            for x in v.values():
                walk(x)
        elif isinstance(v, list | tuple):
            for x in v:
                walk(x)
        elif num(v) is not None:
            out.append(float(v))
    for p in parts:
        walk(p)
    return out


def fact_labels(*parts: Any) -> list[str]:
    """Every string inside the same structures (members, periods, labels, column names)."""
    out: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, Mapping):
            for x in v.values():
                walk(x)
        elif isinstance(v, list | tuple):
            for x in v:
                walk(x)
        elif isinstance(v, str):
            out.append(v)
    for p in parts:
        walk(p)
    return out


__all__ = ["as_time", "citations", "classify", "clean", "fact_labels", "fact_values", "fmt_num", "fmt_pct", "grouped_columns",
           "headline_value", "numbers_bound", "pre_aggregated", "series_analysis", "step_checks", "step_facts",
           "two_period"]
