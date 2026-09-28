"""Process and task mining over an event log (deterministic, domain-neutral).

An event log is any table with a case id, an activity and a timestamp (and optionally the resource that
held the case: a team, a queue, a person). Which columns those are is a parameter; nothing here knows a
particular system. Given the ordered events of each case this computes the directly-follows graph (with
median and p90 transition times), the variants, the throughput-time distribution, bottlenecks, rework,
cancellations, conformance against a declared or inferred reference path, and handovers between resources.

Reading: every statement goes through the caller's gateway runner (`run_sql`, the one gateway). The events
are pulled as ordered rows ``(case, activity, timestamp, resource)`` in keyset pages by case id, and the
analytics run in Python. That is deliberate: the directly-follows relation needs "the next event of the
same case", i.e. a window function or a self-join, and window functions / ordered string aggregation are
dialect-specific (and refused by some validator profiles); plain ordered SELECTs are accepted by every
dialect the gateway validates. Each page is capped by the gateway's row cap; a page that hits the cap drops
its last (possibly partial) case and the next page starts at that case, so no case is ever split. The total
is bounded by `max_events`; a log larger than that is analysed on its first cases and says so (`coverage`).
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, NamedTuple

from sqlglot import exp

from analystos.core.errors import InvalidInput
from analystos.skills.catalog import split_tokens
from analystos.skills.sqlbuild import and_all, col, lit, not_null, table, to_sql

VERSION = "process-mining/1"
DEFAULT_PAGE_ROWS = 50_000
DEFAULT_MAX_EVENTS = 250_000
TOP_VARIANTS = 10
TOP_BOTTLENECKS = 5
TOP_HANDOVERS = 20
# Throughput histogram edges in hours (fixed, so two analyses compare bin by bin).
HIST_EDGES = [0.0, 1.0, 4.0, 8.0, 24.0, 72.0, 168.0, 336.0, 720.0, math.inf]
HIST_LABELS = ["< 1 h", "1-4 h", "4-8 h", "8-24 h", "1-3 days", "3-7 days", "1-2 weeks", "2-4 weeks", "> 4 weeks"]
_CANCEL = re.compile(r"cancel|abandon|withdr[ae]w|reject|void|discard", re.I)
FILTER_OPS = ("=", "!=", "in", "not_in")


class Event(NamedTuple):
    case: str
    activity: str
    ts: datetime
    resource: str | None


@dataclass(frozen=True)
class Mapping:
    case: str
    activity: str
    timestamp: str
    resource: str | None = None

    def columns(self) -> list[str]:
        return [self.case, self.activity, self.timestamp] + ([self.resource] if self.resource else [])


# ----------------------------------------------------------------------------------------------- reading
def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    try:
        return _ts(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _filter_expr(f: dict[str, Any], dialect: str) -> exp.Expression:
    op = f.get("op", "=")
    if op not in FILTER_OPS:
        raise InvalidInput(f"filter operator {op!r} is not one of {', '.join(FILTER_OPS)}")
    c = col(str(f["column"]))
    if op in ("in", "not_in"):
        values = f.get("values") if f.get("values") is not None else [f.get("value")]
        if not values:
            raise InvalidInput(f"filter on {f['column']} needs at least one value")
        e: exp.Expression = exp.In(this=c, expressions=[lit(v, dialect) for v in values])
        return exp.Not(this=e) if op == "not_in" else e
    value = f.get("value")
    if value is None:
        return exp.Is(this=c, expression=exp.Null()) if op == "=" else not_null(c)
    return (exp.EQ if op == "=" else exp.NEQ)(this=c, expression=lit(value, dialect))


_KEYSET = {">=": exp.GTE, ">": exp.GT, "=": exp.EQ}


def event_log_sql(asset: str, mapping: Mapping, *, dialect: str = "postgres", filters: Sequence[dict[str, Any]] = (),
                  after_case: Any = None, op: str = ">=") -> str:
    """One page of ordered events ``case, activity, timestamp[, resource]``: the cases `op` `after_case`."""
    cols = [col(c) for c in mapping.columns()]
    conds = [not_null(col(mapping.case)), not_null(col(mapping.activity)), not_null(col(mapping.timestamp))]
    conds += [_filter_expr(f, dialect) for f in filters]
    if after_case is not None:
        conds.append(_KEYSET[op](this=col(mapping.case), expression=lit(after_case, dialect)))
    q = exp.select(*cols).from_(table(asset)).where(and_all(conds))
    q = q.order_by(col(mapping.case), col(mapping.timestamp), col(mapping.activity))
    return to_sql(q, dialect)


def read_event_log(run_sql: Callable[..., Any], asset: str, mapping: Mapping, *, filters: Sequence[dict[str, Any]] = (),
                   page_rows: int = DEFAULT_PAGE_ROWS, max_events: int = DEFAULT_MAX_EVENTS) -> dict[str, Any]:
    """Pull the log through the gateway in keyset pages by case (module docstring). Returns
    ``{"rows", "queries", "sql", "coverage"}``; rows are ``(case, activity, timestamp, resource)``.

    A page that hits the cap ends inside its last case: that case is dropped and the next page starts at it
    (``>=``). A page holding a single case is re-read as that case alone (``=``), then paging continues after
    it (``>``); only a case longer than a whole page is cut (``split_case``)."""
    dialect = getattr(run_sql, "dialect", "postgres")
    rows: list[tuple[Any, ...]] = []
    queries: list[str] = []
    sqls: list[str] = []
    after, op = None, ">="
    truncated, split_case = False, False

    def page(after_case: Any, cmp: str) -> Any:
        sql = event_log_sql(asset, mapping, dialect=dialect, filters=filters, after_case=after_case, op=cmp)
        sqls.append(sql)
        res = run_sql(sql, purpose="process_mining.events", max_rows=page_rows)
        queries.append(res.query_id)
        return res

    while True:
        res = page(after, op)
        got = [tuple(r) for r in res.rows]
        if not res.truncated:
            rows.extend(got)
            break
        last = got[-1][0] if got else None
        complete = [r for r in got if r[0] != last]
        if complete:
            rows.extend(complete)
            after, op = last, ">="
        else:
            alone = page(last, "=")
            rows.extend(tuple(r) for r in alone.rows)
            split_case = split_case or bool(alone.truncated)
            after, op = last, ">"
        if len(rows) >= max_events:
            truncated = True
            break
    if len(rows) > max_events:
        cut = rows[max_events][0]
        rows = [r for r in rows[:max_events] if r[0] != cut] or rows[:max_events]
    truncated = truncated or split_case
    first_sql = sqls[0]
    return {"rows": rows, "queries": queries, "sql": first_sql,
            "coverage": {"events_read": len(rows), "pages": len(queries), "page_rows": page_rows, "max_events": max_events,
                         "truncated": truncated, "split_case": split_case,
                         "note": "the first cases in case-id order up to the event bound" if truncated else "every case"}}


def cases_from_rows(rows: Iterable[Sequence[Any]]) -> dict[str, list[Event]]:
    """Group rows ``(case, activity, timestamp[, resource])`` by case, each ordered by time (ties keep row order).
    Rows without a case, activity or parseable time are skipped."""
    out: dict[str, list[tuple[datetime, int, Event]]] = defaultdict(list)
    for i, r in enumerate(rows):
        case, activity, ts = r[0], r[1], _ts(r[2])
        if case is None or activity is None or ts is None:
            continue
        resource = r[3] if len(r) > 3 and r[3] not in (None, "") else None
        e = Event(str(case), str(activity), ts, None if resource is None else str(resource))
        out[e.case].append((ts, i, e))
    return {c: [e for *_, e in sorted(evs, key=lambda x: (x[0], x[1]))] for c, evs in out.items()}


# ------------------------------------------------------------------------------------------- statistics
def quantile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated quantile (numpy's default) of unsorted values; None when empty."""
    if not values:
        return None
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def _hours(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 3600.0


def fmt_hours(h: float | None) -> str:
    """Plain duration: minutes under an hour, hours under two days, else days."""
    if h is None:
        return "n/a"
    if h < 1:
        return f"{h * 60:.0f} min"
    if h < 48:
        return f"{h:.1f} h"
    return f"{h / 24:.1f} days"


def _share(n: int, d: int) -> float:
    return round(n / d, 4) if d else 0.0


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%" if x >= 0.01 or x == 0 else f"{x * 100:.1f}%"


def activity_stats(cases: dict[str, list[Event]]) -> list[dict[str, Any]]:
    events: Counter[str] = Counter()
    in_cases: Counter[str] = Counter()
    starts: Counter[str] = Counter()
    ends: Counter[str] = Counter()
    for evs in cases.values():
        events.update(e.activity for e in evs)
        in_cases.update({e.activity for e in evs})
        starts[evs[0].activity] += 1
        ends[evs[-1].activity] += 1
    return [{"activity": a, "events": n, "cases": in_cases[a], "starts": starts[a], "ends": ends[a]}
            for a, n in sorted(events.items(), key=lambda kv: (-kv[1], kv[0]))]


def directly_follows(cases: dict[str, list[Event]]) -> list[dict[str, Any]]:
    """Edges a -> b where b is the next event of the same case: count, cases, median/p90/mean hours."""
    waits: dict[tuple[str, str], list[float]] = defaultdict(list)
    in_cases: Counter[tuple[str, str]] = Counter()
    for evs in cases.values():
        seen = set()
        for a, b in zip(evs, evs[1:], strict=False):
            key = (a.activity, b.activity)
            waits[key].append(_hours(a.ts, b.ts))
            seen.add(key)
        in_cases.update(seen)
    edges = [{"source": s, "target": t, "count": len(w), "cases": in_cases[(s, t)], "median_hours": _r(quantile(w, 0.5)),
              "p90_hours": _r(quantile(w, 0.9)), "mean_hours": _r(sum(w) / len(w))}
             for (s, t), w in waits.items()]
    return sorted(edges, key=lambda e: (-e["count"], e["source"], e["target"]))


def _throughput(evs: list[Event]) -> float:
    return _hours(evs[0].ts, evs[-1].ts)


def variants(cases: dict[str, list[Event]], *, top: int = TOP_VARIANTS, happy_path: Sequence[str] | None = None
             ) -> dict[str, Any]:
    by: dict[tuple[str, ...], list[float]] = defaultdict(list)
    for evs in cases.values():
        by[tuple(e.activity for e in evs)].append(_throughput(evs))
    n = len(cases)
    ranked = sorted(by.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    hp = tuple(happy_path) if happy_path else None
    rows = [{"rank": i + 1, "activities": list(v), "steps": len(v), "cases": len(d), "share": _share(len(d), n),
             "median_hours": _r(quantile(d, 0.5)), "happy_path": v == hp}
            for i, (v, d) in enumerate(ranked[:top])]
    shown = sum(r["cases"] for r in rows)
    return {"total": len(by), "top": rows, "other_cases": n - shown, "other_share": _share(n - shown, n),
            "happy_path_rank": next((i + 1 for i, (v, _) in enumerate(ranked) if v == hp), None) if hp else None}


def throughput(cases: dict[str, list[Event]]) -> dict[str, Any]:
    d = [_throughput(evs) for evs in cases.values()]
    bins = [0] * len(HIST_LABELS)
    for h in d:
        for i in range(len(HIST_LABELS)):
            if HIST_EDGES[i] <= h < HIST_EDGES[i + 1]:
                bins[i] += 1
                break
    by_end: dict[str, list[float]] = defaultdict(list)
    for evs in cases.values():
        by_end[evs[-1].activity].append(_throughput(evs))
    return {
        "cases": len(d), "median_hours": _r(quantile(d, 0.5)), "p90_hours": _r(quantile(d, 0.9)),
        "mean_hours": _r(sum(d) / len(d)) if d else None, "min_hours": _r(min(d)) if d else None,
        "max_hours": _r(max(d)) if d else None,
        "histogram": [{"label": lab, "from_hours": HIST_EDGES[i], "to_hours": None if math.isinf(HIST_EDGES[i + 1]) else
                       HIST_EDGES[i + 1], "cases": bins[i]} for i, lab in enumerate(HIST_LABELS)],
        "by_end_activity": [{"activity": a, "cases": len(v), "median_hours": _r(quantile(v, 0.5)),
                             "p90_hours": _r(quantile(v, 0.9))}
                            for a, v in sorted(by_end.items(), key=lambda kv: (-len(kv[1]), kv[0]))],
    }


def bottlenecks(edges: list[dict[str, Any]], n_cases: int, *, top: int = TOP_BOTTLENECKS,
                min_cases: int | None = None) -> list[dict[str, Any]]:
    """The transitions that cost the most waiting in total: median wait x number of transitions, among those
    seen in at least `min_cases` cases (default 0.5% of cases, at least 1), self-loops included."""
    floor = min_cases if min_cases is not None else max(1, round(n_cases * 0.005))
    ranked = sorted((e for e in edges if e["cases"] >= floor and (e["median_hours"] or 0) > 0),
                    key=lambda e: (-(e["median_hours"] * e["count"]), -e["median_hours"], e["source"], e["target"]))
    return [{**{k: e[k] for k in ("source", "target", "count", "cases", "median_hours", "p90_hours")},
             "weight_hours": _r(e["median_hours"] * e["count"], 1),
             "sentence": f"{e['source']} → {e['target']} takes a median {fmt_hours(e['median_hours'])} "
                         f"(1 in 10 over {fmt_hours(e['p90_hours'])}) for {e['cases']:,} cases."}
            for e in ranked[:top]]


def rework(cases: dict[str, list[Event]]) -> dict[str, Any]:
    """Activities that happen more than once in a case (a loop back to an earlier step)."""
    repeated: Counter[str] = Counter()
    extra: Counter[str] = Counter()
    twice_or_more: Counter[str] = Counter()
    rework_cases = 0
    for evs in cases.values():
        counts = Counter(e.activity for e in evs)
        rep = {a: c for a, c in counts.items() if c > 1}
        if rep:
            rework_cases += 1
        for a, c in rep.items():
            repeated[a] += 1
            extra[a] += c - 1
        for a, c in counts.items():
            if c >= 2:
                twice_or_more[a] += 1
    n = len(cases)
    acts = [{"activity": a, "cases": c, "share": _share(c, n), "extra_events": extra[a]}
            for a, c in sorted(repeated.items(), key=lambda kv: (-kv[1], kv[0]))]
    return {"cases": rework_cases, "share": _share(rework_cases, n), "activities": acts}


def cancel_activities(activities: Iterable[str]) -> list[str]:
    """Activity names that read as a cancellation / abandonment (any language-neutral stem match)."""
    return sorted({a for a in activities if _CANCEL.search(a)})


def cancellations(cases: dict[str, list[Event]], cancel: Sequence[str]) -> dict[str, Any]:
    cancel_set = set(cancel)
    n = len(cases)
    after: Counter[str] = Counter()
    by_resource: Counter[str] = Counter()
    times: list[float] = []
    count = 0
    for evs in cases.values():
        idx = next((i for i, e in enumerate(evs) if e.activity in cancel_set), None)
        if idx is None:
            continue
        count += 1
        after[evs[idx - 1].activity if idx > 0 else "(start)"] += 1
        if evs[idx].resource:
            by_resource[evs[idx].resource] += 1
        times.append(_hours(evs[0].ts, evs[idx].ts))
    return {"activities": list(cancel), "cases": count, "share": _share(count, n),
            "median_hours_to_cancel": _r(quantile(times, 0.5)),
            "after": [{"activity": a, "cases": c, "share": _share(c, count)}
                      for a, c in sorted(after.items(), key=lambda kv: (-kv[1], kv[0]))],
            "by_resource": [{"resource": r, "cases": c}
                            for r, c in sorted(by_resource.items(), key=lambda kv: (-kv[1], kv[0]))[:10]]}


def trace_deviations(trace: Sequence[str], reference: Sequence[str]) -> dict[str, list[str]]:
    """Missing reference steps, extra (non-reference) steps, and reference steps whose first occurrence comes
    before a step that precedes them in the reference."""
    present = set(trace)
    first = {}
    for i, a in enumerate(trace):
        first.setdefault(a, i)
    pos = {a: i for i, a in enumerate(reference)}
    missing = [a for a in reference if a not in present]
    extra = list(dict.fromkeys(a for a in trace if a not in pos))
    out_of_order = [a for a in reference if a in present and any(
        b in present and pos[b] < pos[a] and first[a] < first[b] for b in reference)]
    return {"missing": missing, "extra": extra, "out_of_order": out_of_order}


def conformance(cases: dict[str, list[Event]], reference: Sequence[str], *, source: str,
                cancel: Sequence[str] = ()) -> dict[str, Any]:
    """Completed cases (last step = the reference's last step) checked against the reference path; open
    and cancelled cases are counted and left out (a case still running is not a deviation)."""
    reference = list(reference)
    cancel_set = set(cancel)
    by_variant: Counter[tuple[str, ...]] = Counter(tuple(e.activity for e in evs) for evs in cases.values())
    completed = cancelled = still_open = conforming = 0
    dev: Counter[tuple[str, str]] = Counter()
    deviating_variants: list[tuple[int, tuple[str, ...], dict[str, list[str]]]] = []
    for trace, n in by_variant.items():
        if reference and trace[-1] == reference[-1]:
            completed += n
            d = trace_deviations(trace, reference)
            if not any(d.values()):
                conforming += n
            else:
                deviating_variants.append((n, trace, d))
            for kind, acts in d.items():
                for a in acts:
                    dev[(kind, a)] += n
        elif cancel_set & set(trace):
            cancelled += n
        else:
            still_open += n
    words = {"missing": "skipped", "extra": "outside the reference path", "out_of_order": "out of order"}
    deviations = [{"kind": k, "activity": a, "cases": c, "share": _share(c, completed),
                   "sentence": f"'{a}' {words[k]} in {c:,} completed cases ({_pct(_share(c, completed))})."}
                  for (k, a), c in sorted(dev.items(), key=lambda kv: (-kv[1], kv[0]))]
    top_dev = sorted(deviating_variants, key=lambda x: (-x[0], x[1]))[:5]
    return {"reference": reference, "source": source, "completed_cases": completed, "conforming_cases": conforming,
            "fitness": _share(conforming, completed), "excluded": {"open": still_open, "cancelled": cancelled},
            "deviations": deviations,
            "deviating_variants": [{"activities": list(t), "cases": n, **d} for n, t, d in top_dev]}


def handovers(cases: dict[str, list[Event]], *, top: int = TOP_HANDOVERS) -> dict[str, Any]:
    """Resource changes between consecutive events of a case, resources' load, and ping-pong (A -> B -> A)."""
    pairs: Counter[tuple[str, str]] = Counter()
    pair_cases: Counter[tuple[str, str]] = Counter()
    ping: Counter[tuple[str, str]] = Counter()
    events: Counter[str] = Counter()
    res_cases: Counter[str] = Counter()
    out_: Counter[str] = Counter()
    in_: Counter[str] = Counter()
    with_handover = 0
    for evs in cases.values():
        seq = [e.resource for e in evs if e.resource]
        events.update(seq)
        res_cases.update(set(seq))
        changes = [(a, b) for a, b in zip(seq, seq[1:], strict=False) if a != b]
        if changes:
            with_handover += 1
        pairs.update(changes)
        pair_cases.update(set(changes))
        for a, b in changes:
            out_[a] += 1
            in_[b] += 1
        runs = [r for i, r in enumerate(seq) if i == 0 or r != seq[i - 1]]
        pp = {tuple(sorted((runs[i], runs[i + 1]))) for i in range(len(runs) - 2) if runs[i] == runs[i + 2]}
        ping.update(pp)  # type: ignore[arg-type]
    n = len(cases)
    return {
        "cases_with_handover": with_handover, "share": _share(with_handover, n),
        "pairs": [{"source": a, "target": b, "count": c, "cases": pair_cases[(a, b)]}
                  for (a, b), c in sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0]))[:top]],
        "ping_pong": [{"a": a, "b": b, "cases": c}
                      for (a, b), c in sorted(ping.items(), key=lambda kv: (-kv[1], kv[0]))[:10]],
        "resources": [{"resource": r, "events": c, "cases": res_cases[r], "handovers_out": out_[r], "handovers_in": in_[r]}
                      for r, c in sorted(events.items(), key=lambda kv: (-kv[1], kv[0]))[:top]],
    }


