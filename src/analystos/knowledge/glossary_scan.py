"""Glossary and business-description suggestions from a scan (Stream E): rules find what needs a person,
a model may only fill a draft, a person decides.

Four rules propose `glossary_term` drafts into the review queue (knowledge/suggestions.py), each with the
evidence that produced it:

* **code_set**     a coded column whose profile holds a small, complete enumeration (priority 1..5,
                   status 1..8, a true/false flag): a term per code set whose definition is a skeleton
                   ("1 = ?, 2 = ?") a person fills in; labels the source declared fill it instead;
* **abbreviation** an acronym in table or column names (sla, ci, kpi, sku) from a small built-in expansion
                   list plus the workspace's domain packs (`glossary_expansions`, and `name_prefixes` such as
                   a custom-field prefix, in a pack's hints);
* **recurring**    a business noun several tables share (a group, an item two tables reference);
* **ask**          a word of an Ask question that was not answered (refused, clarified, missing input)
                   and matches no column, table, value or glossary term ("backlog", "P1", "breach").

Tables, views and columns whose description is missing, a low-confidence rule guess, or a pending model
draft get a `description_question` ("What does `u_flag2` in Incident mean?") with the best guess pre-filled.

Invariants: candidates already in the glossary (name or synonym, any store) or already decided (approved,
rejected) or pending are not proposed again; values of sensitive columns are never read, stored or sent;
nothing here writes a glossary entry or catalog text: approving a draft does (`apply_glossary_term`,
`apply_description_answer`). The optional model (`glossary_suggestion`) receives screened names, types,
table context and the profile SHAPE (values only when the workspace allows samples and the column is not
sensitive); its output is validated (known candidate keys only, screened, <= 300 chars, <= 5 synonyms).
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.logging import get_logger
from analystos.skills import catalog as cat

log = get_logger(__name__)

KIND_TERM = "glossary_term"
KIND_QUESTION = "description_question"
ORIGIN = "glossary.scan"
PURPOSE = "glossary_suggestion"
PROMPT = "glossary_suggestion.v1"

MAX_TERMS = 25  # new glossary drafts one scan queues
MAX_QUESTIONS = 20  # new description questions one scan queues
MAX_QUESTIONS_PER_TABLE = 5
MAX_ASK_TURNS = 50  # recent unanswered Ask turns one scan reads
MAX_ASK_TERMS_PER_TURN = 3
MAX_MODEL_TERMS = 15  # candidates one model call may fill
MAX_EVIDENCE = 6
DEFINITION_MAX = 300
SYNONYMS_MAX = 5
CODE_SET_MIN, CODE_SET_MAX = 2, 12
LOW_TABLE_CONFIDENCE = cat.ENRICH_CONFIDENCE
LOW_COLUMN_CONFIDENCE = cat.COLUMN_ENRICH_CONFIDENCE

# Built-in expansions: a suggestion, never a fact (a person approves it). Domain packs add acronyms.
ABBREVIATIONS: dict[str, str] = {
    "sla": "Service Level Agreement", "ola": "Operational Level Agreement", "ci": "Configuration Item",
    "cmdb": "Configuration Management Database", "mttr": "Mean Time To Resolve", "mtbf": "Mean Time Between Failures",
    "mtta": "Mean Time To Acknowledge", "itsm": "IT Service Management", "kpi": "Key Performance Indicator",
    "csat": "Customer Satisfaction", "nps": "Net Promoter Score", "crm": "Customer Relationship Management",
    "erp": "Enterprise Resource Planning", "sku": "Stock Keeping Unit", "po": "Purchase Order", "ytd": "Year To Date",
    "mtd": "Month To Date", "qtd": "Quarter To Date", "ltv": "Lifetime Value", "cogs": "Cost Of Goods Sold",
    "arr": "Annual Recurring Revenue", "mrr": "Monthly Recurring Revenue", "roi": "Return On Investment",
    "eta": "Estimated Time of Arrival", "rma": "Return Merchandise Authorization", "bom": "Bill Of Materials",
    "rfc": "Request For Change", "cab": "Change Advisory Board", "kb": "Knowledge Base", "p1": "Priority 1",
    "p2": "Priority 2", "sev": "Severity",
}
EXPANSION_MAX = 60  # a longer pack entry is an explanation (the definition), not an expansion (a synonym)
# Technical abbreviations every reader knows, or that name no business concept: never suggested.
TECHNICAL = frozenset({"id", "url", "ip", "api", "uuid", "guid", "pk", "fk", "ok", "db", "sql", "utc", "usd", "eur",
                       "gbp", "os", "ui", "ux", "cpu", "ram", "vm", "dns", "vpn", "sso", "mfa", "etl", "bi", "qa",
                       "it", "hq", "iso", "gps", "aws", "gcp", "pii", "ssn", "dob", "cvv", "tin", "iban", "vat",
                       "b2b", "b2c", "hr", "gl", "ar", "ap", "fx", "pos", "upc", "ean", "isbn", "utm", "seo"})
# Words that name nothing on their own: a recurring phrase made only of these is not a business noun.
GENERIC = frozenset({"id", "sys", "name", "number", "no", "description", "short", "long", "updated", "created",
                     "state", "status", "type", "active", "date", "time", "on", "at", "by", "count", "code", "key",
                     "group", "user", "value", "flag", "comment", "comments", "notes", "class", "label", "display",
                     "record", "item", "category", "level", "start", "end", "open", "opened", "closed", "resolved"})
# Words of an analytics question that are not business terms (counting, time, ordering, chart words).
_QUESTION_WORDS_TEXT = """
a about above across after again against all also am among an and any are as at average avg be been before below
between biggest both breakdown broken but by can chart compare compared comparison could count counts daily data day
days did do does doing down during each else every few fewest for from get give go got graph group grouped had has
have having highest how i if in including into is it its last least less list longest lowest many max maximum me mean
median min minimum month monthly months more most much my new next no not number numbers of off old on only or other
our out over overall per percent percentage plot previous quarter quarterly rate ratio rows see share show shortest
since so some split sum than that the their them then there these they this those through to top total trend trends
under up us versus vs was week weekly weeks were what when where which while who why will with within without year
yearly years yesterday today you your please tell display draw give find there's what's how's
jan feb mar apr may jun jul aug sep sept oct nov dec january february march april june july august september october
november december monday tuesday wednesday thursday friday saturday sunday happen happened happening take takes
taking took long longer faster slower still currently current being going
"""
QUESTION_WORDS = frozenset(_QUESTION_WORDS_TEXT.split())
_CODE_WORD = re.compile(r"^[a-z]{1,3}\d{1,2}$")
_QUESTION_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
_INT_LIKE = re.compile(r"^-?\d{1,4}$")
_SHORT_CODE = re.compile(r"^[A-Za-z0-9]{1,3}$")
_SKIP_ASK_REFUSALS = frozenset({"no_scope", "policy_denied"})  # nothing about the words: access or setup


# ------------------------------------------------------------------------------------ candidates
@dataclass
class Candidate:
    """One proposed glossary term before it becomes a draft. `key` is its normalised name (the dedupe key)."""

    key: str
    name: str
    body: str
    rule: str
    placeholder: bool
    confidence: float
    question: str
    synonyms: list[str] = field(default_factory=list)
    mapped_columns: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    body_source: str = "rule"
    model_hint: dict[str, Any] = field(default_factory=dict)  # screened shape the optional model may see

    def merge(self, other: Candidate) -> None:
        """Same term found by another rule: keep the better definition, pool evidence and columns."""
        if self.placeholder and not other.placeholder:
            self.body, self.placeholder, self.body_source = other.body, False, other.body_source
            self.confidence = other.confidence
        for s in other.synonyms:
            if norm(s) not in {norm(x) for x in self.synonyms} and norm(s) != self.key:
                self.synonyms.append(s)
        for m in other.mapped_columns:
            if m not in self.mapped_columns:
                self.mapped_columns.append(m)
        for e in other.evidence:
            if e not in self.evidence and len(self.evidence) < MAX_EVIDENCE:
                self.evidence.append(e)
        if not self.model_hint:
            self.model_hint = other.model_hint


def norm(text: Any) -> str:
    """Normalised term text: lower case, words only (so "Is Active", "is_active" and "is-active" are one term)."""
    return " ".join(cat.split_tokens(str(text or "")))


def _stem_set(text: str) -> set[str]:
    return {cat._stem(t) for t in cat.split_tokens(text) if t not in cat._STOP}


def _column_sensitive(col: dict[str, Any]) -> bool:
    from analystos.skills.profiling import column_is_sensitive

    return bool(col.get("sensitive")) or column_is_sensitive(col.get("tags"), col.get("semantics")) \
        or cat.classify_pii(str(col.get("name") or ""), str(col.get("data_type") or "text")).category is not None


def _table_label(asset: dict[str, Any]) -> str:
    return str(asset.get("business_name") or cat.humanize(cat.split_tokens(asset["name"])))


def _column_label(col: dict[str, Any]) -> str:
    return str(col.get("business_name") or cat.humanize(cat.split_tokens(col["name"])))


def _where(asset: dict[str, Any], col: dict[str, Any] | None = None) -> str:
    return f"{asset['name']}.{col['name']}" if col else str(asset["name"])


def _fq(asset: dict[str, Any], col: dict[str, Any]) -> str:
    return f"{asset['schema']}.{asset['name']}.{col['name']}"


def explained(col: dict[str, Any]) -> bool:
    """A column a person, its source or an accepted draft already explains: never asked about again."""
    return col.get("description_origin") in ("user", "source", "model") or bool((col.get("semantics") or {}).get("reviewed"))


def _coded_role(col: dict[str, Any]) -> bool:
    """A column whose role may carry codes: a dimension, flag, code or a name the rules did not understand;
    never a key, a time, free text, a count or an amount."""
    sem = col.get("semantics") or {}
    role, conf = str(sem.get("semantic_role") or "unknown"), float(sem.get("confidence") or 0.0)
    return not (role in ("identifier", "foreign_key", "timestamp", "date", "text", "name", "contact", "geo", "amount",
                         "percent", "duration") or (role == "measure" and (sem.get("unit") == "count" or conf >= 0.5)))


def _int(v: Any) -> int | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else None


def _contiguous_codes(prof: dict[str, Any]) -> list[str] | None:
    """An integer column whose distinct count fills its whole range (1..5 with 5 distinct values): the codes are
    exactly min..max, known from the profile without reading a value."""
    lo, hi, n = _int(prof.get("min")), _int(prof.get("max")), prof.get("distinct")
    if lo is None or hi is None or not isinstance(n, int) or not CODE_SET_MIN <= n <= CODE_SET_MAX:
        return None
    return [str(v) for v in range(lo, hi + 1)] if hi - lo + 1 == n else None


def needs_code_query(col: dict[str, Any]) -> bool:
    """An integer column with a few distinct codes whose values the profile does not hold (gaps: 1, 2, 3, 6, 7, 8):
    the crawler reads its distinct values through the gateway (`profile.code_values`). Never a sensitive column."""
    prof = col.get("profile") or {}
    if _column_sensitive(col) or not _coded_role(col) or cat.normalize_type(col.get("data_type")) not in ("integer", "bigint"):
        return False
    if prof.get("code_values") or prof.get("values_complete") or _contiguous_codes(prof) is not None:
        return False
    n = prof.get("distinct")
    return isinstance(n, int) and CODE_SET_MIN <= n <= CODE_SET_MAX and _int(prof.get("min")) is not None


def _code_values(col: dict[str, Any]) -> list[str] | None:
    """The complete, coded value list of a non-sensitive column, or None. Coded: integers, booleans or short
    codes (<= 3 characters); words ("hardware", "Phone") describe themselves and are not a code set. Integer
    codes come from the governed distinct read (`code_values`) or a range the distinct count fills."""
    if _column_sensitive(col) or not _coded_role(col):
        return None
    prof = col.get("profile") or {}
    if cat.normalize_type(col.get("data_type")) == "boolean":
        return ["true", "false"]
    values = prof.get("values") if prof.get("values_complete") else None
    if values is None and isinstance(prof.get("code_values"), list):
        values = prof["code_values"]
    if values is None and cat.normalize_type(col.get("data_type")) in ("integer", "bigint", "numeric"):
        values = _contiguous_codes(prof)
    if not isinstance(values, list) or not CODE_SET_MIN <= len(values) <= CODE_SET_MAX:
        return None
    texts = [str(v).strip() for v in values if v is not None and str(v).strip() != ""]
    if len(texts) < CODE_SET_MIN:
        return None
    if all(_INT_LIKE.match(t) for t in texts):
        return sorted(dict.fromkeys(texts), key=lambda t: int(t))
    if all(t.lower() in ("true", "false") for t in texts):
        return ["true", "false"]
    if all(_SHORT_CODE.match(t) for t in texts):
        return sorted(dict.fromkeys(texts))
    return None


def _declared_labels(col: dict[str, Any]) -> dict[str, str]:
    """Labels a source declared for coded values (a ServiceNow choice list, a dictionary), if the crawler kept any."""
    for holder in (col.get("semantics") or {}, col.get("profile") or {}):
        for key in ("value_labels", "choices", "choice_labels"):
            labels = holder.get(key)
            if isinstance(labels, dict) and labels:
                return {str(k): cat.screen_text(str(v), max_chars=60) for k, v in labels.items() if str(v).strip()}
    return {}


def _span(values: list[str]) -> str:
    if len(values) > 2 and all(_INT_LIKE.match(v) for v in values):
        ints = [int(v) for v in values]
        if ints == list(range(ints[0], ints[-1] + 1)):
            return f"{ints[0]}–{ints[-1]}"
    return ", ".join(values)


def code_set_candidates(assets: list[dict[str, Any]]) -> list[Candidate]:
    """(a) A term per code set. The same column name with the same codes in several tables is one term;
    different codes under one name (orders.status vs returns.status) are one term per table."""
    found: dict[str, list[tuple[dict[str, Any], dict[str, Any], list[str]]]] = {}
    for a in assets:
        for c in a.get("columns") or []:
            values = None if explained(c) else _code_values(c)
            if values:
                found.setdefault(c["name"].lower(), []).append((a, c, values))
    out: list[Candidate] = []
    for _, hits in sorted(found.items()):
        same = len({tuple(v) for _, _, v in hits}) == 1
        groups = [hits] if same else [[h] for h in hits]
        for group in groups:
            a, c, values = group[0]
            name = _column_label(c) if same else f"{_table_label(a)} {_column_label(c)}"
            labels = _declared_labels(c)
            boolean = values == ["true", "false"]
            if labels and all(v in labels for v in values):
                body = f"{name}: " + "; ".join(f"{v} = {labels[v]}" for v in values) + "."
                placeholder, confidence, source = False, 0.7, "source"
            elif boolean:
                body = f"{name} is true when … (true = ?; false = ?)"
                placeholder, confidence, source = True, 0.2, "rule"
            else:
                body = f"{name} codes: " + "; ".join(f"{v} = ?" for v in values) + "."
                placeholder, confidence, source = True, 0.2, "rule"
            where = ", ".join(_where(x, y) for x, y, _ in group)
            question = (f"When is “{name}” true, and what does it mean for the business?" if boolean
                        else f"What do the {name} codes {_span(values)} mean?")
            out.append(Candidate(
                key=norm(name), name=name, body=body, rule="code_set", placeholder=placeholder, confidence=confidence,
                question=question, mapped_columns=[_fq(x, y) for x, y, _ in group], body_source=source,
                evidence=[{"kind": "code_set", "where": _where(x, y), "values": v if len(v) <= CODE_SET_MAX else v[:CODE_SET_MAX],
                           "detail": f"{len(v)} values: {_span(v)}"} for x, y, v in group][:MAX_EVIDENCE],
                model_hint={"found_in": [{"table": cat.screen_text(_table_label(x), max_chars=80),
                                          "column": cat.screen_text(y["name"], max_chars=64),
                                          "type": cat.normalize_type(y.get("data_type"))} for x, y, _ in group][:4],
                            "shape": {"distinct_values": len(values), "boolean": boolean},
                            "_values": values, "where": where}))
    return out


def abbreviation_candidates(assets: list[dict[str, Any]], extra: dict[str, str] | None = None, *,
                            expansions: dict[str, str] | None = None,
                            prefixes: dict[str, str] | None = None) -> list[Candidate]:
    """(b) Acronyms in table and column names that the built-in list or a domain pack knows. `extra` are a pack's
    acronyms (abbreviation -> how it is written), `expansions` its glossary expansions or explanations, and
    `prefixes` column-name prefixes with their explanation (a custom-field convention of the source system)."""
    pack = {k.lower(): str(v) for k, v in (expansions or {}).items()}
    known = {**{k.lower(): "" for k in (extra or {})}, **ABBREVIATIONS, **pack}
    marks = {p.lower(): str(v) for k, v in (prefixes or {}).items() if (p := str(k).strip())}
    hits: dict[str, list[str]] = {}
    for a in assets:
        places = [(str(a["name"]), None)] + [(str(c["name"]), c) for c in a.get("columns") or []]
        for text, col in places:
            where = _where(a, col)
            for p in marks:
                if col is not None and text.lower().startswith(p):
                    hits.setdefault(f"prefix:{p}", []).append(where)
            for t in dict.fromkeys(cat.split_tokens(text)):
                if t in known and t not in TECHNICAL:
                    hits.setdefault(t, []).append(where)
    out: list[Candidate] = []
    for abbr, places in sorted(hits.items()):
        places = list(dict.fromkeys(places))
        evidence = [{"kind": "abbreviation", "where": w, "detail": f"“{abbr.removeprefix('prefix:')}” in the name {w}"}
                    for w in places[:MAX_EVIDENCE]]
        if abbr.startswith("prefix:"):
            p = abbr.removeprefix("prefix:")
            out.append(Candidate(key=norm(f"{p} prefix"), name=f"{p} prefix", body=marks[p], rule="abbreviation",
                                 placeholder=False, confidence=0.5, evidence=evidence,
                                 question=f"Is this what the {p} prefix means here, and who owns these fields?"))
            continue
        expansion = known.get(abbr) or ""
        upper = str((extra or {}).get(abbr) or abbr.upper())
        explanation = len(expansion) > EXPANSION_MAX
        if explanation:
            body, placeholder, conf = expansion, False, 0.5
        elif expansion:
            body, placeholder, conf = f"{upper}: {expansion}.", False, 0.5
        else:
            body, placeholder, conf = f"{upper} stands for … (it is used in {', '.join(places[:3])}).", True, 0.2
        out.append(Candidate(key=abbr, name=upper, body=body, rule="abbreviation", placeholder=placeholder, confidence=conf,
                             synonyms=[expansion] if expansion and not explanation else [],
                             evidence=evidence, question=f"What does {upper} mean in your business?",
                             model_hint={"found_in": [{"name": cat.screen_text(w, max_chars=100)} for w in places[:4]],
                                         "shape": {"abbreviation": upper}}))
    return out


def recurring_candidates(assets: list[dict[str, Any]]) -> list[Candidate]:
    """(c) Business nouns several tables share: a reference column's name (owner_group, product_ref) or a
    referenced table's business name, found in two or more tables. Generic words alone never count."""
    labels = {str(a["name"]).lower(): _table_label(a) for a in assets}
    seen: dict[str, dict[str, Any]] = {}

    def add(phrase: str, table: str, where: str, column: str | None) -> None:
        toks = cat.split_tokens(phrase)
        if not toks or all(t in GENERIC or t.isdigit() for t in toks) or len(phrase) < 4:
            return
        entry = seen.setdefault(norm(phrase), {"name": phrase, "tables": set(), "where": [], "columns": []})
        entry["tables"].add(table)
        if where not in entry["where"]:
            entry["where"].append(where)
        if column and column not in entry["columns"]:
            entry["columns"].append(column)

    for a in assets:
        for c in a.get("columns") or []:
            sem = c.get("semantics") or {}
            ref = str((c.get("profile") or {}).get("references") or c.get("references") or "")
            if sem.get("semantic_role") != "foreign_key" and not ref:
                continue
            if _column_sensitive(c):
                continue
            label = _column_label(c)
            if norm(label).endswith(" id"):
                label = cat.humanize(cat.split_tokens(label)[:-1])
            add(label, a["name"], _where(a, c), _fq(a, c))
            target = ref.rsplit(".", 1)[0].split(".")[-1].lower() if ref else ""
            if target and target in labels:
                add(labels[target], a["name"], _where(a, c), None)
    out: list[Candidate] = []
    for key, e in sorted(seen.items()):
        if len(e["tables"]) < 2:
            continue
        tables = sorted(e["tables"])
        name = str(e["name"])[:1].upper() + str(e["name"])[1:]
        out.append(Candidate(
            key=key, name=name, body=f"{_article(name)} {name.lower()} is … (it links {', '.join(tables)}).", rule="recurring",
            placeholder=True, confidence=0.2, question=f"What is a “{name.lower()}” in your business?",
            mapped_columns=e["columns"][:MAX_EVIDENCE],
            evidence=[{"kind": "recurring", "where": w, "detail": f"used by {len(tables)} tables"} for w in e["where"][:MAX_EVIDENCE]],
            model_hint={"found_in": [{"name": cat.screen_text(w, max_chars=100)} for w in e["where"][:4]],
                        "shape": {"tables": len(tables)}}))
    return out


