"""Deterministic catalog skills for the metadata crawler: rules first, zero LLM tokens.

Everything here is a pure function over connector metadata (`DiscoveredAsset` /
`DiscoveredColumn`): no database, no network, no model. The crawler uses them to

* fingerprint assets and diff a crawl against the previous one (new / changed / missing /
  renamed), so unchanged tables are never re-profiled or re-described;
* infer business names, table roles (fact, dimension, ...), domains, grain and column roles from
  names, types and declared references, with a confidence and the evidence that produced it;
* classify PII from column names, strengthened (never weakened) by sample values;
* link columns to glossary terms by token overlap;
* decide which tables still need an (optional, later) LLM description and build compact,
  screened payloads for it. The LLM only fills what these rules could not; its output never
  overrides a reviewed description.

Descriptions produced here are template sentences built only from observed metadata, so they
never state a fact the metadata does not contain.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from analystos.connectors.base import DiscoveredAsset, DiscoveredColumn

TableRole = Literal["fact", "dimension", "bridge", "event", "reference", "staging", "audit", "unknown"]
ColumnRole = Literal["identifier", "foreign_key", "measure", "dimension", "timestamp", "date", "flag", "code", "name",
                     "text", "amount", "percent", "duration", "geo", "contact", "unknown"]
Unit = Literal["currency", "percent", "hours", "minutes", "seconds", "days", "count"]
PiiCategory = Literal["email", "phone", "person_name", "national_id", "payment_card", "address", "date_of_birth",
                      "ip_address", "credential", "free_text_risk"]
Sensitivity = Literal["public", "internal", "confidential", "restricted"]

SENSITIVITY_ORDER: dict[str, int] = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
ENRICH_CONFIDENCE = 0.6
GLOSSARY_THRESHOLD = 0.6
RENAME_SIMILARITY = 0.8
MAX_SCREENED_CHARS = 200


# --------------------------------------------------------------------------------------------
# tokens, names, types
# --------------------------------------------------------------------------------------------
ACRONYMS = frozenset({
    "id", "sla", "ci", "kpi", "crm", "erp", "hr", "po", "sku", "url", "ip", "api", "uuid", "guid", "sys", "itsm",
    "cmdb", "gl", "ar", "ap", "ssn", "dob", "vat", "usd", "eur", "gbp", "utc", "pii", "etl", "bi", "os", "sql", "qa",
    "ui", "ux", "csat", "nps", "mttr", "mtbf", "sso", "vpn", "dns", "cpu", "ram", "upc", "ean", "isbn", "iban", "fx",
    "pos", "roi", "ytd", "mtd", "qtd", "b2b", "b2c", "ltv", "cogs", "ebitda", "hq", "iso", "mfa", "cvv", "tin", "nhs",
    "icd", "cpt", "ehr", "emr", "gps", "eta", "rma", "bom", "mrp", "wms", "tms", "utm", "seo", "cpc", "cpm", "ctr",
    "aws", "gcp", "vm", "db", "it", "pk", "fk", "ok",
})
TABLE_PREFIXES = frozenset({"dim", "fact", "fct", "stg", "raw", "src", "tbl", "vw", "v", "bridge", "brg", "xref", "ref",
                            "lkp", "lookup", "stage", "staging", "landing", "tmp", "temp", "audit", "evt", "hist",
                            "sys"})
UNCOUNTABLE = frozenset({"sales", "data", "news", "status", "series", "analysis", "species", "metadata",
                         "logistics", "analytics", "economics", "insurance", "inventory", "equipment", "feedback",
                         "staff", "payroll", "info", "information", "software", "hardware", "stock"})
IRREGULAR = {"people": "person", "children": "child", "men": "man", "women": "woman", "sales": "sale",
             "addresses": "address", "statuses": "status", "analyses": "analysis", "indices": "index",
             "matrices": "matrix", "criteria": "criterion"}

_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_TYPE_PARAMS = re.compile(r"\(.*\)")


def split_tokens(name: str) -> list[str]:
    """snake_case, kebab, dotted and camelCase/PascalCase names -> lower-case tokens.

    ``SLADueDate`` -> ``[sla, due, date]``; ``customerID`` -> ``[customer, id]``.
    """
    out: list[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", name or ""):
        if part:
            out.extend(t.lower() for t in _CAMEL.findall(part))
    return out


def singularize(word: str) -> str:
    w = word.lower()
    if w in IRREGULAR:
        return IRREGULAR[w]
    if w in UNCOUNTABLE or len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "xes", "ches", "shes", "zes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def _word(t: str) -> str:
    return t.upper() if t in ACRONYMS else t.capitalize()


def humanize(tokens: Sequence[str]) -> str:
    return " ".join(_word(t) for t in tokens)


def normalize_type(data_type: str | None) -> str:
    """Map dialect type spellings to the connector's normalized vocabulary."""
    t = _TYPE_PARAMS.sub("", (data_type or "").strip().lower()).strip()
    if not t:
        return "text"
    if t in {"integer", "int", "int4", "smallint", "int2", "tinyint", "mediumint", "serial", "smallserial"}:
        return "integer"
    if t in {"bigint", "int8", "bigserial", "long"}:
        return "bigint"
    if t.startswith(("numeric", "decimal", "money", "smallmoney", "number")):
        return "numeric"
    if t in {"double", "double precision", "float", "float4", "float8", "real"}:
        return "double"
    if t in {"bool", "boolean", "bit"}:
        return "boolean"
    if t.startswith("timestamp") or t in {"datetime", "datetime2", "smalldatetime", "datetimeoffset", "glide_date_time"}:
        return "timestamp"
    if t in {"date", "glide_date"}:
        return "date"
    if t in {"json", "jsonb", "variant", "object"}:
        return "json"
    return "text"


NUMERIC_TYPES = frozenset({"integer", "bigint", "numeric", "double"})


def asset_key(asset: DiscoveredAsset) -> str:
    return f"{asset.schema_name}.{asset.name}" if asset.schema_name else asset.name


def _strip_table_prefix(tokens: list[str]) -> tuple[list[str], list[str]]:
    """Remove technical prefixes (dim_, fact_, stg_, raw_, tbl_, vw_ ...); returns (remaining, stripped)."""
    stripped: list[str] = []
    rest = list(tokens)
    while len(rest) > 1 and rest[0] in TABLE_PREFIXES:
        stripped.append(rest.pop(0))
    return rest, stripped


def _entity_from_table(name: str) -> str:
    rest, _ = _strip_table_prefix(split_tokens(name))
    rest = [t for t in rest if t not in {"dim", "fact", "fct", "bridge", "xref", "map", "lookup", "lkp"}] or rest
    return " ".join(rest[:-1] + [singularize(rest[-1])]) if rest else name.lower()