# ---------------------------------------------------------------------------------------------- analysis
def analyze_cases(cases: dict[str, list[Event]], *, reference_path: Sequence[str] | None = None,
                  reference_source: str | None = None, cancel: Sequence[str] | None = None,
                  top_variants: int = TOP_VARIANTS) -> dict[str, Any]:
    """Every analytic of the module over grouped cases. Without a declared reference path the most frequent
    variant is the inferred happy path."""
    if not cases:
        raise InvalidInput("the event log has no case with a case id, an activity and a time")
    acts = activity_stats(cases)
    cancel_list = list(cancel) if cancel is not None else cancel_activities(a["activity"] for a in acts)
    all_variants = variants(cases, top=1)
    inferred = all_variants["top"][0]["activities"] if all_variants["top"] else []
    reference = list(reference_path) if reference_path else inferred
    source = reference_source or ("declared" if reference_path else "inferred: the most frequent variant")
    edges = directly_follows(cases)
    n = len(cases)
    n_events = sum(len(v) for v in cases.values())
    tp = throughput(cases)
    var = variants(cases, top=top_variants, happy_path=reference)
    bn = bottlenecks(edges, n)
    rw = rework(cases)
    cn = cancellations(cases, cancel_list)
    conf = conformance(cases, reference, source=source, cancel=cancel_list)
    ho = handovers(cases)
    starts = [e[0].ts for e in cases.values()]
    ends = [e[-1].ts for e in cases.values()]
    result = {
        "version": VERSION,
        "summary": {"cases": n, "events": n_events, "activities": len(acts), "variants": var["total"],
                    "mean_events_per_case": _r(n_events / n), "start": min(starts).isoformat(),
                    "end": max(ends).isoformat(), "median_hours": tp["median_hours"], "p90_hours": tp["p90_hours"],
                    "rework_share": rw["share"], "cancelled_share": cn["share"], "fitness": conf["fitness"],
                    "handover_share": ho["share"]},
        "activities": acts, "edges": edges, "variants": var, "throughput": tp, "bottlenecks": bn, "rework": rw,
        "cancellations": cn, "conformance": conf, "handovers": ho,
    }
    result["highlights"] = highlights(result)
    return result