def _article(word: str) -> str:
    return "An" if word[:1].lower() in "aeiou" else "A"


# ------------------------------------------------------------------------------------ Ask
def vocabulary(assets: list[dict[str, Any]], known: Iterable[str] = ()) -> set[str]:
    """Stems a question word may match without being unknown: table and column names and business names,
    the complete value lists of non-sensitive columns, and glossary names and synonyms."""
    words: set[str] = set()
    for a in assets:
        words |= _stem_set(str(a["name"])) | _stem_set(str(a.get("business_name") or ""))
        for c in a.get("columns") or []:
            words |= _stem_set(str(c["name"])) | _stem_set(str(c.get("business_name") or ""))
            prof = c.get("profile") or {}
            if prof.get("values_complete") and not _column_sensitive(c):
                for v in prof.get("values") or []:
                    words |= _stem_set(str(v))
    for k in known:
        words |= _stem_set(k)
    return words


def ask_terms(question: str, vocab: set[str], missing: Iterable[str] = ()) -> list[str]:
    """(d) Terms of an unanswered question that match nothing the platform knows: code-like words (P1, Sev2),
    the phrases a refusal named as missing, and content words of four letters or more. At most a few."""
    out: list[str] = []

    def keep(term: str) -> None:
        t = " ".join(term.split())
        if t and norm(t) not in {norm(x) for x in out} and len(out) < MAX_ASK_TERMS_PER_TURN:
            out.append(t)

    for m in missing:
        m = str(m or "").strip()
        if m and m.lower() not in ("what to measure",) and not (_stem_set(m) <= vocab) and len(m) <= 60:
            keep(m)
    for raw in _QUESTION_TOKEN.findall(question or ""):
        low = raw.lower()
        if low in QUESTION_WORDS or low.isdigit():
            continue
        if _CODE_WORD.match(low):
            if low not in vocab:
                keep(raw.upper())
            continue
        if len(low) < 4 or cat._stem(low) in vocab or low in vocab or low in cat._STOP:
            continue
        if any(ch.isdigit() for ch in low):
            continue
        keep(low)
    return out