# --------------------------------------------------------------------------------------------
# 1. fingerprint
# --------------------------------------------------------------------------------------------
def column_signature(asset: DiscoveredAsset) -> dict[str, str]:
    """{column name: normalized type}, the comparable shape of an asset."""
    return {c.name: normalize_type(c.data_type) for c in asset.columns}


def fingerprint_asset(asset: DiscoveredAsset) -> str:
    """sha256 of the structural shape: independent of column order, row counts, freshness and text."""
    cols = sorted([c.name, normalize_type(c.data_type), bool(c.nullable), bool(c.is_key),
                   (c.references or "").strip().lower() or None] for c in asset.columns)
    payload = json.dumps({"kind": asset.kind, "columns": cols}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


# --------------------------------------------------------------------------------------------
# 2. crawl diff
# --------------------------------------------------------------------------------------------
class RetypedColumn(BaseModel):
    name: str
    previous_type: str
    current_type: str


class AssetChange(BaseModel):
    key: str
    previous_fingerprint: str | None
    fingerprint: str
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    retyped: list[RetypedColumn] = Field(default_factory=list)
    attributes_changed: bool = False  # nullability / key / reference / kind changed without a name/type change


class RenameCandidate(BaseModel):
    previous_key: str
    current_key: str
    similarity: float


class CrawlDiff(BaseModel):
    full: bool
    new: list[str] = Field(default_factory=list)
    changed: list[AssetChange] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)  # in the previous crawl, not seen in this one
    deprecated: list[str] = Field(default_factory=list)  # missing AND full crawl AND not a rename candidate
    rename_candidates: list[RenameCandidate] = Field(default_factory=list)
    fingerprints: dict[str, str] = Field(default_factory=dict)


def _signature_similarity(a: dict[str, str], b: dict[str, str]) -> float:
    if not a and not b:
        return 0.0
    same = sum(1 for k, v in a.items() if b.get(k) == v)
    return same / max(len(a), len(b))


def diff_crawl(previous: dict[str, dict[str, Any]], current: list[DiscoveredAsset], *, full: bool) -> CrawlDiff:
    """Compare this crawl with the stored state {key: {"fingerprint", "columns": {name: type}}}.

    An incremental crawl (``full=False``) may cover only part of a source, so absent assets are
    listed as ``missing`` but never ``deprecated``. Rename candidates pair a missing and a new
    asset whose column signatures are >= 80% identical (one-to-one, best first).
    """
    out = CrawlDiff(full=full)
    seen: dict[str, DiscoveredAsset] = {}
    for a in current:
        seen[asset_key(a)] = a
    for key in sorted(seen):
        a = seen[key]
        fp = fingerprint_asset(a)
        out.fingerprints[key] = fp
        prev = previous.get(key)
        if prev is None:
            out.new.append(key)
            continue
        if prev.get("fingerprint") == fp:
            out.unchanged.append(key)
            continue
        before = {k: normalize_type(v) for k, v in (prev.get("columns") or {}).items()}
        now = column_signature(a)
        change = AssetChange(key=key, previous_fingerprint=prev.get("fingerprint"), fingerprint=fp,
                             added=sorted(set(now) - set(before)), removed=sorted(set(before) - set(now)),
                             retyped=[RetypedColumn(name=c, previous_type=before[c], current_type=now[c])
                                      for c in sorted(set(now) & set(before)) if before[c] != now[c]])
        change.attributes_changed = not (change.added or change.removed or change.retyped)
        out.changed.append(change)
    out.missing = sorted(k for k in previous if k not in seen)
    pairs: list[tuple[float, str, str]] = []
    for m in out.missing:
        m_sig = {k: normalize_type(v) for k, v in (previous[m].get("columns") or {}).items()}
        for n in out.new:
            s = _signature_similarity(m_sig, column_signature(seen[n]))
            if s >= RENAME_SIMILARITY:
                pairs.append((s, m, n))
    used_m: set[str] = set()
    used_n: set[str] = set()
    for s, m, n in sorted(pairs, key=lambda p: (-p[0], p[1], p[2])):
        if m in used_m or n in used_n:
            continue
        used_m.add(m)
        used_n.add(n)
        out.rename_candidates.append(RenameCandidate(previous_key=m, current_key=n, similarity=round(s, 4)))
    out.deprecated = [m for m in out.missing if m not in used_m] if full else []
    return out


# --------------------------------------------------------------------------------------------
# 4. column semantics (defined before table semantics, which aggregates them)
# --------------------------------------------------------------------------------------------
class ColumnSemantics(BaseModel):
    name: str
    business_name: str
    semantic_role: ColumnRole
    unit: Unit | None = None
    references_entity: str | None = None
    description: str
    confidence: float
    evidence: list[str] = Field(default_factory=list)


_ID_TAIL = {"id", "key", "uuid", "guid", "sk", "fk", "pk", "no", "nbr"}
_FLAG_PREFIX = {"is", "has", "made", "can", "was", "should", "did", "allow", "allows", "enable", "enabled"}
_FLAG_TAIL = {"flag", "ind", "indicator", "yn"}
_TS_TAIL = {"at", "time", "ts", "timestamp", "datetime", "on"}
_TS_WORDS = {"created", "updated", "modified", "opened", "closed", "resolved", "timestamp", "datetime"}
_DATE_TAIL = {"date", "dt", "day"}
_PCT = {"pct", "percent", "percentage", "rate", "ratio", "share"}
_AMOUNT = {"amount", "amt", "price", "cost", "revenue", "fee", "fees", "tax", "discount", "salary", "wage", "balance",
           "spend", "margin", "profit", "income", "payment", "total", "subtotal", "budget", "cogs", "value", "sales",
           "charge", "credit", "debit", "refund", "bonus", "compensation"}
_COUNT = {"count", "cnt", "qty", "quantity", "num", "number", "units", "headcount", "visits", "clicks", "impressions"}
_DURATION_UNITS = {"hours": "hours", "hrs": "hours", "hr": "hours", "hour": "hours", "minutes": "minutes",
                   "mins": "minutes", "min": "minutes", "seconds": "seconds", "secs": "seconds", "sec": "seconds",
                   "days": "days"}
_DURATION = {"duration", "elapsed", "tenure", "age", "mttr", "latency", "lead", "cycle", "downtime", "uptime",
             "response", "resolution", "handle", "wait"}
_GEO = {"country", "city", "region", "latitude", "longitude", "lat", "lon", "lng", "zip", "zipcode", "postcode",
        "postal", "location", "province", "county", "territory", "geo", "timezone"}
_CONTACT = {"email", "phone", "mobile", "fax", "telephone", "address", "street", "addr"}
_CODE = {"code", "cd", "iso", "abbr", "abbreviation", "sku", "upc", "ean", "isbn"}
_NAME = {"name", "title", "label", "surname", "firstname", "lastname"}
_TEXT = {"description", "desc", "comment", "comments", "notes", "note", "text", "body", "summary", "message",
         "remarks", "details", "narrative", "reason", "work_notes", "resolution_notes"}