def highlights(a: dict[str, Any]) -> list[str]:
    """The analysis in a few plain sentences, every number taken from the analysis itself."""
    s, out = a["summary"], []
    top = a["variants"]["top"][0] if a["variants"]["top"] else None
    out.append(f"{s['cases']:,} cases and {s['events']:,} events follow {s['variants']:,} different paths"
               + (f"; the most common path covers {_pct(top['share'])} of cases." if top else "."))
    tp = a["throughput"]
    out.append(f"Half the cases run from first to last step within {fmt_hours(tp['median_hours'])}; "
               f"1 in 10 takes longer than {fmt_hours(tp['p90_hours'])}.")
    if a["bottlenecks"]:
        out.append("Biggest wait: " + a["bottlenecks"][0]["sentence"])
    rw = a["rework"]
    if rw["activities"]:
        r0 = rw["activities"][0]
        out.append(f"Rework in {_pct(rw['share'])} of cases; '{r0['activity']}' repeats in {r0['cases']:,} cases.")
    cn = a["cancellations"]
    if cn["cases"]:
        where = f", most often after '{cn['after'][0]['activity']}'" if cn["after"] else ""
        out.append(f"{cn['cases']:,} cases ({_pct(cn['share'])}) were cancelled{where}.")
    cf = a["conformance"]
    if cf["completed_cases"]:
        line = f"{_pct(cf['fitness'])} of {cf['completed_cases']:,} completed cases follow the reference path exactly"
        missing = next((d for d in cf["deviations"] if d["kind"] == "missing"), None)
        out.append(line + (f"; '{missing['activity']}' is skipped in {missing['cases']:,} of them." if missing else "."))
    ho = a["handovers"]
    if ho["pairs"]:
        p = ho["pairs"][0]
        out.append(f"Most frequent handover: {p['source']} → {p['target']} ({p['count']:,} times).")
    if ho["ping_pong"]:
        p = ho["ping_pong"][0]
        out.append(f"Ping-pong between {p['a']} and {p['b']} in {p['cases']:,} cases.")
    return out