def ask_candidates(turns: list[dict[str, Any]], vocab: set[str]) -> list[Candidate]:
    """A "Define X?" draft per unknown term of recent unanswered Ask turns, citing the question. Values that
    identify a person or an account (e-mails, account numbers) are redacted before anything is read from it."""
    from analystos.llm.redaction import PLACEHOLDER_PREFIX, redact_question

    out: dict[str, Candidate] = {}
    for t in turns:
        if t.get("status") == "answered":
            continue
        refusal = t.get("refusal") or {}
        if refusal.get("kind") in _SKIP_ASK_REFUSALS:
            continue
        details = refusal.get("details") if isinstance(refusal.get("details"), dict) else refusal
        missing = [m.get("name") for m in (details.get("missing") or []) if isinstance(m, dict)]
        redacted = redact_question(str(t.get("question") or "")).text
        shown = cat.screen_text(redacted, max_chars=160)
        plain = re.sub(rf"{PLACEHOLDER_PREFIX}\d+", " ", redacted)
        for term in ask_terms(plain, vocab, missing):
            key = norm(term)
            if not key:
                continue
            ev = {"kind": "ask", "question": shown, "turn_id": t.get("id"), "status": t.get("status"),
                  "detail": f"asked in Ask: “{shown}”"}
            cand = Candidate(key=key, name=term, body=f"“{term}” means … (which records, which condition).", rule="ask",
                             placeholder=True, confidence=0.1, question=f"Define “{term}”? It was used in a question "
                                                                          "the platform could not answer.",
                             evidence=[ev], model_hint={"asked": [shown]})
            if key in out:
                out[key].merge(cand)
            else:
                out[key] = cand
    return list(out.values())