_DATE_PART = {"year", "month", "quarter", "week", "weekday", "dow", "fiscal", "yr", "qtr", "hour"}
_CATEGORICAL = {"status", "state", "type", "category", "priority", "severity", "impact", "urgency", "stage", "tier",
                "segment", "channel", "group", "class", "level", "gender", "grade", "band", "source", "method",
                "mode", "kind", "department", "currency", "language", "brand", "reason"}


def _conf(x: float) -> float:
    return round(max(0.0, min(1.0, x)), 2)


def _target_entity(references: str) -> str:
    table = references.split(".")[-2] if references.count(".") >= 1 else references
    return _entity_from_table(table)


def infer_column_semantics(column: DiscoveredColumn, asset: DiscoveredAsset | None = None) -> ColumnSemantics:
    """Semantic role, unit and a template description for one column from name tokens, type and references."""
    toks = split_tokens(column.name)
    tset = set(toks)
    first, last = (toks[0], toks[-1]) if toks else ("", "")
    dtype = normalize_type(column.data_type)
    numeric = dtype in NUMERIC_TYPES
    bn = column.business_name or humanize(toks) or column.name
    table_entity = _entity_from_table(asset.name) if asset is not None else ""
    ev: list[str] = [f"type {dtype}"]
    role: str = "unknown"
    unit: str | None = None
    conf = 0.3
    ref_entity: str | None = None

    def is_own_id() -> bool:
        head = [t for t in toks if t not in _ID_TAIL and t != "sys"]
        if not head:
            return True  # "id", "sys_id", "key"
        if " ".join(singularize(t) for t in head) == table_entity:
            return True
        # last-word match ("POLineID" in purchase_order_lines) only for the first id-like column,
        # so customer_account_map.account_id stays a reference
        if singularize(head[-1]) != table_entity.split(" ")[-1] or asset is None:
            return False
        first_id = next((c.name for c in asset.columns if split_tokens(c.name)[-1:] and split_tokens(c.name)[-1] in _ID_TAIL), None)
        return first_id == column.name

    if column.references:
        role, conf, ref_entity = "foreign_key", 0.95, _target_entity(column.references)
        ev.append("declared reference")
    elif column.is_key:
        role, conf = "identifier", 0.9
        ev.append("declared key")
        if last in _ID_TAIL and len(toks) > 1 and not is_own_id() and asset is not None:
            ev.append("key column names another entity (composite key part)")
            role, conf = "foreign_key", 0.75
            ref_entity = " ".join(t for t in toks if t not in _ID_TAIL)
    elif last in _ID_TAIL or column.name.lower() in {"sys_id", "uuid", "guid"}:
        if is_own_id():
            role, conf = "identifier", 0.75
            ev.append("id-like name matching the table entity")
        else:
            role, conf = "foreign_key", 0.7
            ref_entity = " ".join(singularize(t) for t in toks if t not in _ID_TAIL)
            ev.append("id-like name referencing another entity")
    elif dtype == "boolean" or first in _FLAG_PREFIX or last in _FLAG_TAIL:
        role, conf = "flag", 0.9 if dtype == "boolean" and (first in _FLAG_PREFIX or last in _FLAG_TAIL) else 0.75
        ev.append("boolean type" if dtype == "boolean" else "flag-like name")
    elif dtype == "date" or (last in _DATE_TAIL and dtype in {"date", "timestamp", "text"}) \
            or (last == "on" and dtype == "date"):
        role, conf = ("date", 0.9 if dtype == "date" else 0.65)
        ev.append("date type" if dtype == "date" else "date-like name")
    elif dtype == "timestamp" or last in _TS_TAIL or (tset & _TS_WORDS and not numeric):
        role, conf = "timestamp", 0.9 if dtype == "timestamp" else 0.6
        ev.append("timestamp type" if dtype == "timestamp" else "timestamp-like name")
    elif tset & _PCT and (numeric or dtype == "text"):
        role, unit, conf = "percent", "percent", 0.85 if numeric else 0.5
        ev.append("percent/rate token")
    elif tset & set(_DURATION_UNITS) and numeric:
        role, unit, conf = "duration", _DURATION_UNITS[next(t for t in toks if t in _DURATION_UNITS)], 0.85
        ev.append("duration unit token")
    elif tset & _DURATION and numeric:
        role, conf = "duration", 0.6
        unit = "days" if "age" in tset or "tenure" in tset else None
        ev.append("duration token")
    elif tset & _COUNT and numeric and not (tset & _AMOUNT):
        role, unit, conf = "measure", "count", 0.8
        ev.append("count token")
    elif tset & _AMOUNT and numeric:
        role, unit, conf = "amount", "currency", 0.8
        ev.append("monetary token")
    elif (last in _NAME or tset & {"firstname", "lastname", "surname"}) and not numeric:
        role, conf = "name", 0.8
        ev.append("name token")
    elif tset & _CODE and not numeric:
        role, conf = "code", 0.75
        ev.append("code token")
    elif tset & _CONTACT:
        role, conf = "contact", 0.8
        ev.append("contact token")
    elif tset & _GEO:
        role, conf = "geo", 0.75
        ev.append("geographic token")
    elif tset & _DATE_PART and dtype in NUMERIC_TYPES | {"text"}:
        role, conf = "dimension", 0.7
        ev.append("date-part token")
    elif (tset & _TEXT or column.name.lower() in _TEXT) and not numeric:
        role, conf = "text", 0.75
        ev.append("free-text token")
    elif tset & _CATEGORICAL:
        role, conf = "dimension", 0.7
        ev.append("categorical token")
    elif numeric:
        role, conf = "measure", 0.45
        ev.append("numeric type without a more specific name signal")
    elif dtype == "text":
        role, conf = "dimension", 0.35
        ev.append("text type without a more specific name signal")
    elif dtype == "json":
        role, conf = "text", 0.4
        ev.append("json payload")

    if column.description:
        conf += 0.05
    desc = _column_description(bn, role, unit, ref_entity, column.references)
    return ColumnSemantics(name=column.name, business_name=bn, semantic_role=role, unit=unit,  # type: ignore[arg-type]
                           references_entity=ref_entity, description=desc, confidence=_conf(conf), evidence=ev)


def _column_description(bn: str, role: str, unit: str | None, ref_entity: str | None, references: str | None) -> str:
    if role == "foreign_key":
        target = f" ({references})" if references else ""
        return f"{bn}: reference to {ref_entity or 'another entity'}{target}."
    phrases = {
        "identifier": "identifier of the record", "flag": "true/false flag", "date": "calendar date",
        "timestamp": "date and time", "percent": "percentage or rate", "duration": "duration",
        "measure": "numeric measure", "amount": "monetary amount", "contact": "contact detail",
        "geo": "geographic attribute", "code": "code value", "name": "name or label", "text": "free text",
        "dimension": "descriptive attribute", "unknown": "column",
    }
    extra = f" in {unit}" if unit and unit not in {"currency", "percent"} else ""
    if unit == "count":
        extra = " (count)"
    return f"{bn}: {phrases.get(role, 'column')}{extra}."