# --------------------------------------------------------------------------------------------- detection
MODELS_FILE = "process_models.yaml"


def declared_event_logs() -> list[dict[str, Any]]:
    """Event logs a domain pack declares (`<pack>/process_models.yaml`: table, mapping, segment column and a
    reference path per segment). Domain content stays in the pack; this only reads the convention."""
    import yaml

    from analystos.capabilities import packs

    out: list[dict[str, Any]] = []
    for p in packs.installed():
        path = p.root / MODELS_FILE if p.root else None
        if path is None or not path.is_file():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for e in data.get("event_logs") or []:
            if isinstance(e, dict) and e.get("table") and isinstance(e.get("mapping"), dict):
                out.append({**e, "pack": p.ref})
    return out


def declared_for(declared: Iterable[dict[str, Any]], names: Iterable[str]) -> dict[str, Any] | None:
    lowered = {n.lower() for n in names if n}
    return next((d for d in declared if str(d["table"]).lower() in lowered), None)


_CASE_TOKENS = {"case", "ticket", "task", "order", "request", "issue", "claim", "application", "process",
                "instance", "document", "parent", "record", "session", "trace"}
_ID_TOKENS = {"id", "number", "no", "num", "key", "ref", "code", "uuid", "guid"}
_ACTIVITY_STRONG = {"activity", "event", "action", "step", "operation", "transition", "task", "milestone"}
_ACTIVITY_WEAK = {"status", "state", "stage", "phase"}
_BEFORE = {"before", "from", "previous", "prev", "old", "prior", "source"}
_TIME_STRONG = {"activity", "event", "action", "step", "occurred", "happened", "performed", "logged", "recorded"}
_TIME_WEAK = {"at", "time", "timestamp", "ts", "date", "datetime", "on", "when"}
_TIME_BAD = {"updated", "modified", "loaded", "ingested", "extracted", "sys", "etl", "valid", "due", "planned"}
_RESOURCE_TEAM = {"group", "team", "queue", "department", "unit", "desk", "org", "organization", "lane"}
_RESOURCE_PERSON = {"resource", "user", "agent", "owner", "assignee", "assigned", "performer", "handler", "operator",
                    "worker", "by"}
