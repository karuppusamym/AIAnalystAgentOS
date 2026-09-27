"""Analyst mode for Ask: plan a question into at most four governed sub-questions, run each through the
single-question Ask ladder, compute facts in code, self-check every step and synthesize a cited answer.

Nothing here executes SQL or decides access. Each step is one call of the single-question Ask
(`agents.sql_agent.ask`, injected as `ask_fn`): semantic compile -> verified registry -> rules ->
generation -> gateway. What this module adds is deterministic:

1. **Plan** (`plan_rules`): the question's shape decides the steps. Breakdown ("by X", "per X", "for each
   X"; "by X and Y" = a total plus one step per dimension), trend, ranking, and comparison/change
   (a two-period step plus a driver breakdown over the most relevant categorical dimensions of the
   catalog). One step whenever one query answers it. The `analyst_planning` model purpose is asked only
   when the rules produce one step for a question that looks compound; its plan is validated (1..4
   steps, non-empty questions, deduplicated) or the rules plan stands. Avoided calls are recorded.
2. **Run**: steps in order; a failed step keeps its refusal and the others still run; a step the ladder
   cannot answer without more detail turns the whole turn into `clarify` (with an assumption when one is
   obvious); a used-up Ask budget skips the remaining steps.
3. **Facts, series, drivers, checks** (`skills/result_facts.py`), never by a model.
4. **Synthesis**: a template from the facts, each evidence line citing its step. The `analyst_synthesis`
   purpose (off unless an administrator sets it to `always`) may write the prose from the facts only;
   its text is used only when every number binds to a computed value and every sentence with a number
   cites a step (`numbers_bound`); otherwise the template is used and the rejection recorded.
5. **Follow-ups**: up to three next questions from the plan shape and the catalog.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from analystos.core.errors import AnalystOSError, InvalidInput
from analystos.core.logging import get_logger
from analystos.skills import result_facts as rf

log = get_logger(__name__)

MAX_STEPS = 4
STEP_ROWS = 50  # rows kept per step in the persisted analysis (the true row count is kept too)
MODEL_ROWS = 12  # rows per step a synthesis model may see, and only when samples may leave the platform
MAX_FOLLOW_UPS = 3
PLANNING_PURPOSE = "analyst_planning"
SYNTHESIS_PURPOSE = "analyst_synthesis"
KINDS = ("question", "total", "breakdown", "trend", "ranking", "comparison", "drivers")
DEFAULT_COMPARISON = ("the latest month", "the previous month")
TREND_ASSUMPTION = "in the last 12 months"

# ------------------------------------------------------------------------------ question shapes
_MONTHS = (r"january|february|march|april|may|june|july|august|september|october|november|december|"
           r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
_PERIOD = (r"(?:(?:this|current|last|previous|prior|the previous|the last|the current) (?:day|week|month|quarter|year)"
           r"|today|yesterday|q[1-4](?: \d{4})?|(?:" + _MONTHS + r")(?: \d{4})?|\d{4}(?:-\d{2})?"
           r"|(?:the )?(?:day|week|month|quarter|year) before)")
_PERIOD_RE = re.compile(r"^(?:(?:in|during|for|over) )?(?:the )?(" + _PERIOD + r")$")
_TRAILING_PERIOD = re.compile(r"(?:\s+(?:in|during|for|over))?\s+(?:the\s+)?(" + _PERIOD + r")\s*$")
_LEAD = re.compile(r"^(?:(?:please|can you|could you|show me|show|give me|tell me|what is|what's|whats|what are|"
                   r"what were|what was|list|i want|i need|let me see|how many|how much|number of|count of|"
                   r"total number of|compare|the)\s+)+")
_CHANGE_VERBS = (r"increase[sd]?|decrease[sd]?|drop(?:ped|s)?|rise[sn]?|rose|rising|fall(?:en|s)?|fell|falling|"
                 r"decline[sd]?|grow[ns]?|grew|spike[sd]?|jump(?:ed|s)?|go(?:ne)? up|went up|go(?:ne)? down|went down|"
                 r"change[sd]?|surge[sd]?|dip(?:ped|s)?|climb(?:ed|s)?|evolve[sd]?|move[sd]?|shift(?:ed|s)?")
_WHY = re.compile(r"^(?:why|how) (?:did|has|have|is|are|was|were|do|does) (?:the )?(?P<m>.+?) (?:" + _CHANGE_VERBS +
                  r")\b(?P<rest>.*)$")
_WHAT_CHANGED = re.compile(r"^what (?:changed|drove the change|drives the change) (?:in|for|with) (?:the )?(?P<m>.+?)(?P<rest>)$")
_VS = re.compile(r"\s+(?:vs\.?|versus|compared (?:to|with)|against)\s+")
_COMPARE_CUE = re.compile(r"\b(?:compare[sd]?|comparison|versus|vs\.?|compared (?:to|with))\b|"
                          r"\bwhy (?:did|has|have|is|are|was|were|do|does)\b.*\b(?:" + _CHANGE_VERBS + r")\b|"
                          r"\bhow (?:did|has|have|does|do) .+ (?:change|changed|evolve|evolved)\b|\bwhat changed\b|"
                          r"\b(?:percent(?:age)? |% )?change (?:in|of|from|between|since)\b")
_RANGE = re.compile(r"(?:\b(?:in|over|during|for|within) (?:the )?)?\b(?:last|past|previous|recent|trailing) "
                    r"(?:\d+ |few |several |two |three |six |twelve )?(?:days?|weeks?|months?|quarters?|years?)\b"
                    r"|(?:\b(?:in|during|for) )?\b(?:this|the current) (?:week|month|quarter|year)\b"
                    r"|\b(?:year|month|quarter) to date\b|\b(?:ytd|mtd|qtd)\b|\bsince (?:" + _MONTHS + r"|\d{4})(?: \d{4})?\b"
                    r"|\b(?:in|during) (?:\d{4}|q[1-4](?: \d{4})?|(?:" + _MONTHS + r")(?: \d{4})?)\b")
_TREND = re.compile(r"\b(?:over time|trends?|trending|time series|monthly|weekly|daily|quarterly|yearly|annually"
                    r"|(?:per|by|each|every|a) (?:day|week|month|quarter|year)"
                    r"|(?:month|week|year|quarter) over (?:month|week|year|quarter)"
                    r"|(?:last|past) \d+ (?:days|weeks|months|quarters|years))\b")
_RANK = re.compile(r"\b(?:top|bottom) \d{1,3}\b|\b(?:highest|lowest|most|least|largest|smallest|biggest|best|worst)\b")
_CUE = re.compile(r"\s(?:broken down by|grouped by|split by|segmented by|for each|for every|in each|across|by|per)\s+")
_GRAINS = {"day", "days", "week", "weeks", "month", "months", "quarter", "quarters", "year", "years", "date", "time"}
_AGG_START = re.compile(r"^(?:average|avg|mean|median|total|sum of|sum)\b")
_COMPLEX_DIM = re.compile(r"\b(?:by|per|average|avg|mean|median|total|sum|count|number of|how many)\b")
_ASSUME = re.compile(r"[\s(]*proceed with this assumption:\s*(?P<a>.+?)[\s).]*$", re.I | re.S)
_COMPOUND = re.compile(r"\band\b|\bvs\.?\b|\bversus\b|\bwhy\b|\bcompare", re.I)


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[?!;:]+", " ", (text or "").lower().replace("’", "'"))).strip().rstrip(".")


def split_assumption(question: str) -> tuple[str, list[str]]:
    """`… Proceed with this assumption: <text>` (the clarify re-ask) -> (question, [text])."""
    m = _ASSUME.search(question or "")
    if m is None:
        return (question or "").strip(), []
    return question[:m.start()].strip(), [m.group("a").strip()]


def _strip(text: str) -> str:
    text = _RANGE.sub(" ", text)
    text = _TREND.sub(" ", text)
    text = _RANK.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" ,")


def _dims(text: str) -> tuple[list[str], str | None, bool]:
    """Dimensions named after a breakdown cue, the time grain among them, and whether they look like a
    second question rather than dimensions."""
    grain = None
    out, complex_ = [], False
    for part in re.split(r"\s*(?:,|&|\band\b|\bthen\b)\s*", _strip(text)):
        part = re.sub(r"^(?:the|each|every|a|an)\s+", "", part.strip())
        if not part:
            continue
        if part in _GRAINS:
            grain = part.rstrip("s")
            continue
        if _COMPLEX_DIM.search(part) or len(part.split()) > 4:
            complex_ = True
        out.append(part)
    return out, grain, complex_


def previous_of(period: str) -> str:
    p = period.strip()
    m = re.match(r"^(?:this|current|the current) (day|week|month|quarter|year)$", p)
    if m:
        return f"last {m.group(1)}"
    m = re.match(r"^(?:last|previous|prior|the previous|the last) (day|week|month|quarter|year)$", p)
    if m:
        return f"the {m.group(1)} before"
    return {"today": "yesterday", "yesterday": "the day before"}.get(p, "the period before")


def _in(period: str) -> str:
    return period if re.match(r"^(?:this|last|previous|prior|current|today|yesterday)\b", period) else f"in {period}"


def shapes(question: str) -> dict[str, Any]:
    """The shapes a question has (only the wording is read; nothing is resolved against the catalog)."""
    q = _plain(question)
    out: dict[str, Any] = {"text": q, "comparison": bool(_COMPARE_CUE.search(q)), "trend": bool(_TREND.search(q)),
                           "ranking": bool(_RANK.search(q)), "range": None, "periods": None, "form": None,
                           "measure": "", "dims": [], "grain": None, "complex": False}
    rng = _RANGE.search(q)
    out["range"] = rng.group(0).strip() if rng else None
    body = _LEAD.sub("", q)
    why = _WHY.match(q) or _WHAT_CHANGED.match(q)
    if why:
        out["comparison"], out["form"] = True, "change"
        measure, rest = why.group("m"), why.group("rest").strip()
        period = _PERIOD_RE.match(rest) or _TRAILING_PERIOD.search(" " + rest)
        if period:
            out["periods"] = (period.group(1), previous_of(period.group(1)))
        cue = _CUE.search(" " + rest + " ")
        if cue:
            out["dims"], out["grain"], out["complex"] = _dims(rest[cue.end() - 1:])
        out["measure"] = _strip(_LEAD.sub("", measure))
        return out
    sides = _VS.split(body, maxsplit=1)
    if len(sides) == 2:
        left, right = sides
        right = right.strip()
        cue = _CUE.search(" " + right + " ")
        right_period_text = right[:cue.start()].strip() if cue else right
        lp = _TRAILING_PERIOD.search(" " + left)
        rp = _PERIOD_RE.match(right_period_text)
        if lp and rp:
            out["comparison"], out["form"] = True, "periods"
            out["periods"] = (lp.group(1), rp.group(1))
            out["measure"] = _strip(left[:len(left) - len(lp.group(0)) + 1].strip())
            if cue:
                out["dims"], out["grain"], out["complex"] = _dims(right[cue.end() - 1:])
            return out
        out["form"] = "members"
    cue = _CUE.search(" " + body + " ")
    if cue and not out["ranking"]:
        start = max(cue.start() - 1, 0)
        out["measure"] = _strip(body[:start])
        out["dims"], out["grain"], out["complex"] = _dims(body[cue.end() - 1:])
    else:
        out["measure"] = _strip(body)
    if out["grain"]:
        out["trend"] = True
    return out


# ------------------------------------------------------------------------------ catalog
def planner_catalog(ctx: Any) -> dict[str, Any]:
    """The caller's tables as the planner needs them: entity words, ranked categorical dimensions (low
    cardinality per the column profile, no identifiers or personal columns) and measures. Never raises:
    without a catalog the plan still stands on the question's wording."""
    try:
        from analystos.agents import ask_rules
        from analystos.registries.verified_queries import tokens

        tables, _ = ask_rules.load_tables(ctx)
    except Exception as exc:  # noqa: BLE001 - the catalog only refines the plan
        log.warning("analyst planner: catalog unavailable: %s", exc)
        return {"tables": []}
    out = []
    for t in tables:
        first, others = ask_rules._default_dimension(t)
        dims = [c for c in [first, *others] if c is not None]
        out.append({"fq": t.fq, "entity": t.entity, "entity_words": sorted(t.entity_words | tokens(t.entity)),
                    "has_time": any(c.is_time for c in t.columns),
                    "dimensions": [{"name": c.name, "label": c.label.lower(), "words": sorted(c.words), "distinct": c.distinct}
                                   for c in dims],
                    "measures": [{"name": c.name, "label": c.label.lower(), "words": sorted(c.words)}
                                 for c in t.columns if c.is_measure]})
    return {"tables": out}