# --------------------------------------------------------------------------------------------
# 3. table semantics
# --------------------------------------------------------------------------------------------
class TableSemantics(BaseModel):
    key: str
    business_name: str
    entity: str
    domain: str
    role: TableRole
    grain: str
    description: str
    confidence: float
    evidence: list[str] = Field(default_factory=list)
    columns: list[ColumnSemantics] = Field(default_factory=list)


DOMAIN_KEYWORDS: dict[str, frozenset[str]] = {
    "finance": frozenset({"invoice", "payment", "ledger", "gl", "journal", "budget", "expense", "tax", "billing",
                          "bill", "receivable", "payable", "ar", "ap", "currency", "fx", "depreciation", "transaction",
                          "balance", "credit", "debit", "fiscal", "cost", "accrual", "finance", "financial", "refund"}),
    "sales": frozenset({"sale", "sales", "order", "quote", "opportunity", "deal", "pipeline", "discount", "store", "pos",
                        "revenue", "return", "cart", "basket", "checkout", "retail", "price", "quantity"}),
    "customer": frozenset({"customer", "client", "crm", "subscriber", "member", "loyalty", "churn", "nps", "csat",
                           "consumer", "household", "persona"}),
    "product": frozenset({"product", "sku", "item", "catalog", "brand", "variant", "upc", "ean", "assortment"}),
    "it_operations": frozenset({"incident", "problem", "change", "ticket", "ci", "cmdb", "sla", "assignment", "caller",
                                "outage", "alert", "server", "host", "deployment", "sys", "itsm", "knowledge",
                                "escalation", "reassignment", "servicenow", "configuration", "resolved", "resolution",
                                "urgency", "impact", "assigned", "request", "task"}),
    "hr": frozenset({"employee", "staff", "headcount", "payroll", "salary", "hire", "termination", "department",
                     "position", "job", "leave", "absence", "attendance", "compensation", "performance", "recruit",
                     "recruiting", "candidate", "applicant", "hr", "worker", "tenure", "manager", "benefit",
                     "onboarding", "requisition"}),
    "supply_chain": frozenset({"inventory", "stock", "warehouse", "supplier", "vendor", "purchase", "po", "procurement",
                               "replenishment", "lot", "bom", "material", "receipt", "demand", "supply", "reorder"}),
    "marketing": frozenset({"campaign", "ad", "ads", "click", "impression", "utm", "lead", "conversion", "audience",
                            "promotion", "promo", "newsletter", "pageview", "seo", "cpc", "cpm", "ctr", "marketing",
                            "attribution", "funnel"}),
    "security": frozenset({"login", "auth", "permission", "access", "vulnerability", "threat", "firewall", "credential",
                           "mfa", "sso", "privilege", "security", "cve", "breach", "password", "entitlement"}),
    "logistics": frozenset({"shipment", "delivery", "carrier", "route", "freight", "tracking", "vehicle", "fleet",
                            "parcel", "dispatch", "transit", "shipping", "consignment", "depot"}),
    "healthcare": frozenset({"patient", "encounter", "diagnosis", "icd", "cpt", "procedure", "claim", "provider",
                             "prescription", "medication", "admission", "discharge", "clinical", "lab", "ehr", "emr",
                             "physician", "hospital", "ward"}),
}

_EVENT_NAME = {"event", "events", "log", "logs", "click", "clicks", "pageview", "pageviews", "activity", "activities",
               "session", "sessions", "telemetry", "tracking", "hit", "hits"}
_AUDIT_NAME = {"audit", "history", "hist", "changelog", "journal", "trail"}
_REFERENCE_NAME = {"ref", "lkp", "lookup", "codes", "code", "types", "type", "enum", "reference", "calendar"}
_BRIDGE_NAME = {"bridge", "brg", "xref", "map", "mapping", "link", "assoc", "association"}
_MEASURE_ROLES = {"measure", "amount", "percent", "duration"}
_DESCRIPTIVE_ROLES = {"name", "text", "code", "dimension", "contact", "geo"}
_TIME_ROLES = {"timestamp", "date"}


def _prefix_role(tokens: list[str]) -> tuple[str | None, str | None]:
    """Role implied by a naming convention (prefix or suffix), with the signal that implied it."""
    if not tokens:
        return None, None
    first, last = tokens[0], tokens[-1]
    table = {
        "fact": {"fact", "fct", "f"}, "dimension": {"dim", "d"}, "bridge": {"bridge", "brg", "xref"},
        "staging": {"stg", "raw", "src", "stage", "staging", "landing", "tmp", "temp"},
        "audit": {"audit", "hist"}, "event": {"evt"}, "reference": {"ref", "lkp", "lookup"},
    }
    if len(tokens) > 1:
        for role, pre in table.items():
            if first in pre and first not in {"f", "d"}:
                return role, f"prefix {first}_"
        suffix = {"fact": {"fact", "fct"}, "dimension": {"dim"}, "bridge": {"bridge", "xref", "map"},
                  "audit": {"audit", "history", "hist", "changelog"}, "event": {"events", "event", "log", "logs"},
                  "reference": {"lookup", "lkp", "codes", "types"}}
        for role, suf in suffix.items():
            if last in suf:
                return role, f"suffix _{last}"
    return None, None


def _structural_role(sem: list[ColumnSemantics], cols: list[DiscoveredColumn], name_tokens: set[str],
                     inbound: int) -> tuple[str, float, list[str]]:
    fk = [s for s in sem if s.semantic_role == "foreign_key"]
    measures = [s for s in sem if s.semantic_role in _MEASURE_ROLES]
    times = [s for s in sem if s.semantic_role in _TIME_ROLES]
    idents = [s for s in sem if s.semantic_role == "identifier"]
    desc = [s for s in sem if s.semantic_role in _DESCRIPTIVE_ROLES]
    names_codes = [s for s in sem if s.semantic_role in {"name", "code"}]
    other = [s for s in sem if s.semantic_role not in {"foreign_key", "identifier", "timestamp", "date", "flag"}]
    ev = [f"{len(fk)} foreign keys, {len(measures)} measures, {len(times)} time columns, {len(idents)} identifiers"]
    col_tokens = {t for c in cols for t in split_tokens(c.name)}
    if len(fk) >= 2 and not measures and len(other) <= 1:
        return "bridge", 0.75, ev + ["only references (plus at most one attribute)"]
    names = [s for s in sem if s.semantic_role == "name"]
    if names and idents and len(desc) >= 2 and len(measures) <= 1:
        conf = 0.65 + (0.15 if inbound else 0.0)
        return "dimension", _conf(conf), ev + ["keyed entity with name attributes"] + (
            [f"referenced by {inbound} other asset(s)"] if inbound else [])
    if len(fk) >= 1 and len(measures) >= 2 and not names:
        return "fact", _conf(0.6 + 0.05 * min(len(measures), 3)), ev + ["reference with several measures"]
    if len(fk) >= 2 and measures:
        return "fact", _conf(0.65 + 0.05 * min(len(measures), 3) + 0.03 * min(len(fk), 3)), ev + ["references with measures"]
    if name_tokens & _EVENT_NAME or (times and fk and col_tokens & {"event", "action", "activity"} and len(measures) <= 1):
        return "event", 0.65, ev + ["time-stamped activity records"]
    if fk and times and measures:
        return "fact", 0.6, ev + ["reference, time column and measure"]
    if len(sem) <= 5 and names_codes and len(fk) <= 1 and not measures:
        return "reference", 0.65, ev + ["few columns with a code/name"]
    if (idents or any(c.is_key for c in cols)) and len(desc) >= 2 and len(measures) <= len(desc):
        conf = 0.6 + (0.15 if inbound else 0.0)
        return "dimension", _conf(conf), ev + ["keyed descriptive attributes"] + (
            [f"referenced by {inbound} other asset(s)"] if inbound else [])
    if inbound and not measures:
        return "dimension", 0.55, ev + [f"referenced by {inbound} other asset(s)"]
    if fk and times:
        return "fact", 0.5, ev + ["transactional shape (references and time)"]
    return "unknown", 0.3, ev