_SEGMENT = {"type", "kind", "category", "class", "process", "flow", "channel"}
_LOG_NAME = {"log", "logs", "event", "events", "activity", "activities", "history", "audit", "trail", "journal", "trace"}
_TIME_TYPES = ("timestamp", "datetime", "date", "time")
_TEXT_TYPES = ("text", "varchar", "char", "string", "nvarchar", "character")


def _is_time(dt: str) -> bool:
    return any(t in dt.lower() for t in _TIME_TYPES)


def _is_text(dt: str) -> bool:
    return any(t in dt.lower() for t in _TEXT_TYPES)


def _distinct(c: dict[str, Any]) -> int | None:
    v = (c.get("profile") or {}).get("distinct")
    return int(v) if isinstance(v, int | float) else None


def detect_event_log(asset: dict[str, Any]) -> dict[str, Any] | None:
    """Suggested mapping for one asset, or None when it has no case / activity / time column.

    `asset`: {"name", "role"?, "row_count"?, "columns": [{"name", "data_type", "is_key"?, "tags"?, "profile"?,
    "semantic_role"?}]}. Deterministic: column names, types, the crawler's table role and profile cardinality."""
    cols = asset.get("columns") or []
    rows = asset.get("row_count") or None
    name_tokens = set(split_tokens(asset.get("name") or ""))
    reasons: list[str] = []

    def best(scored: list[tuple[float, str]]) -> tuple[float, str] | None:
        scored = [x for x in scored if x[0] > 0]
        return sorted(scored, key=lambda x: (-x[0], x[1]))[0] if scored else None

    times = []
    for c in cols:
        if not _is_time(str(c.get("data_type", ""))):
            continue
        toks = set(split_tokens(c["name"]))
        score = 1.0 + 3 * bool(toks & _TIME_STRONG) + 1 * bool(toks & _TIME_WEAK) - 2 * bool(toks & _TIME_BAD)
        times.append((max(score, 0.1), c["name"]))
    t = best(times)
    if t is None:
        return None

    # A status/state column names a step only in a table that reads as a log; in an entity table it is the
    # record's current state (one row per case), not a history.
    log_like = asset.get("role") in ("event", "audit") or bool(name_tokens & _LOG_NAME)
    acts = []
    for c in cols:
        if not _is_text(str(c.get("data_type", ""))) or c["name"] == t[1]:
            continue
        toks = set(split_tokens(c["name"]))
        if toks & _BEFORE or (toks & _ID_TOKENS and not toks & _ACTIVITY_STRONG):
            continue
        score = 3 * bool(toks & _ACTIVITY_STRONG) + (1 if log_like and toks & _ACTIVITY_WEAK else 0)
        d = _distinct(c)
        if d is not None and score > 0:  # cardinality confirms a named step column; it never makes one
            score += 1 if 2 <= d <= 200 else -3
        if score > 0:
            acts.append((float(score), c["name"]))
    a = best(acts)
    if a is None:
        return None

    cases = []
    for c in cols:
        if c["name"] in (t[1], a[1]):
            continue
        toks = split_tokens(c["name"])
        tset = set(toks)
        if not (tset & _ID_TOKENS or tset & _CASE_TOKENS):
            continue
        score = 1.0 + 2 * bool(tset & _CASE_TOKENS) + (2 if toks and toks[-1] == "id" else 1 if toks and toks[-1] in _ID_TOKENS else 0)
        if c.get("is_key"):
            score -= 4  # the event's own key identifies an event, not a case
        if set(c.get("tags") or []) & {"pii", "restricted", "sensitive"}:
            score -= 3  # a person is not a case
        d = _distinct(c)
        if d is not None and rows:
            ratio = d / rows
            score += 2 if ratio < 0.9 else -4
        cases.append((score, c["name"]))
    k = best(cases)
    if k is None:
        return None

    resources = []
    for c in cols:
        if c["name"] in (t[1], a[1], k[1]) or not _is_text(str(c.get("data_type", ""))):
            continue
        toks = set(split_tokens(c["name"]))
        if toks & _BEFORE:
            continue
        pii = bool(set(c.get("tags") or []) & {"pii", "restricted", "sensitive"})
        score = 2 * bool(toks & _RESOURCE_TEAM) + 1 * bool(toks & _RESOURCE_PERSON) - (3 if pii else 0)
        if score > 0:
            resources.append((float(score), c["name"]))
    r = best(resources)

    segments = []
    for c in cols:
        if c["name"] in (t[1], a[1], k[1], r[1] if r else None) or not _is_text(str(c.get("data_type", ""))):
            continue
        toks = set(split_tokens(c["name"]))
        d = _distinct(c)
        if toks & _SEGMENT and (d is None or 2 <= d <= 12) and not toks & _ID_TOKENS:
            top = (c.get("profile") or {}).get("top_values") or []
            segments.append({"column": c["name"], "values": [{"value": v.get("value"), "count": v.get("count")}
                                                             for v in top if v.get("value") is not None][:12]})

    score = t[0] + a[0] + k[0] + (r[0] if r else 0)
    if asset.get("role") == "event":
        score += 3
        reasons.append("the crawler classified the table as an event table")
    if name_tokens & _LOG_NAME:
        score += 1
        reasons.append("the table name reads as a log")
    reasons += [f"{k[1]} identifies the case", f"{a[1]} names the activity", f"{t[1]} orders the steps"]
    if r:
        reasons.append(f"{r[1]} holds the case (resource)")
    return {"mapping": {"case_column": k[1], "activity_column": a[1], "timestamp_column": t[1],
                        "resource_column": r[1] if r else None},
            "segments": segments, "score": round(score, 2), "reasons": reasons}