def _tokens(text: str) -> set[str]:
    from analystos.registries.verified_queries import tokens

    return set(tokens(text or ""))


def _table_for(catalog: dict[str, Any] | None, measure: str) -> dict[str, Any] | None:
    tables = (catalog or {}).get("tables") or []
    want = _tokens(measure)
    scored = sorted(((len(want & set(t["entity_words"])), i) for i, t in enumerate(tables)), reverse=True)
    if scored and scored[0][0] > 0:
        return tables[scored[0][1]]
    measured = [t for t in tables if any(want and want <= set(m["words"]) for m in t["measures"])]
    if len(measured) == 1:
        return measured[0]
    return tables[0] if len(tables) == 1 else None


def measure_kind(catalog: dict[str, Any] | None, measure: str) -> str | None:
    """count (the phrase names a table's records) | sum (a numeric column) | agg (already aggregated) | None."""
    if _AGG_START.match(measure):
        return "agg"
    want = _tokens(measure)
    for t in (catalog or {}).get("tables") or []:
        if want and want <= set(t["entity_words"]) | {"count", "record", "row"}:
            return "count"
        if want and any(want <= set(m["words"]) for m in t["measures"]):
            return "sum"
    return None


def driver_dimensions(catalog: dict[str, Any] | None, measure: str, question: str, k: int = 2,
                      exclude: tuple[str, ...] = ()) -> list[str]:
    """The most relevant categorical dimensions for a measure: words shared with the question first,
    then the catalog's own order (pack-declared, glossary, categorical names, fewest values)."""
    table = _table_for(catalog, measure)
    if table is None:
        return []
    asked = _tokens(question)
    skip = {e.lower() for e in exclude}
    dims = [d for d in table["dimensions"] if d["label"] not in skip and d["name"].lower() not in skip]
    ranked = sorted(enumerate(dims), key=lambda e: (-len(set(e[1]["words"]) & asked), e[0]))
    return [d["label"] for _, d in ranked[:k]]