def _inbound_references(asset: DiscoveredAsset, entity: str, all_assets: Iterable[DiscoveredAsset] | None) -> int:
    if not all_assets:
        return 0
    n = 0
    own = {asset.name.lower(), asset.source_name.lower()}
    for other in all_assets:
        if other is asset or other.name == asset.name:
            continue
        for c in other.columns:
            if c.references and c.references.split(".")[-2 if "." in c.references else 0].lower() in own:
                n += 1
                break
            toks = split_tokens(c.name)
            if c.references is None and len(toks) > 1 and toks[-1] in _ID_TAIL \
                    and " ".join(singularize(t) for t in toks[:-1]) == entity:
                n += 1
                break
    return n


def _domain(name_tokens: list[str], cols: list[DiscoveredColumn]) -> tuple[str, float, list[str]]:
    scores: dict[str, float] = {}
    hits: dict[str, list[str]] = {}
    tset = {singularize(t) for t in name_tokens} | set(name_tokens)
    col_tokens: set[str] = set()
    for c in cols:
        col_tokens |= {singularize(t) for t in split_tokens(c.name)} | set(split_tokens(c.name))
    for dom, kws in DOMAIN_KEYWORDS.items():
        th = sorted(tset & kws)
        ch = sorted((col_tokens & kws) - set(th))
        s = 3.0 * len(th) + 1.0 * len(ch)
        if s:
            scores[dom], hits[dom] = s, th + ch
    if not scores:
        return "generic", 0.3, ["no domain keywords"]
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    best, s = ranked[0]
    runner = ranked[1][1] if len(ranked) > 1 else 0.0
    if s < 2:
        return "generic", 0.35, [f"weak domain signal ({best}: {', '.join(hits[best])})"]
    conf = 0.45 + 0.08 * min(s, 6) - (0.15 if runner and runner >= s * 0.8 else 0.0)
    return best, _conf(min(conf, 0.95)), [f"domain {best} keywords: {', '.join(hits[best][:6])}"]


def _grain(role: str, entity: str, sem: list[ColumnSemantics], cols: list[DiscoveredColumn],
           name_tokens: set[str]) -> tuple[str, float]:
    keys = [c for c in cols if c.is_key]
    by = {s.name: s for s in sem}
    fk_entities = [by[c.name].references_entity for c in cols
                   if by[c.name].semantic_role == "foreign_key" and by[c.name].references_entity]
    time_part = ""
    if name_tokens & {"daily", "day"}:
        time_part = " per day"
    elif name_tokens & {"weekly", "week"}:
        time_part = " per week"
    elif name_tokens & {"monthly", "month"}:
        time_part = " per month"
    own_keys = [c for c in keys if by[c.name].semantic_role == "identifier"]
    if len(keys) == 1 and own_keys:
        return f"one row per {entity}{time_part}", 0.85
    if keys and len(keys) >= 2:
        parts = [by[c.name].references_entity or by[c.name].business_name.lower() for c in keys]
        return "one row per " + " per ".join(dict.fromkeys(parts)), 0.8
    idents = [s for s in sem if s.semantic_role == "identifier"]
    if len(idents) == 1:
        return f"one row per {entity}{time_part}", 0.7
    if role in {"bridge"} and len(fk_entities) >= 2:
        return "one row per " + " and ".join(dict.fromkeys(fk_entities)) + " combination", 0.6
    if role in {"fact", "event"} and fk_entities:
        if not time_part and any(s.semantic_role == "date" for s in sem):
            time_part = " per day" if "snapshot" in name_tokens else ""
        return f"one row per {entity}{time_part}", 0.45
    return f"one row per {entity}{time_part}", 0.35


_ROLE_PHRASE = {"fact": "Fact table", "dimension": "Dimension table", "bridge": "Bridge table",
                "event": "Event table", "reference": "Reference table", "staging": "Staging table",
                "audit": "Audit/history table", "unknown": "Table"}


def _describe(role: str, business_name: str, domain: str, grain: str, sem: list[ColumnSemantics],
              kind: str) -> str:
    noun = _ROLE_PHRASE[role] if kind == "table" else _ROLE_PHRASE[role].replace("table", kind.replace("_", " "))
    dom = f" in the {domain.replace('_', ' ')} domain" if domain != "generic" else ""
    parts = [f"{noun} '{business_name}'{dom} with {len(sem)} columns, {grain}."]
    measures = [s.business_name for s in sem if s.semantic_role in _MEASURE_ROLES]
    links = list(dict.fromkeys(s.references_entity for s in sem if s.semantic_role == "foreign_key" and s.references_entity))
    times = [s.business_name for s in sem if s.semantic_role in _TIME_ROLES]
    if measures:
        parts.append("Measures: " + ", ".join(measures[:5]) + ("." if len(measures) <= 5 else ", ..."))
    if links:
        parts.append("References: " + ", ".join(links[:5]) + ".")
    if times:
        parts.append("Time columns: " + ", ".join(times[:4]) + ".")
    return " ".join(parts)


