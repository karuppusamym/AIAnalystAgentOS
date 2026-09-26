"""Ask rules rung: simple single-table question shapes answered from catalog metadata, no model.

Shapes (after polite lead-ins like "show me" / "what is the" are dropped):

  distribution of X [by Y]              breakdown of X by Y, X broken down by Y, X distribution by Y
  count of X by Y                       number of X per Y, X count by Y, count X by Y, X by Y
  how many X [verb] per|by|each Y       "how many orders were placed each month", "... through each channel"
  how many X [verb] [in total]          a plain row count
  top|bottom N Y [by X]                 ranked counts (or "by total/average M")
  X over time | X per|by|each month     series (X a count, or "average/total M"); daily/weekly/monthly X
  average|mean|median|total M [by Y]    one aggregate of a numeric column, optionally grouped

Resolution is deterministic and strict. X names a table (its entity words, e.g. "orders") or,
for a distribution, a categorical column. Y and M name columns of that table by their words:
the column name, its business name and the workspace glossary synonyms (the registry lexicon);
every word of the phrase must be explained by one column (an exact name wins over a wider one).
Two or more columns that explain a phrase equally -> clarify, listing them. Anything left over
(a filter, a comparison, an unknown word, a restricted or personal column) -> no plan: the question
falls through to generation, which is where it went before this rung existed.

A distribution with no Y picks its grouping by a documented rule: a dimension the domain pack
declares for the entity (`ask_distributions`), else the categorical column (2..50 profiled values,
not an identifier, key, free text or personal column) with a glossary link, then a categorical name
(status, priority, category, ...), then the fewest values, then catalog order. The other candidates
come back as follow-up questions.

A time column is chosen the same way: the only one, else the one the question names (a verb such
as "opened" or "resolved"), else the one whose name says the record starts (created, opened, start,
the pack's `event_start` words, or the entity itself: order_date for orders), else clarify.

The SQL is built by `skills/sqlbuild.aggregate_query` (quoted identifiers, sqlglot nodes) and runs
through `QueryGateway.execute`, whose validator checks scope and columns as for any statement.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from analystos.contracts.analysis import Derivation
from analystos.core.logging import get_logger

log = get_logger(__name__)

GRAINS = ("day", "week", "month", "quarter")
DEFAULT_GRAIN = "month"
MAX_DIMENSIONS = 2
MAX_DEFAULT_VALUES = 50  # a default grouping must be readable as a distribution
MAX_GROUP_VALUES = 1000  # skills/sqlbuild.MAX_GROUPS: more distinct values is an identifier, not a dimension
SUGGESTIONS = 4
SUPPORTED_DIALECTS = ("postgres", "duckdb", "tsql")

# ------------------------------------------------------------------------------ parsing
_LEAD = re.compile(r"^(?:(?:please|can you|could you|show me|show|give me|get me|list|tell me|plot|chart|display|draw|"
                   r"what is|what's|whats|what are|i want|i need|let me see|see)\s+)+")
_ARTICLES = re.compile(r"\b(?:the|a|an|our|my|all)\s+")
_CUE = r"(?:broken down by|grouped by|split by|segmented by|based on|for each|for every|in each|through each|across|by|per|each|every)"
_WORD_GRAIN = {"daily": "day", "weekly": "week", "monthly": "month", "quarterly": "quarter"}
_VERBS = ("came in", "come in", "were opened", "opened", "created", "raised", "logged", "placed", "received", "submitted",
          "closed", "resolved", "filed", "reported", "booked", "issued", "paid", "shipped", "made", "recorded")
_VERB = re.compile(r"^(?P<x>.+?)(?:\s+(?:that |which )?(?:were|was|are|is|have been|has been|got|get|did|do)?\s*"
                   r"(?P<verb>" + "|".join(re.escape(v) for v in _VERBS) + r")(?:\s+(?:through|via|in|on|at|from))?)$")
_TOTAL_SUFFIX = re.compile(r"\s+(?:in total|overall|altogether|are there|were there|do we have|we have|exist|in the data)$")
_AGG_WORDS = {"average": "avg", "avg": "avg", "mean": "avg", "median": "median", "total": "sum", "sum of": "sum", "sum": "sum"}
_AGG = r"(?P<agg>average|avg|mean|median|total|sum of|sum)"
_COUNT_NOUNS = frozenset({"count", "number", "volume", "record", "row", "entry", "item"})

_SHAPES: list[tuple[str, re.Pattern[str]]] = [
    ("top", re.compile(r"^(?P<dir>top|bottom) (?P<n>\d{1,3}) (?P<y>.+?)(?: by (?P<x>.+))?$")),
    ("distribution", re.compile(r"^(?:distribution|breakdown|split|mix) of (?P<x>.+?)(?: " + _CUE + r" (?P<y>.+))?$")),
    ("distribution", re.compile(r"^(?P<x>.+?) (?:distribution|breakdown)(?: " + _CUE + r" (?P<y>.+))?$")),
    ("distribution", re.compile(r"^(?P<x>.+?) (?:broken down|split|grouped|segmented|distributed) by (?P<y>.+)$")),
    ("aggregate", re.compile(r"^" + _AGG + r" (?:of )?(?P<m>.+?)(?: " + _CUE + r" (?P<y>.+))?$")),
    ("count", re.compile(r"^(?:how many|number of|count of|total number of|no of|count) (?P<x>.+?)(?: " + _CUE + r" (?P<y>.+))?$")),
    ("count", re.compile(r"^(?P<x>.+?) (?:count|counts|volume) (?:" + _CUE + r") (?P<y>.+)$")),
    ("count", re.compile(r"^(?P<x>[a-z0-9 ]+?) (?:by|per) (?P<y>.+)$")),
]


@dataclass
class Intent:
    shape: str  # count | distribution | aggregate | top
    agg: str = "count"  # count | sum | avg | median
    subject: str | None = None  # the records counted (or, for a distribution, possibly a column)
    measure: str | None = None
    dims: list[str] = field(default_factory=list)
    grain: str | None = None
    top: int | None = None
    descending: bool = True
    verb: str | None = None


def _normalize(question: str) -> str:
    q = " " + re.sub(r"[?!.;:]+", " ", question.lower().replace("’", "'")).strip() + " "
    q = re.sub(r"\s+", " ", q).strip()
    q = _LEAD.sub("", q)
    return _ARTICLES.sub("", q + " ").strip()


def _split_dims(text: str | None) -> list[str]:
    if not text:
        return []
    return [p.strip() for p in re.split(r"\s*(?:,|&|\band\b|\bthen\b)\s*", text) if p.strip()]


def parse(question: str) -> Intent | None:
    """The shape of a question, or None. Only the wording is read here; nothing is resolved."""
    q = _normalize(question)
    if not q or len(q) > 200:
        return None
    grain = None
    over = re.match(r"^(?P<x>.+?) (?:over time|trend|trends|time series)$", q)
    if over:
        q, grain = over.group("x"), DEFAULT_GRAIN
    pre = re.match(r"^(?P<g>daily|weekly|monthly|quarterly) (?P<x>.+)$", q)
    if pre:
        q, grain = pre.group("x"), _WORD_GRAIN[pre.group("g")]
    total = _TOTAL_SUFFIX.search(q)
    if total:
        q = q[:total.start()].strip()
    for shape, pattern in _SHAPES:
        m = pattern.match(q)
        if m is None:
            continue
        g = m.groupdict()
        intent = Intent(shape=shape, dims=_split_dims(g.get("y")), grain=grain)
        if shape == "top":
            intent.top, intent.descending = int(g["n"]), g["dir"] == "top"
            intent.dims = _split_dims(g["y"])
            x = (g.get("x") or "").strip()
            am = re.match(r"^" + _AGG + r" (?:of )?(?P<m>.+)$", x)
            if am:
                intent.agg, intent.measure = _AGG_WORDS[am.group("agg")], am.group("m")
            else:
                intent.subject = x or None
        elif shape == "aggregate":
            intent.agg, intent.measure = _AGG_WORDS[g["agg"]], re.sub(r"^(?:number of|count of) ", "", g["m"].strip())
        else:
            intent.subject = g["x"].strip()
        if intent.subject:
            vm = _VERB.match(intent.subject)
            if vm:
                intent.subject, intent.verb = vm.group("x").strip(), vm.group("verb").split()[-1]
        grains = [d for d in intent.dims if d in GRAINS or d in ("time", "date")]
        for d in grains:
            intent.dims.remove(d)
            intent.grain = DEFAULT_GRAIN if d in ("time", "date") else d
        if len(intent.dims) > MAX_DIMENSIONS or (grains and len(grains) > 1):
            return None
        if shape == "count" and pattern is _SHAPES[-1][1] and not intent.dims and not intent.grain:
            return None
        return intent
    if grain is not None:  # "orders over time", "monthly orders", "average invoice amount over time"
        am = re.match(r"^" + _AGG + r" (?:of )?(?P<m>.+)$", q)
        if am:
            return Intent(shape="aggregate", agg=_AGG_WORDS[am.group("agg")], measure=am.group("m"), grain=grain)
        return Intent(shape="count", subject=q, grain=grain)
    return None


# ------------------------------------------------------------------------------ catalog
_ABBREVIATIONS = {"pct": ("percent", "percentage"), "perc": ("percent", "percentage"), "amt": ("amount",),
                  "qty": ("quantity",), "cnt": ("count",), "num": ("count",), "dt": ("date",), "cat": ("category",),
                  "dept": ("department",), "grp": ("group",), "desc": ("description",), "addr": ("address",),
                  "ts": ("time",), "tm": ("time",)}
_CATEGORICAL_WORDS = frozenset({"status", "state", "type", "category", "priority", "severity", "impact", "urgency", "stage",
                                "tier", "segment", "channel", "group", "class", "level", "grade", "band", "source", "method",
                                "mode", "kind", "department", "currency", "region", "country", "brand", "reason"})
_NOT_DIMENSION_ROLES = frozenset({"identifier", "text", "contact", "timestamp", "date"})
_MEASURE_ROLES = frozenset({"measure", "amount", "percent", "duration"})
_START_WORDS = ("created", "opened", "start", "started", "submitted", "received", "placed", "ordered", "booked", "issued")


@dataclass
class Column:
    name: str
    data_type: str
    role: str
    semantic: str | None
    distinct: int | None
    ordinal: int
    words: frozenset[str]  # every word that may name it
    name_words: frozenset[str]  # the words of its own name (exact match)
    glossary: bool
    personal: bool
    label: str

    @property
    def is_time(self) -> bool:
        return self.semantic == "datetime" or self.role in ("timestamp", "date") or self.data_type in ("timestamp", "date")

    @property
    def is_numeric(self) -> bool:
        return self.data_type in ("integer", "bigint", "numeric", "double") or self.semantic == "numeric"

    @property
    def is_measure(self) -> bool:
        return self.is_numeric and not self.is_time and (self.role in _MEASURE_ROLES or self.role == "unknown")

    @property
    def is_dimension(self) -> bool:
        if self.personal or self.is_time or self.role in _NOT_DIMENSION_ROLES:
            return False
        if self.distinct is not None and self.distinct > MAX_GROUP_VALUES:
            return False
        return not self.is_measure or (self.distinct is not None and self.distinct <= MAX_DEFAULT_VALUES)

    @property
    def default_candidate(self) -> bool:
        return (self.is_dimension and self.role not in ("foreign_key", "name") and self.semantic != "boolean"
                and self.role != "flag" and self.distinct is not None and 2 <= self.distinct <= MAX_DEFAULT_VALUES)


@dataclass
class Table:
    fq: str
    source_id: str
    dialect: str
    entity: str
    entity_words: frozenset[str]
    columns: list[Column]
    pack_dimensions: list[tuple[str, str]] = field(default_factory=list)  # (label, column) the domain pack declares
    event_start: tuple[str, ...] = ()

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


def _words(text: str | None, lex: Any = None) -> frozenset[str]:
    from analystos.registries.verified_queries import tokens

    out = set(tokens(text or "", lex))
    for w in list(out):
        out.update(_ABBREVIATIONS.get(w, ()))
    return frozenset(out)


def load_tables(ctx: Any) -> tuple[list[Table], Any]:
    """The caller's in-scope tables with their column metadata (denied columns removed), and the
    workspace lexicon. Reads the catalog the crawler wrote; semantics missing there are inferred
    from the column name and type (the same deterministic rules the crawler uses)."""
    from sqlalchemy import select

    from analystos.capabilities import packs
    from analystos.connectors.base import DiscoveredColumn
    from analystos.db.base import session_scope
    from analystos.db.models import SourceAsset, SourceColumn
    from analystos.knowledge.entries import visible_entries
    from analystos.registries import verified_queries as vqr
    from analystos.skills.catalog import _entity_from_table, infer_column_semantics, normalize_type, split_tokens

    scope = ctx.scope
    denied = set(getattr(scope, "denied_columns", None) or [])
    hints = packs.hints()
    declared = {str(d.get("entity", "")).lower(): d for d in hints.ask_distributions}
    tables: list[Table] = []
    with session_scope() as s:
        lex = vqr.lexicon(s, ctx.workspace.id)
        mapped: set[str] = set()
        for e in visible_entries(s, ctx.workspace.id, kinds=("term", "metric", "definition"), trusted_only=True):
            mapped.update(m.lower() for m in e.mapped_columns or ())
        for fq in scope.assets:
            schema, _, name = fq.partition(".")
            asset = s.scalar(select(SourceAsset).where(SourceAsset.workspace_id == ctx.workspace.id,
                                                       SourceAsset.schema_name == schema, SourceAsset.name == name))
            if asset is None:
                continue
            allowed = set(scope.columns.get(fq) or [])
            source_id = scope.asset_sources.get(fq, asset.source_id)
            dialect = (scope.source_dialects or {}).get(source_id, "postgres")
            entity = _entity_from_table(name)
            cols = []
            for c in s.scalars(select(SourceColumn).where(SourceColumn.asset_id == asset.id).order_by(SourceColumn.ordinal)):
                if f"{fq}.{c.name}" in denied or (allowed and c.name not in allowed):
                    continue
                sem = dict(c.semantics or {})
                profile = dict(c.profile or {})
                role = sem.get("semantic_role")
                if not role:
                    role = infer_column_semantics(DiscoveredColumn(name=c.name, data_type=c.data_type)).semantic_role
                gloss = sem.get("glossary") or {}
                glossary = bool(gloss) or any(m.endswith(f"{name}.{c.name}".lower()) for m in mapped)
                tags = {str(t).lower() for t in c.tags or []}
                personal = bool(tags & {"pii", "restricted", "sensitive"}) or bool((sem.get("pii") or {}).get("is_pii")) \
                    or role == "contact"
                words = set(_words(c.name.replace("_", " ")))
                words |= _words(c.business_name, None) if c.business_name else set()
                if gloss.get("term"):
                    words |= _words(str(gloss["term"]))
                cols.append(Column(name=c.name, data_type=normalize_type(c.data_type), role=str(role or "unknown"),
                                   semantic=c.semantic_type or profile.get("semantic_type"),
                                   distinct=profile.get("distinct"), ordinal=c.ordinal, words=frozenset(words),
                                   name_words=_words(c.name.replace("_", " ")), glossary=glossary, personal=personal,
                                   label=(c.business_name or " ".join(split_tokens(c.name))).strip()))
            spec = declared.get(entity) or declared.get(name.lower())
            pack_dims = [(str(d["label"]), str(d["column"])) for d in (spec or {}).get("dimensions") or []
                         if any(c.name == d["column"] for c in cols)]
            ewords = set(_words(entity)) | set(_words(asset.business_name)) if asset.business_name else set(_words(entity))
            for noun in (spec or {}).get("nouns") or []:
                ewords |= _words(str(noun))
            tables.append(Table(fq=fq, source_id=source_id, dialect=dialect, entity=entity, entity_words=frozenset(ewords),
                                columns=cols, pack_dimensions=pack_dims, event_start=tuple(hints.event_start)))
    return tables, lex


# ------------------------------------------------------------------------------ resolution
@dataclass
class Plan:
    """What the rules will run (status answer) or ask (status clarify)."""

    status: str  # answer | clarify
    table: Table | None = None
    intent: Intent | None = None
    dims: list[tuple[str, Derivation, str]] = field(default_factory=list)  # (alias, derivation, label)
    measure: tuple[str, str, Derivation | None, str] | None = None  # (alias, agg, derivation, label)
    order: str = "dimensions"
    limit: int | None = None
    notes: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    ambiguous: dict[str, list[str]] | None = None  # phrase -> candidate columns

    def summary(self) -> dict[str, Any]:
        return {"status": self.status, "table": self.table.fq if self.table else None,
                "dimensions": [a for a, _, _ in self.dims], "measure": self.measure[:2] if self.measure else None,
                "notes": self.notes, "ambiguous": self.ambiguous}


class _Ambiguous(Exception):
    def __init__(self, phrase: str, candidates: list[Column]) -> None:
        super().__init__(phrase)
        self.phrase, self.candidates = phrase, candidates


def _match(phrase: str, columns: list[Column], table: Table, lex: Any) -> Column | None:
    """The one column a phrase names: every word of the phrase explained by the column's words
    (the table's own entity words may be dropped: "order region" on orders); an exact name wins.
    None when nothing matches; _Ambiguous when several match equally."""
    want = set(_words(phrase, lex)) - {"by"}
    if not want:
        return None
    for w in (want, want - table.entity_words):
        if not w:
            continue
        exact = [c for c in columns if w == c.name_words]
        if len(exact) == 1:
            return exact[0]
        found = exact or [c for c in columns if w <= c.words]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise _Ambiguous(phrase, found)
    return None


def _subject_table(subject: str | None, tables: list[Table], lex: Any) -> Table | None:
    """The table whose records a phrase counts ("orders", "invoices", "records" when only one)."""
    want = set(_words(subject or "", lex)) - _COUNT_NOUNS - {"by"}
    if not want:
        return tables[0] if len(tables) == 1 else None
    exact = [t for t in tables if want == set(t.entity_words) or want == set(_words(t.entity))]
    if len(exact) == 1:
        return exact[0]
    found = [t for t in tables if want <= t.entity_words]
    return found[0] if len(found) == 1 else None


def _time_column(table: Table, intent: Intent, question: str, lex: Any) -> Column:
    times = [c for c in table.columns if c.is_time]
    if not times:
        raise LookupError("no time column")
    if len(times) == 1:
        return times[0]
    asked = set(_words(question, lex)) | (set(_words(intent.verb)) if intent.verb else set())
    named = [c for c in times if (c.name_words - {"date", "time", "at", "on"}) & asked]
    if len(named) == 1:
        return named[0]
    starts = set(_START_WORDS) | set(table.event_start) | set(table.entity_words)
    started = [c for c in times if c.name_words & starts]
    if len(started) == 1:
        return started[0]
    raise _Ambiguous("time", named or started or times)


def _default_dimension(table: Table) -> tuple[Column | None, list[Column]]:
    """The documented default grouping and the other candidates (see the module docstring)."""
    by_pack = [table.column(col) for _, col in table.pack_dimensions]
    by_pack = [c for c in by_pack if c is not None and c.is_dimension]
    ranked = sorted((c for c in table.columns if c.default_candidate),
                    key=lambda c: (not c.glossary, not (c.name_words & _CATEGORICAL_WORDS), c.distinct or 0, c.ordinal))
    ordered = by_pack + [c for c in ranked if c not in by_pack]
    return (ordered[0] if ordered else None), ordered[1:]


def _alias(base: str, taken: set[str]) -> str:
    alias = base
    while alias in taken:
        alias = f"{alias}_"
    taken.add(alias)
    return alias


def _question_for(table: Table, intent: Intent, dims: list[str]) -> str:
    """A question in the rules' own wording that resolves to exactly these columns (follow-ups)."""
    by = " and ".join(dims)
    if intent.shape == "aggregate" and intent.measure:
        word = {"avg": "average", "median": "median", "sum": "total"}[intent.agg]
        return f"{word} {intent.measure} by {by}"
    return f"distribution of {table.entity} by {by}" if intent.shape == "distribution" else f"count of {table.entity} by {by}"


def resolve(intent: Intent, tables: list[Table], question: str, lex: Any = None) -> Plan | None:
    """A plan for an intent over the caller's tables, or None (fall through to generation)."""
    try:
        return _resolve(intent, tables, question, lex)
    except _Ambiguous as amb:
        table = next((t for t in tables if amb.candidates and amb.candidates[0] in t.columns), None)
        options = [c.name for c in amb.candidates]
        suggestions = []
        if table is not None and amb.phrase != "time":
            for c in amb.candidates[:SUGGESTIONS]:
                dims = [c.label.lower() if d == amb.phrase else d for d in intent.dims] or [c.label.lower()]
                if intent.shape == "aggregate" and intent.measure == amb.phrase:
                    suggestions.append(_question_for(table, Intent(shape="aggregate", agg=intent.agg,
                                                                   measure=c.label.lower()), intent.dims or ["month"]))
                else:
                    suggestions.append(_question_for(table, intent, dims))
        return Plan(status="clarify", table=table, intent=intent, ambiguous={amb.phrase: options},
                    suggestions=suggestions)
    except LookupError:
        return None


def _resolve(intent: Intent, tables: list[Table], question: str, lex: Any) -> Plan | None:
    tables = [t for t in tables if t.dialect in SUPPORTED_DIALECTS and t.columns]
    if not tables:
        return None
    plan = Plan(status="answer", intent=intent)
    subject_column: Column | None = None
    if intent.shape == "aggregate" or intent.measure:
        found = [(t, c) for t in tables for c in [_match(intent.measure or "", [c for c in t.columns if c.is_measure], t, lex)]
                 if c is not None]
        if len(found) != 1:
            if len(found) > 1:
                raise _Ambiguous(intent.measure or "", [c for _, c in found])
            return None
        table, measure_col = found[0]
        if intent.agg == "median" and table.dialect == "tsql":
            return None
    else:
        table = _subject_table(intent.subject, tables, lex)
        if table is None:
            if intent.shape != "distribution":
                return None
            hits = [(t, c) for t in tables for c in [_match(intent.subject or "", [c for c in t.columns if c.is_dimension], t, lex)]
                    if c is not None]
            if len(hits) != 1:
                if len(hits) > 1:
                    raise _Ambiguous(intent.subject or "", [c for _, c in hits])
                return None
            table, subject_column = hits[0]
        measure_col = None
    plan.table = table
    taken = set(c.name for c in table.columns)
    dim_cols: list[Column] = [subject_column] if subject_column is not None else []
    for phrase in intent.dims:
        c = _match(phrase, [c for c in table.columns if c.is_dimension], table, lex)
        if c is None:
            return None
        if c not in dim_cols:
            dim_cols.append(c)
    if len(dim_cols) > MAX_DIMENSIONS:
        return None
    others: list[Column] = []
    if intent.shape == "distribution" and not dim_cols and not intent.grain:
        chosen, others = _default_dimension(table)
        if chosen is None:
            return None
        dim_cols = [chosen]
        why = ("the domain pack declares it for " + table.entity if chosen.name in [c for _, c in table.pack_dimensions]
               else "it is the categorical column with a glossary term" if chosen.glossary
               else "it is the categorical column with the fewest values")
        plan.notes.append(f"grouped by {chosen.label.lower()} because {why}")
        plan.suggestions = [_question_for(table, intent, [c.label.lower()]) for c in others[:SUGGESTIONS]]
    if intent.shape == "top" and not dim_cols:
        return None
    for c in dim_cols:
        plan.dims.append((c.name, Derivation(column=c.name), c.label.lower()))
    if intent.grain:
        tcol = _time_column(table, intent, question, lex)
        taken_aliases = {a for a, _, _ in plan.dims}
        alias = intent.grain if intent.grain not in taken else f"{tcol.name}_{intent.grain}"
        plan.dims.append((_alias(alias, taken_aliases), Derivation(type="date_trunc", column=tcol.name, grain=intent.grain),
                          f"{intent.grain} of {tcol.label.lower()}"))
        plan.notes.append(f"time axis {tcol.name} by {intent.grain}")
    if measure_col is None and intent.agg != "count":
        return None
    if measure_col is not None and intent.agg != "count":
        prefix, word = {"avg": ("avg", "average"), "median": ("median", "median"), "sum": ("total", "total")}[intent.agg]
        plan.measure = (f"{prefix}_{measure_col.name}", intent.agg, Derivation(column=measure_col.name),
                        f"{word} {measure_col.label.lower()}")
    else:
        plan.measure = (f"{table.entity.replace(' ', '_')}_count", "count", None, f"number of {table.entity} records")
    if intent.shape == "top":
        plan.order, plan.limit = "measure_desc", intent.top
        if not intent.descending:
            return None  # "bottom N": ascending ranking is rarely what is meant without a model; fall through
    elif intent.shape in ("distribution", "count") and plan.dims and not intent.grain:
        plan.order = "measure_desc"
    return plan


def sql_for(plan: Plan) -> str:
    from analystos.skills.sqlbuild import aggregate_query

    assert plan.table is not None and plan.measure is not None
    alias, agg, d, _ = plan.measure
    return aggregate_query(plan.table.fq, plan.table.dialect, dimensions=[(a, dd) for a, dd, _ in plan.dims],
                           measure=(alias, agg, d), order=plan.order, limit=plan.limit)


def explain(plan: Plan) -> str:
    alias, agg, _, label = plan.measure  # type: ignore[misc]
    by = " and ".join(lbl for _, _, lbl in plan.dims)
    what = label[0].upper() + label[1:]
    text = f"{what}{' by ' + by if by else ''} in {plan.table.fq}"  # type: ignore[union-attr]
    if plan.limit:
        text += f", top {plan.limit}"
    notes = f" ({'; '.join(plan.notes)})" if plan.notes else ""
    return f"{text}{notes}. Built from the catalog by the Ask rules; no model call."


def chart_for(plan: Plan) -> dict[str, Any] | None:
    if not plan.dims or plan.measure is None:
        return None
    time_dim = next((a for a, d, _ in plan.dims if d.type == "date_trunc"), None)
    return {"type": "line" if time_dim else "bar", "x": time_dim or plan.dims[0][0], "y": plan.measure[0]}


def plan_for(ctx: Any, question: str) -> Plan | None:
    """The rules' plan for a question in the caller's scope, or None. Never raises: a catalog that
    cannot be read means no rule answer, and the question takes the next rung."""
    intent = parse(question)
    if intent is None or not getattr(getattr(ctx, "scope", None), "columns", None):
        return None
    try:
        tables, lex = load_tables(ctx)
    except Exception as exc:  # noqa: BLE001 - the rung is optional; generation still runs
        log.warning("ask rules: catalog unavailable: %s", exc)
        return None
    return resolve(intent, tables, question, lex)