def total_question(catalog: dict[str, Any] | None, measure: str) -> str:
    kind = measure_kind(catalog, measure)
    return {"agg": measure, "count": f"how many {measure}", "sum": f"total {measure}"}.get(kind or "", f"{measure} in total")


# ------------------------------------------------------------------------------ plan
def _step(kind: str, goal: str, question: str, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "goal": goal[:200], "question": re.sub(r"\s+", " ", question).strip()[:500], **extra}


def _with(question: str, suffixes: list[str]) -> str:
    extra = [s for s in suffixes if s and s.lower() not in question.lower()]
    return " ".join([question.rstrip(" ?."), *extra]).strip()


def plan_rules(question: str, catalog: dict[str, Any] | None = None, assumptions: list[str] | None = None) -> dict[str, Any]:
    """The deterministic plan (see the module doc). Steps are numbered, capped at MAX_STEPS and
    deduplicated; `assumptions` (accepted by the user) are added to every step question."""
    accepted = list(assumptions or [])
    s = shapes(question)
    measure = s["measure"]
    applied: list[str] = []
    steps: list[dict[str, Any]] = []
    approach = "One governed query answers this question."
    rng = [s["range"]] if s["range"] else []
    if s["comparison"] and measure and s["form"] in ("change", "periods"):
        cur, prev = s["periods"] or (None, None)
        if cur is None:
            cur, prev = DEFAULT_COMPARISON
            applied.append(f"compared {cur} with {prev}, because the question names no period")
        when = f"{_in(cur)} compared with {prev}, one column per period"
        steps.append(_step("comparison", f"Compare {measure} between {prev} and {cur}", f"{measure} {when}"))
        dims = s["dims"] if s["dims"] and not s["complex"] else driver_dimensions(catalog, measure, question)
        for d in dims[:MAX_STEPS - 1]:
            steps.append(_step("drivers", f"Find which {d} values drove the change", f"{measure} by {d} {when}"))
        approach = ("Compare the two periods, then break the change down by "
                    + (" and ".join(dims[:MAX_STEPS - 1]) or "no dimension (none found in the catalog)") + " to find the drivers.")
    elif len(s["dims"]) >= 2 and not s["complex"] and not s["ranking"] and measure:
        goal = f"Overall {measure}" if _AGG_START.match(measure) else f"Total {measure}"
        steps.append(_step("total", goal, _with(total_question(catalog, measure), rng)))
        per = f" per {s['grain']}" if s["grain"] else ""
        for d in s["dims"][:MAX_STEPS - 1]:
            steps.append(_step("breakdown", f"Break {measure} down by {d}", _with(f"{measure} by {d}{per}", rng)))
        approach = f"The total first, then one breakdown per dimension ({', '.join(s['dims'][:MAX_STEPS - 1])})."
    else:
        kind = ("comparison" if s["comparison"] else "trend" if s["trend"] else "ranking" if s["ranking"]
                else "breakdown" if s["dims"] else "question")
        goal = {"comparison": "Compare the values asked about", "trend": f"Show {measure or 'the measure'} over time",
                "ranking": "Rank the values asked about", "breakdown": f"Break {measure or 'the measure'} down by "
                + " and ".join(s["dims"]), "question": "Answer the question"}[kind]
        extra = {"assumption": TREND_ASSUMPTION} if kind == "trend" and not s["range"] else {}
        steps.append(_step(kind, goal, question.strip(), **extra))
    return _finalise({"approach": approach, "origin": "rules", "steps": steps, "assumptions": accepted + applied,
                      "measure": measure, "dimensions": s["dims"], "shape": {k: s[k] for k in
                      ("comparison", "trend", "ranking", "form", "range", "grain")}}, accepted)