def infer_table_semantics(asset: DiscoveredAsset, *, all_assets: list[DiscoveredAsset] | None = None) -> TableSemantics:
    """Business name, role, domain, grain and a factual template description for one asset.

    Naming conventions (dim_/fact_/stg_ ...) and structure (references, measures, time columns,
    keys, inbound references from ``all_assets``) are both evidence. When they agree the confidence
    is high; when they disagree the convention wins with reduced confidence and the disagreement
    is recorded. ``confidence`` < 0.6 means "rules were not sure" (see ``needs_enrichment``).
    """
    tokens = split_tokens(asset.name)
    rest, stripped = _strip_table_prefix(tokens)
    entity = _entity_from_table(asset.name)
    sem = [infer_column_semantics(c, asset) for c in asset.columns]
    name_set = set(tokens)
    inbound = _inbound_references(asset, entity, all_assets)
    evidence: list[str] = []
    if stripped:
        evidence.append("stripped technical prefix " + "_".join(stripped) + "_")

    p_role, p_signal = _prefix_role(tokens)
    s_role, s_conf, s_ev = _structural_role(sem, asset.columns, name_set, inbound)
    if p_role in {"staging", "audit"}:
        role, role_conf = p_role, 0.85
        evidence.append(f"role {p_role} from {p_signal}; modelled shape looks like {s_role}")
    elif p_role:
        if s_role == p_role:
            role, role_conf = p_role, 0.95
            evidence.append(f"role {p_role} from {p_signal}, confirmed by structure")
        elif s_role == "unknown" or (p_role == "dimension" and s_role == "reference") or \
                (p_role == "reference" and s_role == "dimension") or (p_role == "event" and s_role == "fact"):
            role, role_conf = p_role, 0.85
            evidence.append(f"role {p_role} from {p_signal}")
        else:
            role, role_conf = p_role, 0.7
            evidence.append(f"role {p_role} from {p_signal}; structure suggests {s_role}")
    else:
        role, role_conf = s_role, s_conf
        if name_set & _AUDIT_NAME and role in {"unknown", "event", "fact"}:
            role, role_conf = "audit", 0.7
            evidence.append("audit/history name token")
        elif name_set & _REFERENCE_NAME and role in {"unknown", "dimension"} and len(asset.columns) <= 6:
            role, role_conf = "reference", 0.7
            evidence.append("reference/lookup name token")
        elif name_set & _BRIDGE_NAME and role in {"unknown", "fact"} and not any(
                s.semantic_role in _MEASURE_ROLES for s in sem):
            role, role_conf = "bridge", 0.7
            evidence.append("bridge/mapping name token")
        else:
            evidence.append(f"role {role} from structure")
    evidence.extend(s_ev)

    if role in {"dimension", "reference"}:
        bn_tokens = rest[:-1] + [singularize(rest[-1])] if rest[-1] not in UNCOUNTABLE else rest
    else:
        bn_tokens = rest
    business_name = asset.business_name or humanize(bn_tokens)
    if asset.business_name:
        evidence.append("business name declared by the source")
    domain, d_conf, d_ev = _domain(rest, asset.columns)
    evidence.extend(d_ev)
    grain, g_conf = _grain(role, entity, sem, asset.columns, name_set)
    evidence.append(f"grain confidence {g_conf}")
    description = _describe(role, business_name, domain, grain, sem, asset.kind)
    conf = 0.5 * role_conf + 0.25 * d_conf + 0.25 * g_conf
    if not asset.columns:
        conf = min(conf, 0.3)
        evidence.append("no columns discovered")
    return TableSemantics(key=asset_key(asset), business_name=business_name, entity=entity, domain=domain,
                          role=role, grain=grain, description=description, confidence=_conf(conf),  # type: ignore[arg-type]
                          evidence=evidence, columns=sem)


# --------------------------------------------------------------------------------------------
# 5. PII
# --------------------------------------------------------------------------------------------
class PiiResult(BaseModel):
    category: PiiCategory | None = None
    sensitivity: Sensitivity = "internal"
    confidence: float = 0.0
    reasons: list[str] = Field(default_factory=list)


_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_EMAIL_SEARCH = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
_NANP_RE = re.compile(r"^(?:\+?1[\s.-]?)?\(?([2-9]\d{2})\)?[\s.-]?([2-9]\d{2})[\s.-]?(\d{4})$")
_SSN_RE = re.compile(r"^(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}$")
_SSN_SEARCH = re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")
_CARD_RE = re.compile(r"^(?:\d[ -]?){12,18}\d$")
_CARD_SEARCH = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")

_PII_NAME_RULES: list[tuple[str, str, str]] = [
    # (category, sensitivity, why) — evaluated by _pii_from_name in this order
    ("credential", "restricted", "credential-like column name"),
    ("national_id", "restricted", "national identifier column name"),
    ("payment_card", "restricted", "payment card / bank account column name"),
    ("ip_address", "confidential", "IP address column name"),
    ("email", "confidential", "email column name"),
    ("phone", "confidential", "phone column name"),
    ("date_of_birth", "confidential", "date of birth column name"),
    ("address", "confidential", "postal address column name"),
    ("person_name", "confidential", "person name column name"),
    ("free_text_risk", "internal", "free-text column may contain personal data"),
]
_PERSON_NOUNS = {"customer", "employee", "contact", "caller", "user", "patient", "requester", "requestor", "assignee",
                 "manager", "owner", "author", "person", "member", "student", "driver", "applicant", "candidate",
                 "recipient", "sender", "beneficiary", "guest", "client", "subscriber", "agent", "opened", "resolved",
                 "assigned", "holder", "cardholder"}


def _pii_from_name(tokens: list[str]) -> str | None:
    t = set(tokens)
    joined = "_".join(tokens)
    if t & {"password", "passwd", "pwd", "secret", "passphrase", "token", "apikey", "salt", "otp", "pin"} \
            or {"api", "key"} <= t or {"private", "key"} <= t or {"access", "key"} <= t or {"secret", "key"} <= t:
        usage_counter = "token" in t and t & {"count", "cnt", "num", "usage", "total"}
        if not usage_counter:
            return "credential"
    if t & {"ssn", "passport", "nino", "aadhaar", "nhs"} or "social_security" in joined or "national_id" in joined or "tax_id" in joined or "tin" in t \
            or "driver_license" in joined or "driver_licence" in joined or "drivers_license" in joined:
        return "national_id"
    if t & {"cvv", "cvc", "iban", "ccn", "pan"} or "credit_card" in joined or "card_number" in joined \
            or "cc_number" in joined or "card_no" in joined or "card_num" in joined or "bank_account" in joined \
            or "routing_number" in joined:
        return "payment_card"
    if "ip" in t or t & {"ipv4", "ipv6", "ipaddress"} or "remote_addr" in joined:
        return "ip_address"
    if t & {"email", "mail", "emailaddress"} and not t & {"sent", "count", "campaign", "template", "opened"}:
        return "email"
    if t & {"phone", "mobile", "telephone", "fax", "msisdn", "cell", "cellphone", "tel"}:
        return "phone"
    if "dob" in t or "birth" in t or "birthday" in t or "birthdate" in t:
        return "date_of_birth"
    if t & {"address", "street", "addr", "postcode", "zipcode"} or "zip" in t or "postal_code" in joined \
            or ("address" in joined):
        return "address"
    if t & {"firstname", "lastname", "surname", "fullname"} or ({"name"} & t and t & {"first", "last", "given",
                                                                                    "family", "middle", "full",
                                                                                    "maiden", "display"}) \
            or ("name" in t and t & _PERSON_NOUNS):
        return "person_name"
    if t & {"notes", "note", "comment", "comments", "remarks", "message", "body", "narrative", "freetext"} \
            or {"work", "notes"} <= t or {"free", "text"} <= t:
        return "free_text_risk"
    return None


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _is_card(v: str) -> bool:
    if not _CARD_RE.match(v):
        return False
    digits = re.sub(r"\D", "", v)
    return 13 <= len(digits) <= 19 and len(set(digits)) > 1 and _luhn_ok(digits)