# ------------------------------------------------------------------------------------ dedupe
def known_names(entries: Iterable[Any]) -> set[str]:
    """Normalised names and synonyms of glossary entries, plus a parenthesised abbreviation ("Priority 1 (P1)")."""
    out: set[str] = set()
    for e in entries:
        name = str(getattr(e, "name", None) or (e.get("name") if isinstance(e, dict) else "") or "")
        syns = getattr(e, "synonyms", None) if not isinstance(e, dict) else e.get("synonyms")
        for label in [name, *[str(s) for s in syns or ()]]:
            if norm(label):
                out.add(norm(label))
            for inner in re.findall(r"\(([^)]{1,40})\)", label):
                if norm(inner):
                    out.add(norm(inner))
    return out


def dedupe(cands: Iterable[Candidate], known: set[str], decided: set[str]) -> tuple[list[Candidate], dict[str, int]]:
    """One candidate per normalised name, merged across rules; drop those whose name or a synonym (an
    expansion) is already a glossary name or synonym, and those already decided or pending (`decided`)."""
    merged: dict[str, Candidate] = {}
    for c in cands:
        if not c.key:
            continue
        if c.key in merged:
            merged[c.key].merge(c)
        else:
            merged[c.key] = c
    for c in [c for c in merged.values() if c.rule == "abbreviation"]:
        # "CI" whose expansion is another candidate ("Configuration item"): one term, the acronym a synonym
        target = next((merged[norm(s)] for s in c.synonyms if norm(s) in merged and norm(s) != c.key), None)
        if target is not None:
            target.merge(Candidate(key=target.key, name=target.name, body="", rule=c.rule, placeholder=True,
                                   confidence=0.0, question="", synonyms=[c.name], evidence=c.evidence))
            del merged[c.key]
    kept: list[Candidate] = []
    skipped = {"known": 0, "decided": 0}
    for c in merged.values():
        if c.key in known or any(norm(s) in known for s in c.synonyms):
            skipped["known"] += 1
        elif subject_for(c.key) in decided:
            skipped["decided"] += 1
        else:
            kept.append(c)
    order = {"ask": 0, "code_set": 1, "recurring": 2, "abbreviation": 3}
    kept.sort(key=lambda c: (order.get(c.rule, 9), c.key))
    return kept, skipped


def subject_for(key: str) -> str:
    from analystos.knowledge.entries import slugify

    return f"glossary:{slugify(key, 120)}"