def _finalise(plan: dict[str, Any], accepted: list[str]) -> dict[str, Any]:
    seen, steps = set(), []
    for st in plan["steps"]:
        q = _with(st["question"], accepted)
        key = _plain(q)
        if not key or key in seen:
            continue
        seen.add(key)
        steps.append({**st, "question": q, "n": len(steps) + 1})
        if len(steps) == MAX_STEPS:
            break
    plan["steps"] = steps
    plan["headline_step"] = 1
    return plan


def looks_compound(question: str) -> bool:
    return bool(_COMPOUND.search(question or ""))


def validate_model_plan(data: Any) -> str | None:
    """None when the model's plan is usable: 1..MAX_STEPS steps, each a non-empty question, no duplicates."""
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        return 'answer must be {"approach": str, "steps": [{"goal": str, "question": str}]}'
    steps = data["steps"]
    if not 1 <= len(steps) <= MAX_STEPS:
        return f"a plan has 1 to {MAX_STEPS} steps, not {len(steps)}"
    seen = set()
    for i, st in enumerate(steps, start=1):
        if not isinstance(st, dict) or not isinstance(st.get("question"), str) or not st["question"].strip():
            return f"step {i} has no question"
        if len(st["question"]) > 500:
            return f"step {i} question is too long"
        key = _plain(st["question"])
        if key in seen:
            return f"step {i} repeats an earlier step"
        seen.add(key)
    return None


def make_plan(ctx: Any, question: str, catalog: dict[str, Any] | None) -> dict[str, Any]:
    """Rules first; the `analyst_planning` model only for a one-step plan of a compound-looking question
    (or when an administrator sets the purpose to `always`). The model's plan is validated or dropped."""
    cleaned, accepted = split_assumption(question)
    plan = plan_rules(cleaned, catalog, accepted)
    if getattr(ctx, "router", None) is None:
        return plan
    from analystos.agents.common import llm_json, model_gate
    from analystos.llm.redaction import redact_question, restore_values

    rq = redact_question(cleaned)
    tables = [{"table": t["fq"], "dimensions": [d["label"] for d in t["dimensions"][:12]],
               "measures": [m["label"] for m in t["measures"][:12]], "has_time": t["has_time"]}
              for t in (catalog or {}).get("tables", [])[:8]]
    payload = {"question": rq.text, "max_steps": MAX_STEPS, "rules_plan": [st["question"] for st in plan["steps"]],
               "catalog": tables}
    compound = len(plan["steps"]) == 1 and looks_compound(cleaned)
    if not model_gate(ctx, PLANNING_PURPOSE, payload, deterministic_ok=not compound):
        return plan
    data, model = llm_json(ctx, PLANNING_PURPOSE, "analyst_planning.v1", payload, validate=validate_model_plan)
    if data is None:
        plan["model_outcome"] = str(model)
        return plan
    reason = validate_model_plan(data)
    if reason:
        plan["model_rejected"] = reason
        return plan
    steps = [_step(st.get("kind") if st.get("kind") in KINDS else "question",
                   str(st.get("goal") or f"Step {i}"), restore_values(st["question"], rq.values))
             for i, st in enumerate(data["steps"], start=1)]
    return _finalise({**plan, "approach": str(data.get("approach") or "Planned by a model; each step is a governed query.")[:500],
                      "origin": "model", "model": model, "steps": steps}, accepted)