def _is_ip(v: str) -> bool:
    if not re.fullmatch(r"[0-9A-Fa-f:.]{3,45}", v) or ("." not in v and ":" not in v):
        return False
    try:
        ipaddress.ip_address(v)
    except ValueError:
        return False
    return True


def _is_phone(v: str, hinted: bool) -> bool:
    if _E164_RE.match(v):
        return True
    if _NANP_RE.match(v):
        return hinted or bool(re.search(r"[\s().+-]", v))
    return False


_VALUE_DETECTORS: list[tuple[str, str, Any]] = [
    ("payment_card", "restricted", _is_card),
    ("national_id", "restricted", lambda v: bool(_SSN_RE.match(v))),
    ("email", "confidential", lambda v: bool(_EMAIL_RE.match(v))),
    ("ip_address", "confidential", _is_ip),
]


def classify_pii(name: str, data_type: str, sample_values: list[str] | None = None) -> PiiResult:
    """PII category and sensitivity from the column name, strengthened by sample values.

    Values can add or upgrade a classification (e.g. a ``contact`` column holding e-mail
    addresses, a notes column containing a card number) but never downgrade a name-based one.
    Reasons describe counts and patterns only; sample values are never echoed.
    """
    tokens = split_tokens(name)
    dtype = normalize_type(data_type)
    cat = _pii_from_name(tokens)
    if cat == "date_of_birth" and dtype not in {"date", "timestamp", "text"}:
        cat = None
    if cat == "free_text_risk" and dtype not in {"text", "json"}:
        cat = None
    result = PiiResult()
    if cat:
        rule = next(r for r in _PII_NAME_RULES if r[0] == cat)
        result = PiiResult(category=cat, sensitivity=rule[1], confidence=0.7 if cat != "free_text_risk" else 0.5,  # type: ignore[arg-type]
                           reasons=[rule[2]])

    values = [str(v).strip() for v in (sample_values or [])[:500] if v is not None and str(v).strip()]
    if not values:
        return result
    n = len(values)

    def upgrade(category: str, sensitivity: str, confidence: float, reason: str) -> None:
        cur = SENSITIVITY_ORDER[result.sensitivity]
        new = SENSITIVITY_ORDER[sensitivity]
        if new > cur or (new == cur and result.category in {None, "free_text_risk"}):
            result.category, result.sensitivity = category, sensitivity  # type: ignore[assignment]
        result.confidence = _conf(max(result.confidence, confidence))
        result.reasons.append(reason)

    for category, sensitivity, fn in _VALUE_DETECTORS:
        k = sum(1 for v in values if fn(v))
        if k and k / n >= 0.5:
            conf = 0.95 if cat == category else 0.85
            upgrade(category, sensitivity, conf, f"{k}/{n} sample values match the {category.replace('_', ' ')} pattern")
    hinted_phone = cat == "phone"
    k = sum(1 for v in values if _is_phone(v, hinted_phone))
    if k and k / n >= 0.5:
        upgrade("phone", "confidential", 0.95 if hinted_phone else 0.8, f"{k}/{n} sample values match a phone pattern")

    if dtype in {"text", "json"} and result.category in {None, "free_text_risk"}:
        embedded: list[tuple[str, str]] = []
        if any(_EMAIL_SEARCH.search(v) for v in values):
            embedded.append(("email address", "confidential"))
        if any(_SSN_SEARCH.search(v) for v in values):
            embedded.append(("national identifier", "restricted"))
        if any(_is_card(m.group(0)) for v in values for m in _CARD_SEARCH.finditer(v)):
            embedded.append(("payment card number", "restricted"))
        if embedded:
            worst = max((s for _, s in embedded), key=lambda s: SENSITIVITY_ORDER[s])
            result.category = "free_text_risk"
            if SENSITIVITY_ORDER[worst] > SENSITIVITY_ORDER[result.sensitivity]:
                result.sensitivity = worst  # type: ignore[assignment]
            result.confidence = _conf(max(result.confidence, 0.8))
            result.reasons.append("sample values embed " + ", ".join(e for e, _ in embedded))
    return result


# --------------------------------------------------------------------------------------------
# 6. glossary linking
# --------------------------------------------------------------------------------------------
class GlossaryLink(BaseModel):
    term_id: str
    column_fq: str
    score: float
    reason: str


_STOP = {"the", "of", "a", "an", "and", "or", "to", "in", "for", "by", "per", "on"}


def _stem(t: str) -> str:
    w = singularize(t)
    for suf in ("ing", "ed"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)]
            break
    if w.endswith("e") and len(w) > 3:
        w = w[:-1]
    return w


def _stems(text: str) -> frozenset[str]:
    return frozenset(_stem(t) for t in split_tokens(text) if t not in _STOP)


def link_glossary(columns: list[dict[str, Any]], terms: list[dict[str, Any]], *,
                  threshold: float = GLOSSARY_THRESHOLD) -> list[GlossaryLink]:
    """Best glossary term per column: explicit ``mapped_columns`` (score 1.0), else Dice overlap of
    stemmed tokens between the column (name + business name) and the term name or a synonym.
    Links under ``threshold`` are dropped; ties break on term id for a stable result."""
    phrases: list[tuple[str, str, frozenset[str], str]] = []
    mapped: dict[str, str] = {}
    for term in terms:
        tid = str(term["id"])
        for m in term.get("mapped_columns") or []:
            mapped.setdefault(str(m).lower(), tid)
        for label, kind in [(term.get("name") or "", "name")] + [(s, "synonym") for s in term.get("synonyms") or []]:
            st = _stems(label)
            if st:
                phrases.append((tid, label, st, kind))
    links: list[GlossaryLink] = []
    for col in columns:
        fq = str(col["fq"])
        low = fq.lower()
        hit = mapped.get(low) or next((tid for m, tid in sorted(mapped.items())
                                       if low.endswith("." + m) or m.endswith("." + low)), None)
        if hit:
            links.append(GlossaryLink(term_id=hit, column_fq=fq, score=1.0, reason="column is mapped to the term"))
            continue
        name_st = _stems(str(col.get("name") or fq.split(".")[-1]))
        bn_st = _stems(str(col.get("business_name") or ""))
        best: tuple[float, str, str] | None = None
        for tid, label, st, kind in phrases:
            for cs in {name_st, bn_st, name_st | bn_st}:
                if not cs:
                    continue
                inter = len(cs & st)
                if not inter:
                    continue
                score = 1.0 if cs == st else 2 * inter / (len(cs) + len(st))
                score = min(score, 0.95)  # only an explicit mapping earns 1.0
                cand = (round(score, 4), tid, f"token overlap with term {kind} '{label}'")
                if best is None or cand[0] > best[0] or (cand[0] == best[0] and cand[1] < best[1]):
                    best = cand
        if best and best[0] >= threshold:
            links.append(GlossaryLink(term_id=best[1], column_fq=fq, score=best[0], reason=best[2]))
    return links