# ------------------------------------------------------------------------------------ descriptions
def description_questions(assets: list[dict[str, Any]], drafts: dict[str, dict[str, Any]] | None = None,
                          *, per_table: int = MAX_QUESTIONS_PER_TABLE) -> list[dict[str, Any]]:
    """Tables, views and columns a person should describe: no description, a low-confidence rule guess, or a
    pending model draft (`drafts`: subject -> {"description", "suggestion_id"}). Owner, source, reviewed and
    accepted text is never asked about again. Each: {subject, target, asset_id, column?, title, question, guess,
    guess_origin, draft_id?, evidence}."""
    drafts = drafts or {}
    out: list[dict[str, Any]] = []
    for a in assets:
        sem = a.get("semantics") or {}
        subject = f"asset:{a['id']}"
        noun = "view" if a.get("kind") == "view" else "table"
        label = _table_label(a)
        if not a.get("reviewed") and a.get("description_origin") not in ("user", "source", "model"):
            draft = drafts.get(subject)
            desc = a.get("description")
            low = float(sem.get("confidence") or 0.0) < LOW_TABLE_CONFIDENCE or sem.get("role") in (None, "unknown")
            if draft or not desc or cat.is_placeholder_description(desc, table_name=a["name"]) \
                    or (a.get("description_origin") == "rule" and low):
                guess, origin = (draft["description"], "model") if draft else (desc or "", "rule" if desc else "none")
                out.append({"subject": subject, "target": "asset", "asset_id": a["id"], "title": f"Describe the {noun} {label}",
                            "question": f"What does the {noun} “{label}” ({a['name']}) hold? One or two sentences: what one "
                                        "row is and what it is used for.",
                            "guess": guess, "guess_origin": origin, "draft_id": (draft or {}).get("suggestion_id"),
                            "evidence": [{"kind": "description", "where": a["name"],
                                          "detail": "a model draft is waiting" if draft else
                                          ("no description" if not desc else "the rule guess has low confidence")}]})
        gaps: list[tuple[int, int, dict[str, Any], dict[str, Any] | None]] = []
        for i, c in enumerate(a.get("columns") or []):
            csem = c.get("semantics") or {}
            if c.get("description_origin") in ("user", "source", "model") or csem.get("reviewed"):
                continue
            draft = drafts.get(f"column:{a['id']}:{c['name']}")
            desc = c.get("description")
            unsure = float(csem.get("confidence") or 0.0) < LOW_COLUMN_CONFIDENCE or not csem.get("semantic_role")
            if not (draft or not desc or unsure):
                continue
            if csem.get("semantic_role") in ("identifier",) and not draft:
                continue
            # the columns a person is most needed for first: a waiting draft, then what the rules did not understand
            gaps.append((0 if draft else 1 if unsure else 2, i, c, draft))
        for _, _, c, draft in sorted(gaps, key=lambda g: (g[0], g[1]))[:per_table]:
            csubject = f"column:{a['id']}:{c['name']}"
            desc = c.get("description")
            guess, origin = (draft["description"], "model") if draft else (desc or "", "rule" if desc else "none")
            out.append({"subject": csubject, "target": "column", "asset_id": a["id"], "column": c["name"],
                        "title": f"Describe {a['name']}.{c['name']}",
                        "question": f"What does “{c['name']}” in {label} mean?",
                        "guess": guess, "guess_origin": origin, "draft_id": (draft or {}).get("suggestion_id"),
                        "evidence": [{"kind": "description", "where": _where(a, c),
                                      "detail": "a model draft is waiting" if draft else
                                      ("no description" if not desc else "the name rules did not understand this column")}]})
    return out


# ------------------------------------------------------------------------------------ model assist
def model_payload(cands: list[Candidate], *, allow_values: bool) -> list[dict[str, Any]]:
    """What the optional model sees: screened names, types, table context and the profile shape. Code values
    only when the workspace policy allows samples (a code set never comes from a sensitive column)."""
    out = []
    for c in cands[:MAX_MODEL_TERMS]:
        hint = {k: v for k, v in c.model_hint.items() if not k.startswith("_") and k != "where"}
        item = {"key": c.key, "name": cat.screen_text(c.name, max_chars=80), "rule": c.rule, **hint}
        if allow_values and c.model_hint.get("_values"):
            item["values"] = [cat.screen_text(str(v), max_chars=20) for v in c.model_hint["_values"]][:CODE_SET_MAX]
        out.append(item)
    return out