# ------------------------------------------------------------------------------ step analytics
def analyse(kind: str, *, sql: str | None, dialect: str, result: dict[str, Any],
            history: tuple[float, ...] = ()) -> dict[str, Any]:
    """Facts, series, two-period comparison/drivers and checks of one answered step's result."""
    columns, rows = list(result.get("columns") or []), list(result.get("rows") or [])
    row_count = result.get("row_count")
    truncated = bool(result.get("truncated"))
    dims = rf.grouped_columns(sql, dialect)
    out: dict[str, Any] = {"facts": rf.step_facts(columns, rows, row_count=row_count, truncated=truncated, dimensions=dims)}
    series = rf.series_analysis(columns, rows, dims) if out["facts"]["time_column"] else None
    if series is not None:
        out["series"] = series
    comparison = rf.two_period(columns, rows, dims) if kind != "trend" else None
    if comparison is not None:
        out["comparison"] = {k: v for k, v in comparison.items()
                             if k not in ("drivers", "offsets", "appeared", "disappeared", "members")}
        if "drivers" in comparison:
            out["drivers"] = {k: comparison[k] for k in ("members", "drivers", "offsets", "appeared", "disappeared")}
    out["checks"] = rf.step_checks(kind, sql=sql, dialect=dialect, columns=columns, rows=rows, row_count=row_count,
                                   truncated=truncated, series=series, comparison=comparison, history=history,
                                   dimensions=dims)
    return out


def stored_result(result: dict[str, Any]) -> dict[str, Any]:
    rows = list(result.get("rows") or [])
    return {**{k: v for k, v in result.items() if k != "rows"}, "rows": rows[:STEP_ROWS],
            "rows_stored": min(len(rows), STEP_ROWS)}


def step_entry(step: dict[str, Any], out: dict[str, Any], dialect: str,
               finish: Callable[[dict[str, Any]], tuple[str, dict[str, Any] | None]]) -> dict[str, Any]:
    status, refusal = finish(out)
    entry: dict[str, Any] = {k: step[k] for k in ("n", "kind", "goal", "question")}
    entry.update({"status": status, "answered_by": out.get("answered_by") if status == "answered" else None,
                  "route": out.get("route"), "sql": out.get("sql"), "explanation": out.get("explanation"),
                  "chart": out.get("chart") if isinstance(out.get("chart"), dict) else None, "model": out.get("model"),
                  "verified_query": out.get("verified_query"), "governance": out.get("governance", "ad_hoc"),
                  "semantic": out.get("semantic"), "suggestions": list(out.get("suggestions") or []),
                  "attempts": len(out.get("attempts") or [])})
    if out.get("rules"):
        entry["rules"] = out["rules"]
    if refusal is not None:
        entry["refusal"] = refusal
    if status == "answered" and isinstance(out.get("result"), dict):
        entry["result"] = stored_result(out["result"])
        entry.update(analyse(step["kind"], sql=out.get("sql"), dialect=dialect, result=out["result"]))
    return entry


def skipped_entry(step: dict[str, Any], refusal: dict[str, Any] | None, note: str) -> dict[str, Any]:
    return {**{k: step[k] for k in ("n", "kind", "goal", "question")}, "status": "skipped", "answered_by": None,
            "sql": None, "result": None, "checks": [], "note": note, **({"refusal": refusal} if refusal else {})}


def rerun_entry(old: dict[str, Any], sql: str, result: dict[str, Any], dialect: str, *, edited: bool, at: str) -> dict[str, Any]:
    """A step re-run with (edited) SQL through the gateway: facts and checks recomputed; the previous
    headline value feeds the magnitude check, the previous SQL and query are kept."""
    before = rf.headline_value(old.get("result", {}).get("columns") or [], old.get("result", {}).get("rows") or []) \
        if old.get("result") else None
    entry = {k: v for k, v in old.items() if k not in ("facts", "series", "comparison", "drivers", "checks", "refusal", "note")}
    entry.update({"status": "answered", "answered_by": "sql", "sql": sql, "model": None, "governance": "ad_hoc",
                  "semantic": None, "verified_query": None, "result": stored_result(result),
                  "rerun": {"at": at, "edited": edited, "previous_sql": old.get("sql"),
                            "previous_query_id": (old.get("result") or {}).get("query_id")}})
    entry.update(analyse(old.get("kind") or "question", sql=sql, dialect=dialect, result=result,
                         history=(before,) if before is not None else ()))
    return entry