# --------------------------------------------------------------------------------------------
# 7-8. enrichment gate and batches for the optional LLM step
# --------------------------------------------------------------------------------------------
_PLACEHOLDERS = {"", "n/a", "na", "none", "null", "tbd", "todo", "-", "--", "?", "description", "no description",
                 "placeholder", "desc", "table", "tmp", "test", "to be defined", "to do", "unknown", "no comment"}


def is_placeholder_description(text: str | None, *, table_name: str | None = None) -> bool:
    if text is None:
        return True
    t = re.sub(r"\s+", " ", text).strip().strip(".").lower()
    if t in _PLACEHOLDERS or len(t) < 4 or "lorem ipsum" in t or t.startswith(("auto-generated", "autogenerated")):
        return True
    return bool(table_name) and t.replace(" ", "_") == table_name.lower()


def needs_enrichment(table: TableSemantics, *, existing_description: str | None, reviewed: bool) -> bool:
    """True only when an LLM may fill the description: never for reviewed tables or tables with a
    real (non-placeholder, non-rule-generated) description, and only when rules were not confident."""
    if reviewed:
        return False
    human = existing_description is not None and not is_placeholder_description(
        existing_description, table_name=table.key.split(".")[-1]) and existing_description.strip() != table.description
    if human:
        return False
    return table.confidence < ENRICH_CONFIDENCE or table.role == "unknown" or not table.description


_INJECTION = re.compile(
    r"(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+|your\s+|these\s+)*(previous|prior|above|earlier|"
    r"preceding|system|safety)?\s*(instructions?|prompts?|rules?|messages?|context|directions?)"
    r"|system\s*prompt|you\s+are\s+now|act\s+as\s|pretend\s+(to\s+be|you)|jailbreak|developer\s+mode"
    r"|new\s+instructions?|do\s+anything\s+now|<\|[^|]*\|>|\b(assistant|system|user)\s*:"
    r"|\bbegin\s+(prompt|instructions)|\bexfiltrat|\bexecute\s+(this|the\s+following)",
    re.I)
_URL = re.compile(r"(https?://|ftp://|www\.)\S+|\b[\w.-]+\.(com|net|org|io|ai|xyz|ru|cn)/\S*", re.I)
_FENCE = re.compile(r"```.*?(```|$)|~~~.*?(~~~|$)", re.S)
_TAGS = re.compile(r"<\s*/?\s*(script|style|iframe|img|a|system|instructions?)\b[^>]*>", re.I)
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏ -‮⁠-⁤﻿]")


def has_injection(s: str | None) -> bool:
    """True when untrusted text contains an instruction-to-the-model pattern (the same test
    `screen_text` uses to drop a sentence), so a caller can refuse the text rather than trim it."""
    return bool(s) and bool(_INJECTION.search(_CTRL.sub(" ", str(s))))


def screen_text(s: str | None, *, max_chars: int = MAX_SCREENED_CHARS) -> str:
    """Neutralise metadata text before it is shown to a model: drop code fences, markup, URLs,
    control/zero-width characters and any sentence that reads like an instruction to the model;
    cap the length. Names and comments are untrusted data from the source system."""
    if not s:
        return ""
    t = _CTRL.sub(" ", str(s))
    t = _FENCE.sub(" ", t)
    t = _TAGS.sub(" ", t)
    t = _URL.sub(" ", t)
    sentences = re.split(r"(?<=[.!?;\n])\s+", t)
    t = " ".join(x for x in sentences if not _INJECTION.search(x))
    t = re.sub(r"`+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > max_chars:
        t = t[:max_chars].rsplit(" ", 1)[0].rstrip() + "..."
    return t


def _baseline(sem: Any) -> dict[str, Any]:
    if isinstance(sem, TableSemantics):
        sem = sem.model_dump()
    sem = sem or {}
    return {k: (screen_text(sem[k]) if isinstance(sem.get(k), str) else sem.get(k))
            for k in ("business_name", "role", "domain", "grain", "description", "confidence") if k in sem}


def _is_sensitive(col: dict[str, Any]) -> bool:
    sens = col.get("sensitivity")
    cat = col.get("pii_category")
    if sens is None and cat is None:
        r = classify_pii(str(col.get("name", "")), str(col.get("data_type") or col.get("type") or "text"))
        sens, cat = r.sensitivity, r.category
    return SENSITIVITY_ORDER.get(str(sens), 1) >= SENSITIVITY_ORDER["confidential"] or (
        cat is not None and cat != "free_text_risk")


def enrichment_batches(items: list[dict[str, Any]], *, max_tables: int = 25, max_columns: int = 12,
                       exclude_sensitive: bool = True) -> list[list[dict[str, Any]]]:
    """Compact, screened payloads for the optional LLM description step.

    Each item: ``{"key", "name", "semantics": TableSemantics | dict, "description"?, "columns": [{"name",
    "data_type", "sensitivity"?, "pii_category"?, "description"?}]}``. Output keeps the table name,
    the deterministic baseline and at most ``max_columns`` column names/types, never values, and
    (by default) no sensitive columns — their names alone can describe personal data.
    """
    max_tables = max(1, int(max_tables))
    payloads: list[dict[str, Any]] = []
    for it in items:
        cols_out: list[dict[str, Any]] = []
        omitted = 0
        for c in it.get("columns") or []:
            if exclude_sensitive and _is_sensitive(c):
                omitted += 1
                continue
            if len(cols_out) >= max_columns:
                omitted += 1
                continue
            entry = {"name": screen_text(str(c.get("name", "")), max_chars=64),
                     "type": normalize_type(c.get("data_type") or c.get("type"))}
            if c.get("description"):
                entry["comment"] = screen_text(c["description"], max_chars=120)
            cols_out.append(entry)
        p: dict[str, Any] = {"key": it.get("key") or it.get("name"),
                             "table": screen_text(str(it.get("name") or it.get("key") or ""), max_chars=128),
                             "baseline": _baseline(it.get("semantics") or it.get("baseline")),
                             "columns": cols_out}
        if it.get("description"):
            p["source_comment"] = screen_text(it["description"])
        if omitted:
            p["columns_omitted"] = omitted
        payloads.append(p)
    return [payloads[i:i + max_tables] for i in range(0, len(payloads), max_tables)]