# ----------------------------------------------------------------------------------------------- derived tables
# The event log as two ordinary tables, so every other part of the platform (Ask, step-by-step answers, investigations
# and their agents, metrics, monitors, schedules, dashboards) works on process data with no process-specific code:
# one row per case and one row per step-to-step transition. Same definitions as the analysis above.
CASE_TABLE_COLUMNS = ["case_id", "segment", "started_at", "ended_at", "duration_hours", "steps", "status", "path",
                      "path_rank", "first_activity", "last_activity", "follows_expected_path", "skipped_steps",
                      "steps_out_of_order", "repeated_steps", "rework", "cancelled", "first_resource", "last_resource",
                      "resources", "handovers"]
TRANSITION_TABLE_COLUMNS = ["case_id", "segment", "step", "from_activity", "to_activity", "transition", "from_at", "to_at",
                            "wait_hours", "from_resource", "to_resource", "handover"]


def case_rows(cases: dict[str, list[Event]], *, reference: Sequence[str], cancel: Sequence[str],
              segment: str | None = None) -> list[dict[str, Any]]:
    """One row per case. `status` is completed (last step = the reference's last step), cancelled or open;
    `follows_expected_path` is only set for completed cases, as in `conformance`."""
    reference = list(reference)
    cancel_set = set(cancel)
    rank = {v: i + 1 for i, (v, _) in enumerate(sorted(Counter(tuple(e.activity for e in evs) for evs in cases.values()).items(),
                                                       key=lambda kv: (-kv[1], kv[0])))}
    out = []
    for case_id in sorted(cases):
        evs = cases[case_id]
        trace = [e.activity for e in evs]
        counts = Counter(trace)
        completed = bool(reference) and trace[-1] == reference[-1]
        cancelled = bool(cancel_set & set(trace))
        dev = trace_deviations(trace, reference) if completed else None
        resources = [e.resource for e in evs if e.resource]
        repeated = sum(c - 1 for c in counts.values() if c > 1)
        out.append({
            "case_id": str(case_id), "segment": segment, "started_at": evs[0].ts, "ended_at": evs[-1].ts,
            "duration_hours": _r(_throughput(evs)), "steps": len(evs),
            "status": "completed" if completed else "cancelled" if cancelled else "open",
            "path": " → ".join(trace), "path_rank": rank[tuple(trace)], "first_activity": trace[0], "last_activity": trace[-1],
            "follows_expected_path": (not any(dev.values())) if dev is not None else None,
            "skipped_steps": ", ".join(dev["missing"]) or None if dev is not None else None,
            "steps_out_of_order": ", ".join(dev["out_of_order"]) or None if dev is not None else None,
            "repeated_steps": repeated, "rework": repeated > 0, "cancelled": cancelled,
            "first_resource": resources[0] if resources else None, "last_resource": resources[-1] if resources else None,
            "resources": len(set(resources)),
            "handovers": sum(1 for a, b in zip(evs, evs[1:], strict=False) if a.resource and b.resource and a.resource != b.resource),
        })
    return out