# ------------------------------------------------------------------------------ synthesis
def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def key_facts(e: dict[str, Any]) -> list[str]:
    """The step's facts as short clauses, most important first (the first one answers the step)."""
    facts = e.get("facts") or {}
    out: list[str] = []
    comp, drv, ser = e.get("comparison"), e.get("drivers"), e.get("series")
    if comp:
        what = comp.get("measure") or "the total"
        pct = f", {rf.fmt_pct(comp['pct_change'], signed=True)}" if comp.get("pct_change") is not None else ""
        out.append(f"{what} went from {rf.fmt_num(comp['previous'])} to {rf.fmt_num(comp['current'])} "
                   f"({rf.fmt_num(comp['change'], signed=True)}{pct})")
    if drv and drv.get("drivers"):
        d = drv["drivers"][0]
        share = f", {rf.fmt_pct(d['share_of_change'])} of the change" if d.get("share_of_change") is not None else ""
        out.append(f"the largest driver is {d['member']} ({rf.fmt_num(d['change'], signed=True)}{share})")
        if drv.get("offsets"):
            o = drv["offsets"][0]
            out.append(f"{o['member']} moved the other way ({rf.fmt_num(o['change'], signed=True)})")
        if drv.get("appeared"):
            out.append("new in the current period: " + ", ".join(map(str, drv["appeared"][:3])))
        if drv.get("disappeared"):
            out.append("gone in the current period: " + ", ".join(map(str, drv["disappeared"][:3])))
    if ser:
        per = ser.get("grain") or "period"
        if ser["direction"] == "flat":
            trend = f"{ser['column']} is flat: the slope is under one percent of the mean"
        else:
            share = (f" ({rf.fmt_pct(ser['slope_share_of_mean'], signed=True)} of the mean)"
                     if ser.get("slope_share_of_mean") is not None else "")
            trend = f"{ser['column']} is {ser['direction']} by {rf.fmt_num(ser['slope_per_period'], signed=True)} per {per}{share}"
        out.append(f"{trend} over {rf.fmt_num(ser['points'])} periods from {ser['first_period']} to {ser['last_period']}")
        if ser.get("anomalies"):
            a = ser["anomalies"][0]
            out.append(f"{rf.fmt_num(len(ser['anomalies']))} anomalous period(s); the largest is {a['period']} at "
                       f"{rf.fmt_num(a['value'])}")
    if out:
        if comp and drv and drv.get("drivers") and e.get("kind") == "drivers":
            out = out[1:] + out[:1]  # a driver step answers "which members"; its total repeats the comparison step
        return out
    if not facts.get("rows_shown"):
        return ["no rows were returned"]
    if facts.get("values"):
        return ["; ".join(f"the {col} is {rf.fmt_num(v)}" for col, v in list(facts["values"].items())[:3])]
    measures = facts.get("measures") or []
    if measures:
        m = next((x for x in measures if not x["pre_aggregated"]), measures[0])
        hi, lo = m.get("max_label") or "one row", m.get("min_label") or "one row"
        if "total" in m:
            out.append(f"{hi} has the highest {m['column']} ({rf.fmt_num(m['max'])}) of a total of "
                       f"{rf.fmt_num(m['total'])} across {rf.fmt_num(m['count'])} rows")
        else:
            out.append(f"{hi} has the highest {m['column']} ({rf.fmt_num(m['max'])}); {lo} the lowest ({rf.fmt_num(m['min'])})")
        if len(measures) > 1 and "total" in m:
            out.append(f"{lo} has the lowest {m['column']} ({rf.fmt_num(m['min'])})")
        return out
    return [f"{rf.fmt_num(facts.get('row_count'))} rows were returned"]


def caveats(plan: dict[str, Any], entries: list[dict[str, Any]]) -> list[str]:
    out = []
    for e in entries:
        n = e["n"]
        if e["status"] == "answered":
            for c in e.get("checks") or []:
                if c["status"] == "suspect":
                    out.append(f"{e['goal']}: {c['note']} (step {n}).")
            if e.get("comparison") and e.get("kind") in ("comparison", "drivers") and e["comparison"].get("pct_change") is None \
                    and e["comparison"].get("previous") == 0:
                out.append(f"{e['goal']}: the previous period is zero, so no percentage change applies (step {n}).")
        else:
            why = (e.get("refusal") or {}).get("title") or e.get("note") or "not answered"
            out.append(f"{e['goal']} could not be answered: {why} (step {n}).")
    for a in plan.get("assumptions") or []:
        out.append(f"Assumed: {a}.")
    return out


def template_synthesis(plan: dict[str, Any], entries: list[dict[str, Any]], headline: int) -> dict[str, Any]:
    answered = {e["n"]: e for e in entries if e["status"] == "answered"}
    head = answered[headline]
    facts = key_facts(head)
    answer = f"{_cap(facts[0])} (step {headline})."
    evidence = [{"step": headline, "text": f"{_cap(f)} (step {headline})."} for f in facts[1:3]]
    for n, e in answered.items():
        if n == headline:
            continue
        evidence += [{"step": n, "text": f"{e['goal']}: {f} (step {n})."} for f in key_facts(e)[:2]]
    notes = caveats(plan, entries)
    text = answer
    if evidence:
        text += "\n\nEvidence:\n" + "\n".join(f"- {b['text']}" for b in evidence)
    if notes:
        text += "\n\nCaveats:\n" + "\n".join(f"- {c}" for c in notes)
    return {"text": text, "answer": answer, "evidence": evidence, "caveats": notes, "citations": rf.citations(text),
            "origin": "template"}


def _aggregated(sql: str | None, dialect: str) -> bool:
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(sql or "", read=dialect)
    except Exception:  # noqa: BLE001 - unparsable: treat as row-level
        return False
    return tree is not None and (tree.find(exp.AggFunc) is not None or tree.find(exp.Group) is not None)