def validate_model_output(data: Any, keys: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Model output -> {key: {definition, synonyms, confidence}}: only keys that were asked, screened text of at
    most 300 characters (instruction-like text refused), at most five short synonyms, confidence <= 0.7."""
    allowed = set(keys)
    out: dict[str, dict[str, Any]] = {}
    items = data.get("terms") if isinstance(data, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("key") not in allowed or item["key"] in out:
            continue
        raw = str(item.get("definition") or "")
        if cat.has_injection(raw):
            continue
        definition = cat.screen_text(raw, max_chars=DEFINITION_MAX)
        if not definition or cat.is_placeholder_description(definition) or definition.strip()[-1:] == "?":
            continue
        synonyms: list[str] = []
        for s in item.get("synonyms") if isinstance(item.get("synonyms"), list) else []:
            t = cat.screen_text(str(s), max_chars=60)
            if t and not cat.has_injection(str(s)) and norm(t) != item["key"] and norm(t) not in {norm(x) for x in synonyms}:
                synonyms.append(t)
            if len(synonyms) >= SYNONYMS_MAX:
                break
        try:
            conf = min(0.7, max(0.0, float(item.get("confidence"))))
        except (TypeError, ValueError):
            conf = 0.5
        out[item["key"]] = {"definition": definition, "synonyms": synonyms, "confidence": round(conf, 4)}
    return out


def _model_fill(router: Any, workspace_id: str, cands: list[Candidate], *, allow_values: bool) -> dict[str, Any]:
    """One bounded call for the candidates rules could not define; avoided calls are recorded as savings."""
    from analystos.agents.prompts import prompt, prompt_version_id
    from analystos.core.errors import AnalystOSError
    from analystos.llm.cache import estimate_tokens
    from analystos.runtime.context import workspace_call_ctx

    todo = [c for c in cands if c.placeholder][:MAX_MODEL_TERMS]
    report: dict[str, Any] = {"called": False, "filled": 0}
    if not todo or router is None:
        report["skipped"] = "nothing to fill" if not todo else "no router"
        return report
    system = prompt(PROMPT)
    version = prompt_version_id(PROMPT, system)
    payload = json.dumps({"terms": model_payload(todo, allow_values=allow_values)}, separators=(",", ":"), sort_keys=True)
    ctx = workspace_call_ctx(workspace_id, agent_id="catalog_steward", prompt_version=version)
    mode = router.mode(PURPOSE)
    if mode == "off" or not router.available(PURPOSE, ctx):
        router.record_skip(PURPOSE, ctx, estimated_tokens=estimate_tokens(system + payload) + 300,
                           reason=f"mode={mode}: glossary drafts kept as questions for a person")
        report["skipped"] = "mode off" if mode == "off" else "no model available"
        return report
    try:
        resp = router.complete_json(PURPOSE, system, payload, ctx=ctx, max_tokens=1500)
    except AnalystOSError as exc:
        report["skipped"] = f"model call failed: {exc.message[:120]}"
        return report
    report["called"] = True
    model = str(getattr(resp, "model", None) or "unknown")
    answers = validate_model_output(getattr(resp, "data", None), [c.key for c in todo])
    for c in todo:
        a = answers.get(c.key)
        if not a:
            continue
        c.body, c.placeholder, c.body_source, c.confidence = a["definition"], False, "model", a["confidence"]
        c.model_hint = {**c.model_hint, "_model": model, "_version": version}
        for s in a["synonyms"]:
            if norm(s) not in {norm(x) for x in c.synonyms}:
                c.synonyms.append(s)
        report["filled"] += 1
    return report


def _record_avoided(router: Any, workspace_id: str, todo: list[Candidate]) -> None:
    """Model assist was not requested: the skeletons wait for a person, and the avoided call shows as savings."""
    from analystos.llm.cache import estimate_tokens
    from analystos.runtime.context import workspace_call_ctx

    try:
        text = json.dumps(model_payload(todo, allow_values=False), separators=(",", ":"))
        router.record_skip(PURPOSE, workspace_call_ctx(workspace_id, agent_id="catalog_steward", prompt_version=PROMPT),
                           estimated_tokens=estimate_tokens(text) + 300,
                           reason=f"rules: {len(todo)} glossary drafts wait for a person")
    except Exception as exc:  # noqa: BLE001 - accounting never fails a scan
        log.info("glossary skip not recorded: %s", type(exc).__name__)


# ------------------------------------------------------------------------------------ DB side
def _assets(session: Session, workspace_id: str, asset_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    from analystos.db.models import SourceAsset, SourceColumn
    from analystos.skills.profiling import column_is_sensitive

    q = select(SourceAsset).where(SourceAsset.workspace_id == workspace_id, SourceAsset.lifecycle == "active")
    ids = list(asset_ids) if asset_ids is not None else None
    if ids is not None:
        q = q.where(SourceAsset.id.in_(ids))
    rows = list(session.scalars(q.order_by(SourceAsset.schema_name, SourceAsset.name)))
    if any(a.selected for a in rows):
        rows = [a for a in rows if a.selected]  # the tables in use; unselected ones are metadata only
    by_asset: dict[str, list[Any]] = {a.id: [] for a in rows}
    if by_asset:
        for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id.in_(list(by_asset)))
                                 .order_by(SourceColumn.asset_id, SourceColumn.ordinal)):
            by_asset[c.asset_id].append(c)
    out = []
    for a in rows:
        cols = []
        for c in by_asset[a.id]:
            sensitive = column_is_sensitive(c.tags, c.semantics)
            cols.append({"name": c.name, "data_type": c.data_type, "business_name": c.business_name,
                         "description": c.description, "description_origin": c.description_origin,
                         "semantics": c.semantics or {}, "tags": c.tags or [], "sensitive": sensitive,
                         # a sensitive column's profile never leaves this function
                         "profile": {} if sensitive else (c.profile or {})})
        out.append({"id": a.id, "schema": a.schema_name, "name": a.name, "kind": a.kind, "business_name": a.business_name,
                    "description": a.description, "description_origin": a.description_origin, "reviewed": a.reviewed,
                    "semantics": a.semantics or {}, "columns": cols})
    return out


def _subjects(session: Session, workspace_id: str, prefix: str, statuses: Iterable[str]) -> set[str]:
    from analystos.db.models import KnowledgeSuggestion

    return set(session.scalars(select(KnowledgeSuggestion.subject).where(
        KnowledgeSuggestion.workspace_id == workspace_id, KnowledgeSuggestion.subject.like(f"{prefix}%"),
        KnowledgeSuggestion.status.in_(list(statuses)))))


def _pending_drafts(session: Session, workspace_id: str) -> dict[str, dict[str, Any]]:
    from analystos.db.models import KnowledgeSuggestion

    out: dict[str, dict[str, Any]] = {}
    for r in session.scalars(select(KnowledgeSuggestion).where(
            KnowledgeSuggestion.workspace_id == workspace_id, KnowledgeSuggestion.status == "pending",
            KnowledgeSuggestion.kind.in_(["table_description", "column_description"]))):
        desc = ((r.fields or {}).get("description") or {}).get("value")
        if desc:
            out[r.subject] = {"description": str(desc), "suggestion_id": r.id}
    return out


def _pack_vocabulary(session: Session, workspace_id: str, assets: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """What the domain packs this workspace uses (policy `domain_packs`, else those whose tables match) add to the
    abbreviation rule: acronyms, glossary expansions (or explanations) and name prefixes from each pack's hints."""
    from analystos.capabilities import packs
    from analystos.governance.policy import get_workspace, load_policy

    out: dict[str, dict[str, str]] = {"acronyms": {}, "expansions": {}, "prefixes": {}}
    try:
        policy = load_policy(session, get_workspace(session, workspace_id))
        enabled = packs.enabled_for([a["name"] for a in assets], [c["name"] for a in assets for c in a["columns"]],
                                    getattr(policy, "domain_packs", None))
        out["acronyms"] = dict(packs.merge_hints(enabled).acronyms)
        for p in enabled:
            for key, name in (("expansions", "glossary_expansions"), ("prefixes", "name_prefixes")):
                value = (p.hints or {}).get(name)
                if isinstance(value, dict):
                    out[key].update({str(k): str(v) for k, v in value.items() if str(v).strip()})
    except Exception as exc:  # noqa: BLE001 - vocabulary is best effort; the built-in list still applies
        log.info("domain pack vocabulary unavailable: %s", type(exc).__name__)
    return out


def _samples_allowed(session: Session, workspace_id: str) -> bool:
    from analystos.governance.policy import get_workspace, load_policy

    try:
        return bool(load_policy(session, get_workspace(session, workspace_id)).send_data_samples_to_models)
    except Exception:  # noqa: BLE001 - the safe default: no values
        return False


def _recent_turns(session: Session, workspace_id: str, limit: int = MAX_ASK_TURNS) -> list[dict[str, Any]]:
    from analystos.db.models import AskTurn

    rows = session.scalars(select(AskTurn).where(AskTurn.workspace_id == workspace_id,
                                                 AskTurn.status.in_(["clarify", "needs_input", "refused"]))
                           .order_by(AskTurn.created_at.desc(), AskTurn.id.desc()).limit(limit))
    return [{"id": t.id, "question": t.question, "status": t.status, "refusal": t.refusal or {}} for t in rows]


def _draft_fields(c: Candidate, batch: str | None) -> dict[str, Any]:
    from analystos.knowledge.suggestions import field

    rule = {"source": "rule", "rule": c.rule, "scan": batch}
    if c.body_source == "model":
        body_prov = {"source": "model", "model": c.model_hint.get("_model"), "purpose": PURPOSE,
                     "prompt_version": c.model_hint.get("_version"), "rule": c.rule}
    elif c.body_source == "source":
        body_prov = {"source": "source", "rule": c.rule, "evidence": "labels declared by the source"}
    else:
        body_prov = rule
    return {"name": field(c.name, 0.6, **rule),
            "body": field(c.body, c.confidence, **body_prov),
            "synonyms": field(list(c.synonyms), c.confidence if c.synonyms else 0.5,
                              **(body_prov if c.body_source == "model" and c.synonyms else rule)),
            "mapped_columns": field(list(c.mapped_columns), 0.9 if c.mapped_columns else 0.5, **rule),
            "evidence": field(list(c.evidence), 1.0, **rule),
            "question": field(c.question, 1.0, **rule),
            "placeholder": field(bool(c.placeholder), 1.0, **rule)}


def _question_fields(q: dict[str, Any], batch: str | None) -> dict[str, Any]:
    from analystos.knowledge.suggestions import field

    rule = {"source": "rule", "rule": "description_gap", "scan": batch}
    guess_prov = ({"source": "model", "suggestion_id": q.get("draft_id")} if q["guess_origin"] == "model"
                  else {"source": q["guess_origin"], "rule": "catalog"})
    return {"question": field(q["question"], 1.0, **rule),
            "description": field(q["guess"] or "", 0.5 if q["guess_origin"] == "model" else 0.2, **guess_prov),
            "evidence": field(q["evidence"], 1.0, **rule),
            "target": field({"kind": q["target"], "asset_id": q["asset_id"], "column": q.get("column")}, 1.0, **rule)}


def propose_terms(session: Session, workspace_id: str, cands: list[Candidate], *, batch: str | None,
                  proposed_by: str, limit: int = MAX_TERMS) -> int:
    from analystos.knowledge.suggestions import propose

    n = 0
    for c in cands:
        if n >= limit:
            break
        title = c.name if not c.placeholder or c.rule != "ask" else f"Define “{c.name}”?"
        row = propose(session, workspace_id, kind=KIND_TERM, subject=subject_for(c.key), title=title,
                      fields=_draft_fields(c, batch), origin=ORIGIN if c.rule != "ask" else "glossary.ask",
                      proposed_by=f"model:{c.model_hint['_model']}" if c.body_source == "model" else proposed_by,
                      batch=batch)
        n += int(row is not None)
    return n


def scan(session: Session, workspace_id: str, *, proposed_by: str = "process:glossary-scan", batch: str | None = None,
         asset_ids: Iterable[str] | None = None, router: Any = None, use_model: bool = False,
         include_ask: bool = True, include_descriptions: bool = True) -> dict[str, Any]:
    """Run every rule over the workspace catalog (or `asset_ids`) and recent unanswered Ask turns; queue new
    glossary drafts and description questions. Returns counts (and what the optional model did)."""
    from analystos.knowledge.entries import visible_entries
    from analystos.knowledge.suggestions import propose

    assets = _assets(session, workspace_id, asset_ids)
    entries = visible_entries(session, workspace_id, kinds=["term", "definition", "metric"])
    known = known_names(entries)
    decided = _subjects(session, workspace_id, "glossary:", ["pending", "approved", "rejected"])
    vocab_pack = _pack_vocabulary(session, workspace_id, assets)
    rules = code_set_candidates(assets) + recurring_candidates(assets) + abbreviation_candidates(
        assets, vocab_pack["acronyms"], expansions=vocab_pack["expansions"], prefixes=vocab_pack["prefixes"])
    if include_ask:
        vocab = vocabulary(_assets(session, workspace_id) if asset_ids is not None else assets, known)
        rules += ask_candidates(_recent_turns(session, workspace_id), vocab)
    cands, skipped = dedupe(rules, known, decided)
    cands = cands[:MAX_TERMS]
    model: dict[str, Any] = {"called": False, "filled": 0, "skipped": "not requested"}
    if use_model and router is not None:
        model = _model_fill(router, workspace_id, cands, allow_values=_samples_allowed(session, workspace_id))
    elif router is not None and any(c.placeholder for c in cands):
        _record_avoided(router, workspace_id, [c for c in cands if c.placeholder])
    terms = propose_terms(session, workspace_id, cands, batch=batch, proposed_by=proposed_by)
    closed = close_explained(session, workspace_id, assets)
    questions = 0
    if include_descriptions:
        asked = _subjects(session, workspace_id, "describe:", ["pending", "approved", "rejected"])
        for q in description_questions(assets, _pending_drafts(session, workspace_id)):
            if questions >= MAX_QUESTIONS:
                break
            subject = f"describe:{q['subject']}"
            if subject in asked:
                continue
            row = propose(session, workspace_id, kind=KIND_QUESTION, subject=subject, title=q["title"],
                          fields=_question_fields(q, batch), origin="glossary.describe", proposed_by=proposed_by, batch=batch)
            questions += int(row is not None)
    by_rule: dict[str, int] = {}
    for c in cands:
        by_rule[c.rule] = by_rule.get(c.rule, 0) + 1
    return {"glossary_terms": terms, "description_questions": questions, "closed_explained": closed,
            "candidates": len(cands), "by_rule": by_rule,
            "skipped_known": skipped["known"], "skipped_decided": skipped["decided"], "assets": len(assets), "model": model}


def close_explained(session: Session, workspace_id: str, assets: list[dict[str, Any]]) -> int:
    """Pending questions a person no longer needs to answer: a description question, or a code-set term, whose
    columns have since been explained (by a person, the source or an accepted draft). They are superseded, not
    rejected, so they would be asked again if the explanation were removed."""
    from analystos.db.models import KnowledgeSuggestion

    by_fq = {f"{a['schema']}.{a['name']}.{c['name']}".lower(): c for a in assets for c in a.get("columns") or []}
    by_id = {(a["id"], c["name"]): c for a in assets for c in a.get("columns") or []}
    closed = 0
    for row in session.scalars(select(KnowledgeSuggestion).where(
            KnowledgeSuggestion.workspace_id == workspace_id, KnowledgeSuggestion.status == "pending",
            KnowledgeSuggestion.kind.in_([KIND_QUESTION, KIND_TERM]))):
        fields = row.fields or {}
        if row.kind == KIND_QUESTION:
            parts = (row.subject or "").split(":")  # describe:column:<asset>:<column>
            col = by_id.get((parts[2], parts[3])) if len(parts) == 4 and parts[1] == "column" else None
            stale = col is not None and explained(col)
        else:
            evidence = _value(fields, "evidence", []) or []
            mapped = [by_fq.get(str(m).lower()) for m in (_value(fields, "mapped_columns", []) or [])]
            stale = bool(evidence) and all(e.get("kind") == "code_set" for e in evidence if isinstance(e, dict))                 and bool(mapped) and all(c is not None and explained(c) for c in mapped)
        if stale:
            row.status, row.reason = "superseded", "the column is now described; nothing to ask"
            closed += 1
    return closed


def from_ask_turn(session: Session, workspace_id: str, turn: Any) -> int:
    """After an unanswered Ask turn: its unknown terms as "Define X?" drafts (rules only, at most a few)."""
    if getattr(turn, "status", None) not in ("clarify", "needs_input", "refused"):
        return 0
    from analystos.knowledge.entries import visible_entries

    known = known_names(visible_entries(session, workspace_id, kinds=["term", "definition", "metric"]))
    vocab = vocabulary(_assets(session, workspace_id), known)
    cands = ask_candidates([{"id": turn.id, "question": turn.question, "status": turn.status,
                             "refusal": turn.refusal or {}}], vocab)
    decided = _subjects(session, workspace_id, "glossary:", ["pending", "approved", "rejected"])
    kept, _ = dedupe(cands, known, decided)
    return propose_terms(session, workspace_id, kept, batch=f"ask:{turn.id}", proposed_by="process:ask",
                         limit=MAX_ASK_TERMS_PER_TURN)


def pending_counts(session: Session, workspace_id: str) -> dict[str, Any]:
    """Pending drafts per kind, and how many are questions a person must answer (terms + descriptions)."""
    from sqlalchemy import func

    from analystos.db.models import KnowledgeSuggestion

    rows = session.execute(select(KnowledgeSuggestion.kind, func.count()).where(
        KnowledgeSuggestion.workspace_id == workspace_id, KnowledgeSuggestion.status == "pending")
        .group_by(KnowledgeSuggestion.kind)).all()
    by_kind = {k: int(n) for k, n in rows}
    return {"pending": by_kind, "questions": by_kind.get(KIND_TERM, 0) + by_kind.get(KIND_QUESTION, 0),
            "total": sum(by_kind.values())}


# ------------------------------------------------------------------------------------ approval
def _value(fields: dict[str, Any], name: str, default: Any = None) -> Any:
    f = fields.get(name)
    return f.get("value", default) if isinstance(f, dict) else default


def _human(fields: dict[str, Any], name: str) -> bool:
    return ((fields.get(name) or {}).get("provenance") or {}).get("source") == "human"


def _strings(value: Any, *, limit: int, max_chars: int) -> list[str]:
    items = value if isinstance(value, list) else [x for x in str(value or "").split(",")]
    out: list[str] = []
    for x in items:
        t = " ".join(str(x).split())[:max_chars]
        if t and t not in out:
            out.append(t)
    return out[:limit]


def term_error(fields: dict[str, Any]) -> str | None:
    """Why a glossary draft cannot be approved as it stands: a skeleton definition nobody filled in."""
    body = str(_value(fields, "body") or "").strip()
    if not body or not str(_value(fields, "name") or "").strip():
        return "definition_required"
    if _value(fields, "placeholder") and not _human(fields, "body"):
        return "definition_required"
    return None


def apply_glossary_term(session: Session, row: Any, fields: dict[str, Any], user: Any) -> str:
    """An approved glossary draft becomes a trusted workspace glossary entry (kind `term`, origin `agent`,
    accepted by a person). An existing entry with that name is updated, except a person's own definition,
    which is kept (only synonyms and mapped columns are added). Mapped columns link to it at once; every
    other column links on the next crawl (skills/catalog.link_glossary)."""
    from analystos.context.service import add_entry
    from analystos.db.models import ContextEntry

    name = " ".join(str(_value(fields, "name") or row.title).split())[:300]
    body = str(_value(fields, "body") or "").strip()
    synonyms = _strings(_value(fields, "synonyms"), limit=10, max_chars=100)
    mapped = _strings(_value(fields, "mapped_columns"), limit=50, max_chars=400)
    existing = next((e for e in session.scalars(select(ContextEntry).where(
        ContextEntry.workspace_id == row.workspace_id, ContextEntry.kind == "term")) if norm(e.name) == norm(name)), None)
    if existing is not None:
        existing.synonyms = list(dict.fromkeys([*(existing.synonyms or []), *synonyms]))
        existing.mapped_columns = list(dict.fromkeys([*(existing.mapped_columns or []), *mapped]))
        if existing.origin == "user":
            note, entry = f"glossary term {existing.id} kept (a person's definition); synonyms and columns added", existing
        else:
            existing.body, existing.origin, existing.trusted = body, "agent", True
            note, entry = f"glossary term {existing.id} updated", existing
    else:
        entry = add_entry(session, workspace_id=row.workspace_id, kind="term", name=name, body=body, synonyms=synonyms,
                          mapped_columns=mapped, origin="agent", trusted=True)
        session.flush()
        note = f"glossary term {entry.id} created"
    _link_mapped(session, row.workspace_id, entry, mapped)
    try:
        from analystos.registries import verified_queries

        verified_queries._lexicons.pop(row.workspace_id, None)  # the new synonyms reach Ask at once
    except Exception:  # noqa: BLE001 - the lexicon refreshes on its own within a minute
        pass
    return note


def _link_mapped(session: Session, workspace_id: str, entry: Any, mapped: list[str]) -> None:
    from analystos.db.models import SourceAsset, SourceColumn

    for fq in mapped:
        parts = fq.split(".")
        if len(parts) < 2:
            continue
        table, column = parts[-2], parts[-1]
        schema = ".".join(parts[:-2]) or None
        q = select(SourceColumn).join(SourceAsset, SourceAsset.id == SourceColumn.asset_id).where(
            SourceAsset.workspace_id == workspace_id, SourceAsset.name == table, SourceColumn.name == column)
        if schema:
            q = q.where(SourceAsset.schema_name == schema)
        for c in session.scalars(q):
            c.semantics = {**(c.semantics or {}), "glossary": {"term_id": entry.id, "term": entry.name, "score": 1.0,
                                                               "reason": "column is mapped to the term",
                                                               "origin": "review"}}


def answer_error(fields: dict[str, Any]) -> str | None:
    """A description question needs an answer: a person's text, or a model draft they accept as it is."""
    text = str(_value(fields, "description") or "").strip()
    if not text:
        return "answer_required"
    if not _human(fields, "description") and ((fields.get("description") or {}).get("provenance") or {}).get("source") != "model":
        return "answer_required"
    return None


def apply_description_answer(session: Session, row: Any, fields: dict[str, Any], user: Any) -> str:
    """The answer becomes catalog text: a person's words with origin `user` (the curation path: no crawl or
    model overwrites it), or an accepted model draft with origin `model`, reviewed. Text a person or the source
    wrote in the meantime is kept. The model draft the question wrapped is superseded."""
    from analystos.db.models import KnowledgeSuggestion, SourceAsset, SourceColumn

    target = _value(fields, "target") or {}
    text = str(_value(fields, "description") or "").strip()
    origin = "user" if _human(fields, "description") else "model"
    a = session.get(SourceAsset, str(target.get("asset_id") or ""))
    if a is None or a.workspace_id != row.workspace_id:
        return "asset not found"
    draft_subject = f"asset:{a.id}"
    if target.get("kind") == "column":
        c = session.scalar(select(SourceColumn).where(SourceColumn.asset_id == a.id, SourceColumn.name == str(target.get("column"))))
        if c is None:
            return "column not found"
        draft_subject = f"column:{a.id}:{c.name}"
        if c.description_origin in ("user", "source") and " ".join((c.description or "").split()) != " ".join(text.split()):
            note = "catalog kept: owner or source description"
        else:
            c.description, c.description_origin = text, origin
            c.semantics = {**(c.semantics or {}), "reviewed": True}
            note = "catalog updated"
    else:
        if (a.description_origin in ("user", "source")) and " ".join((a.description or "").split()) != " ".join(text.split()):
            note = "catalog kept: owner or source description"
        else:
            a.description, a.description_origin, a.reviewed = text, origin, True
            a.semantics = {k: v for k, v in (a.semantics or {}).items() if k != "model_description_draft"}
            note = "catalog updated"
    for d in session.scalars(select(KnowledgeSuggestion).where(
            KnowledgeSuggestion.workspace_id == row.workspace_id, KnowledgeSuggestion.subject == draft_subject,
            KnowledgeSuggestion.status == "pending", KnowledgeSuggestion.kind.in_(["table_description", "column_description"]))):
        d.status, d.reason = "superseded", f"answered by description question {row.id}"
    return note


def close_questions_for(session: Session, workspace_id: str, subject: str, decided_id: str) -> None:
    """A table/column draft decided in the queue answers the description question that wrapped it."""
    from analystos.db.models import KnowledgeSuggestion

    for q in session.scalars(select(KnowledgeSuggestion).where(
            KnowledgeSuggestion.workspace_id == workspace_id, KnowledgeSuggestion.subject == f"describe:{subject}",
            KnowledgeSuggestion.status == "pending")):
        q.status, q.reason = "superseded", f"decided through draft {decided_id}"