def transition_rows(cases: dict[str, list[Event]], *, segment: str | None = None) -> list[dict[str, Any]]:
    """One row per consecutive pair of events in a case, with the wait between them and whether the work
    changed hands."""
    out = []
    for case_id in sorted(cases):
        evs = cases[case_id]
        for i, (a, b) in enumerate(zip(evs, evs[1:], strict=False), start=1):
            out.append({
                "case_id": str(case_id), "segment": segment, "step": i, "from_activity": a.activity, "to_activity": b.activity,
                "transition": f"{a.activity} → {b.activity}", "from_at": a.ts, "to_at": b.ts, "wait_hours": _r(_hours(a.ts, b.ts)),
                "from_resource": a.resource, "to_resource": b.resource,
                "handover": bool(a.resource and b.resource and a.resource != b.resource),
            })
    return out


TABLE_DESCRIPTIONS = {
    "cases": ("Process cases: one row per case of the event log {log} (a case is every step sharing one case id), with "
              "when it started and ended, its duration in hours, its path of steps, whether it followed the expected path, "
              "rework, cancellation and how often the work changed hands. Derived by the platform from the event log."),
    "transitions": ("Process transitions: one row per step-to-step move in a case of the event log {log}, with the wait in "
                    "hours between the two steps and whether the work changed hands. Derived by the platform from the event log."),
}
COLUMN_DESCRIPTIONS = {
    "case_id": "The case: every event with this id is one case.",
    "segment": "The kind of case (the event log's segment value, e.g. a task type); empty when the log has one kind.",
    "started_at": "Time of the case's first step.", "ended_at": "Time of the case's last step so far.",
    "duration_hours": "Hours from the case's first step to its last step (for an open case, so far).",
    "steps": "Number of steps (events) in the case.",
    "status": "completed (reached the expected path's last step), cancelled, or open.",
    "path": "The case's steps in order, joined by arrows.",
    "path_rank": "1 = the most common path of its kind, 2 = the next, and so on.",
    "first_activity": "The case's first step.", "last_activity": "The case's last step so far.",
    "follows_expected_path": "For a completed case: true when it followed the expected path exactly (no step skipped, "
                             "added or out of order); empty for open or cancelled cases.",
    "skipped_steps": "For a completed case: expected steps it skipped.",
    "steps_out_of_order": "For a completed case: expected steps it took out of order.",
    "repeated_steps": "How many steps repeat an earlier step of the case (rework loops, e.g. a second reassignment).",
    "rework": "True when any step repeats.", "cancelled": "True when the case has a cancellation step.",
    "first_resource": "Who (the team or person) did the case's first step.", "last_resource": "Who did the case's last step.",
    "resources": "How many different teams or people worked on the case.",
    "handovers": "How many times the work changed hands between consecutive steps.",
    "step": "The position of this move in its case (1 = first to second step).",
    "from_activity": "The step the move starts from.", "to_activity": "The step the move goes to.",
    "transition": "The move, as 'from step → to step'.", "from_at": "Time of the from step.", "to_at": "Time of the to step.",
    "wait_hours": "Hours between the two steps.", "from_resource": "Who did the from step.", "to_resource": "Who did the to step.",
    "handover": "True when the two steps were done by different teams or people.",
}