def bound_inputs(entries: list[dict[str, Any]], plan: dict[str, Any], rows: dict[int, list[list[Any]]]) -> tuple[list[float], list[str]]:
    """The numbers a synthesis may quote (facts, series, comparison, drivers, the rows it was shown and
    the numbers in check notes) and the labels to mask before checking (members, periods, columns)."""
    parts: list[Any] = []
    for e in entries:
        if e["status"] != "answered":
            continue
        parts += [e.get("facts"), e.get("series"), e.get("comparison"), e.get("drivers"), rows.get(e["n"], [])]
        parts += [[float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*(?:\.\d+)?", c["note"])]
                  for c in e.get("checks") or []]
        parts.append(list((e.get("result") or {}).get("columns") or []))
    labels = rf.fact_labels(*parts) + [e["goal"] for e in entries] + list(plan.get("assumptions") or [])
    labels += [str(v) for r in rows.values() for row in r for v in row if isinstance(v, str)]
    return rf.fact_values(*parts), labels


def synthesize(ctx: Any, question: str, plan: dict[str, Any], entries: list[dict[str, Any]], headline: int) -> dict[str, Any]:
    """Template first; the `analyst_synthesis` model only when its purpose allows, from the facts only,
    and accepted only if `numbers_bound` passes."""
    template = template_synthesis(plan, entries, headline)
    if getattr(ctx, "router", None) is None:
        return template
    from analystos.agents.common import llm_json, model_gate
    from analystos.llm.redaction import redact_question

    dialect = next(iter((getattr(ctx.scope, "source_dialects", None) or {}).values()), "postgres")
    samples = bool(getattr(getattr(ctx, "policy", None), "send_data_samples_to_models", False))
    rows = {e["n"]: [list(r) for r in (e.get("result") or {}).get("rows", [])[:MODEL_ROWS]]
            for e in entries if e["status"] == "answered" and (samples or _aggregated(e.get("sql"), dialect))}
    steps = []
    for e in entries:
        item: dict[str, Any] = {"n": e["n"], "goal": e["goal"], "status": e["status"]}
        if e["status"] == "answered":
            item.update({"facts": (e.get("facts") or {}).get("statements"), "measures": (e.get("facts") or {}).get("measures"),
                         "series": e.get("series"), "comparison": e.get("comparison"),
                         "drivers": {k: (v[:5] if isinstance(v, list) else v) for k, v in (e.get("drivers") or {}).items()} or None,
                         "suspect_checks": [c["note"] for c in e.get("checks") or [] if c["status"] == "suspect"]})
            if e["n"] in rows:
                item["columns"], item["rows"] = (e.get("result") or {}).get("columns"), rows[e["n"]]
        steps.append(item)
    payload = {"question": redact_question(question).text, "headline_step": headline, "steps": steps,
               "assumptions": plan.get("assumptions") or [], "template": template["text"]}
    if not model_gate(ctx, SYNTHESIS_PURPOSE, payload, deterministic_ok=True):
        return template
    values, labels = bound_inputs(entries, plan, rows)
    answered = [e["n"] for e in entries if e["status"] == "answered"]

    def check(data: Any) -> str | None:
        if not isinstance(data, dict) or not isinstance(data.get("text"), str) or not data["text"].strip():
            return 'answer must be {"text": str}'
        bound = rf.numbers_bound(data["text"], values, labels=labels, steps=answered)
        return None if bound["ok"] else "; ".join(bound["problems"][:3])

    data, model = llm_json(ctx, SYNTHESIS_PURPOSE, "analyst_synthesis.v1", payload, validate=check)
    if data is None:
        return {**template, "model_outcome": str(model)}
    reason = check(data)
    if reason:
        return {**template, "rejected": reason[:500], "model": model}
    text = data["text"].strip()
    return {"text": text, "answer": text.split("\n", 1)[0], "evidence": template["evidence"], "caveats": template["caveats"],
            "citations": rf.citations(text), "origin": "model", "model": model}


# ------------------------------------------------------------------------------ follow-ups
def follow_ups(plan: dict[str, Any], catalog: dict[str, Any] | None, entries: list[dict[str, Any]]) -> list[str]:
    measure = plan.get("measure") or ""
    kinds = {st["kind"] for st in plan["steps"]}
    used = tuple(plan.get("dimensions") or []) + tuple(
        m.group(1) for st in plan["steps"] for m in [re.search(r" by (.+?)(?: in | per |$|,)", st["question"])] if m)
    out: list[str] = []
    if measure:
        table = _table_for(catalog, measure)
        nxt = driver_dimensions(catalog, measure, "", k=2, exclude=used)
        if "trend" not in kinds and (table is None or table["has_time"]):
            out.append(f"{measure} over time")
        for d in nxt:
            out.append(f"{measure} by {d}")
        if "trend" in kinds and nxt:
            out.append(f"{measure} by {nxt[0]} over time")
    for e in entries:
        out += e.get("suggestions") or []
    asked = {_plain(st["question"]) for st in plan["steps"]}
    seen: list[str] = []
    for q in out:
        if _plain(q) not in asked and _plain(q) not in {_plain(s) for s in seen}:
            seen.append(q)
    return seen[:MAX_FOLLOW_UPS]


# ------------------------------------------------------------------------------ run
def _stage(ctx: Any, key: str, text: str, **data: Any) -> None:
    fn = getattr(ctx, "on_stage", None)
    if fn is not None:
        fn(key, text, data)


def _ask_step(ctx: Any, step: dict[str, Any], total: int, ask_fn: Callable[..., dict[str, Any]],
              refuse: Callable[[AnalystOSError], dict[str, Any]], parameters: dict[str, Any] | None) -> dict[str, Any]:
    outer = getattr(ctx, "on_stage", None)
    n = step["n"]

    def inner(key: str, text: str, data: dict[str, Any]) -> None:
        if outer is not None:
            outer(key, f"Step {n} of {total}: {text}", {**(data or {}), "step": n, "of": total})

    ctx.on_stage = inner
    try:
        return ask_fn(ctx, step["question"], parameters=parameters or None)
    except AnalystOSError as exc:
        return {"status": "refused", "refusal": refuse(exc)}
    except Exception as exc:  # noqa: BLE001 - one broken step must not lose the others
        log.exception("analyst step %s failed", n)
        return {"status": "refused", "refusal": refuse(AnalystOSError(f"The step failed ({type(exc).__name__})."))}
    finally:
        ctx.on_stage = outer


def _mirror(entry: dict[str, Any], out: dict[str, Any]) -> None:
    for k in ("answered_by", "sql", "chart", "model", "verified_query", "governance", "semantic"):
        out[k] = entry.get(k)


def run(ctx: Any, question: str, *, parameters: dict[str, Any] | None = None, ask_fn: Callable[..., dict[str, Any]],
        finish: Callable[[dict[str, Any]], tuple[str, dict[str, Any] | None]],
        refuse: Callable[[AnalystOSError], dict[str, Any]]) -> dict[str, Any]:
    """One analyst-mode turn. Returns the single-question `out` shape (status, sql, result, chart ... of
    the headline step, so every existing screen keeps working) plus `analysis`."""
    if parameters and "semantic_query" in parameters:
        raise InvalidInput("semantic_query is a quick-mode option; ask in quick mode to run a metric query.")
    catalog = planner_catalog(ctx)
    plan = make_plan(ctx, question, catalog)
    steps, total = plan["steps"], len(plan["steps"])
    _stage(ctx, "plan", f"Planned {total} step{'s' if total != 1 else ''}: {plan['approach']}", origin=plan["origin"],
           steps=[{k: st[k] for k in ("n", "goal", "question")} for st in steps])
    dialect = next(iter((getattr(ctx.scope, "source_dialects", None) or {}).values()), "postgres")
    entries: list[dict[str, Any]] = []
    full: dict[int, dict[str, Any]] = {}
    stop: dict[str, Any] | None = None
    clarified: dict[str, Any] | None = None
    decisions: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    for st in steps:
        n = st["n"]
        if stop is not None:
            entries.append(skipped_entry(st, stop, "not run: the Ask budget is used up"))
            continue
        if clarified is not None:
            entries.append(skipped_entry(st, None, "not run: an earlier step needs more detail"))
            continue
        _stage(ctx, "step", f"Step {n} of {total}: {st['goal']}", step=n, of=total, question=st["question"])
        out = _ask_step(ctx, st, total, ask_fn, refuse, parameters)
        entry = step_entry(st, out, dialect, finish)
        entries.append(entry)
        decisions += [{**d, "step": n} for d in out.get("decisions") or []]
        attempts += [{**a, "step": n} for a in out.get("attempts") or []]
        if entry["status"] == "answered":
            full[n] = out
            suspect = sum(1 for c in entry.get("checks") or [] if c["status"] == "suspect")
            _stage(ctx, "checks", f"Step {n} of {total}: facts computed, {suspect} check(s) to look at" if suspect
                   else f"Step {n} of {total}: facts computed, every check passed", step=n, of=total, suspect=suspect)
        elif entry["status"] in ("clarify", "needs_input"):
            clarified = entry
        elif (entry.get("refusal") or {}).get("kind") in ("budget_exceeded", "spend_cap"):
            stop = entry["refusal"]
    plan_out = {k: plan[k] for k in ("approach", "origin", "assumptions") if k in plan}
    plan_out["steps"] = [{k: st[k] for k in ("n", "kind", "goal", "question")} for st in steps]
    for k in ("model", "model_rejected", "model_outcome"):
        if plan.get(k):
            plan_out[k] = plan[k]
    analysis: dict[str, Any] = {"mode": "analyst", "plan": plan_out, "steps": entries, "synthesis": None,
                                "follow_ups": [], "headline_step": None}
    base = {"route": "analyst", "decisions": decisions, "attempts": attempts, "analysis": analysis, "model": None}
    if clarified is not None:
        ref = clarified.get("refusal") or {}
        details = ref.get("details") or {}
        assumption = next((s.get("assumption") for s in steps if s["n"] == clarified["n"] and s.get("assumption")), None)
        if assumption is None and st_needs_period(clarified, plan):
            assumption = "comparing " + " with ".join(DEFAULT_COMPARISON)
        return {**base, "status": "clarify", "clarify_step": clarified["n"], "assumption": assumption,
                "missing": details.get("missing") or [], "suggestions": details.get("suggestions") or [],
                "explanation": f"{clarified['goal']} (step {clarified['n']}) needs more detail: {ref.get('message') or ''}".strip()
                + (f" Suggested assumption: {assumption}." if assumption else "")}
    answered = [e for e in entries if e["status"] == "answered"]
    if not answered:
        first = entries[0].get("refusal") if entries else None
        return {**base, "status": "refused", "refusal": first}
    headline = plan.get("headline_step", 1)
    if headline not in full:
        headline = answered[0]["n"]
    _stage(ctx, "synthesis", "Writing the answer from the computed facts")
    analysis["synthesis"] = synthesize(ctx, question, plan, entries, headline)
    analysis["follow_ups"] = follow_ups(plan, catalog, entries)
    analysis["headline_step"] = headline
    head = next(e for e in entries if e["n"] == headline)
    out = {**base, "status": "answered", "result": full[headline].get("result"),
           "explanation": analysis["synthesis"]["text"], "suggestions": analysis["follow_ups"],
           "parent_turn_id": None}
    _mirror(head, out)
    if head.get("rules"):
        out["rules"] = head["rules"]
    return out


def st_needs_period(entry: dict[str, Any], plan: dict[str, Any]) -> bool:
    return entry.get("kind") in ("comparison", "drivers") and not (plan.get("shape") or {}).get("range") \
        and not any("compared" in a for a in plan.get("assumptions") or [])


__all__ = ["MAX_STEPS", "analyse", "driver_dimensions", "follow_ups", "key_facts", "make_plan", "plan_rules",
           "planner_catalog", "rerun_entry", "run", "shapes", "split_assumption", "step_entry", "synthesize",
           "template_synthesis", "validate_model_plan"]
