"""Context compiler (P4-T03, spec v3 §4.3): the only way run context reaches a model prompt.

Given a purpose, the text the call is about (objective, question) and the caller's mandatory
inputs, it builds the prompt context under the purpose profile's budget
(`platform settings → context.profiles`):

* a **relevant-column digest** of the authorized catalog: columns ranked by the objective's terms,
  by columns that matching knowledge maps to, then by analytical usefulness; SQL purposes keep only
  the tables the question or SQL references;
* **knowledge sections** (glossary, business rules, metrics, prior findings, negative knowledge,
  episodes) scored deterministically against the objective; a section with nothing relevant says
  `NO_MATCH` instead of padding the prompt with unrelated grounding;
* **receipts** for every included item (id, section, source, sha256) so the call record and the UI
  can show which context was used, and an **omitted** list for everything that did not fit;
* it **fails visibly** (`ContextOverBudget`) when the mandatory part alone exceeds the budget; the
  caller then takes the deterministic path and records the refusal. Nothing is cut mid-JSON.

The output is split for prompt caching (P4-T04): `header` is stable per workspace (workspace,
domain packs, dialects) and goes right after the static system text; `body` is the volatile part
and goes last. Selection is pure (`compile_context`); `load_knowledge` is the only DB access.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from analystos.contracts.platform import PurposeProfile
from analystos.core.errors import ContextOverBudget

NO_MATCH = "NO_MATCH"

# context_entry.kind -> compiler section. Crawler `table` entries are not listed: the catalog
# section already carries the same structure, ranked, so they would only duplicate it.
KIND_SECTIONS = {"term": "glossary", "definition": "glossary", "note": "glossary", "rule": "business_rules",
                 "metric": "metrics", "episode": "episodes"}
KNOWLEDGE_SECTIONS = ("glossary", "business_rules", "metrics", "prior_findings", "negative_knowledge", "episodes")

_SEMANTIC_PRIORITY = {"boolean": 0, "datetime": 1, "categorical": 2, "numeric": 3, "text": 5, "id": 6}
_STOP = {"the", "and", "for", "with", "that", "this", "from", "what", "which", "into", "over", "are", "was", "were",
         "how", "why", "does", "did", "our", "all", "any", "per", "by", "of", "to", "in", "on", "is", "it", "be",
         "find", "show", "drivers", "driver", "analyse", "analyze", "between", "across", "most", "more", "less"}
_WORD = re.compile(r"[a-z0-9]+")


def terms(text: str | None) -> set[str]:
    """Normalised content words: lower-case, split on non-alphanumerics (so snake_case splits),
    stop words and 1–2 letter tokens dropped, a plural `s` removed."""
    out = set()
    for w in _WORD.findall((text or "").lower()):
        if len(w) < 3 or w in _STOP:
            continue
        if len(w) > 4 and w.endswith("es") and not w.endswith("ses"):
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def compact(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), ensure_ascii=False)


def _size(value: Any) -> int:
    return len(compact(value)) + 1


def _sha(value: Any) -> str:
    return hashlib.sha256((value if isinstance(value, str) else compact(value)).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class KnowledgeItem:
    id: str
    section: str
    name: str
    text: str
    source: str
    mapped_columns: tuple[str, ...] = ()
    trusted: bool = True


@dataclass
class CompiledContext:
    purpose: str
    header: dict[str, Any]
    body: dict[str, Any]
    receipts: list[dict[str, Any]] = field(default_factory=list)
    omitted: list[dict[str, Any]] = field(default_factory=list)
    no_match: list[str] = field(default_factory=list)
    budget_chars: int = 0
    mandatory_chars: int = 0
    refused: str | None = None  # set by callers that turn ContextOverBudget into a refusal

    @property
    def chars(self) -> int:
        return (len(compact(self.header)) if self.header else 0) + len(compact(self.body))

    def summary(self) -> dict[str, Any]:
        """What the call record and the UI show as "context used"."""
        return {"purpose": self.purpose, "chars": self.chars, "budget_chars": self.budget_chars,
                "receipts": self.receipts, "omitted": self.omitted, "no_match": self.no_match}


# ------------------------------------------------------------------------------------ scoring
def item_score(item: KnowledgeItem, query: set[str], in_scope_columns: set[str]) -> float:
    """Share of the query terms the item mentions (name counts double), plus 0.5 when it maps to a
    column in scope that the query is about. Deterministic: the same inputs always select the same context."""
    if not query:
        return 0.0
    name_terms = terms(item.name)
    body_terms = terms(item.text)
    hit = len(query & name_terms) * 2 + len(query & (body_terms - name_terms))
    score = min(1.0, hit / len(query))
    # mapping bonus only for a mapped in-scope column the query is about (a mapping alone is not relevance)
    if in_scope_columns and any(_col_key(c) in in_scope_columns and terms(_col_key(c)) & query for c in item.mapped_columns):
        score = min(1.0, score + 0.5)
    return round(score, 4)


def _col_key(ref: str) -> str:
    """`schema.table.column` or a bare column name -> the bare column name, lower-case."""
    return ref.rsplit(".", 1)[-1].lower()


def _col_ref(ref: str) -> tuple[str | None, str]:
    """`[schema.]table.column` -> (table, column); a bare column -> (None, column). Lower-case."""
    parts = ref.lower().split(".")
    return (parts[-2] if len(parts) >= 2 else None), parts[-1]


def _boosted(table: str, entry: Any, boost: set[tuple[str | None, str]]) -> bool:
    name = _col_name(entry).lower()
    return (table, name) in boost or (None, name) in boost


def _col_name(entry: Any) -> str:
    return str(entry.get("name") if isinstance(entry, dict) else entry)


def _col_relevance(table: str, entry: Any, query: set[str], boost: set[tuple[str | None, str]]) -> int:
    name = _col_name(entry)
    meaning = entry.get("meaning", "") if isinstance(entry, dict) else ""
    return len(terms(f"{name} {meaning}") & query) + (2 if _boosted(table, entry, boost) else 0)


def _col_key_sort(table: str, entry: Any, query: set[str], boost: set[tuple[str | None, str]],
                  ordinal: int) -> tuple[int, int, int]:
    semantic = entry.get("semantic_type") if isinstance(entry, dict) else None
    return -_col_relevance(table, entry, query, boost), _SEMANTIC_PRIORITY.get(semantic or "", 4), ordinal


_STATS = ("distinct", "null_rate", "values")


def _round(value: Any) -> Any:
    """Profile floats to 4 significant digits: `3.37625000000001` costs tokens and says nothing more."""
    return float(f"{value:.4g}") if isinstance(value, float) else value


def _render_column(entry: Any, detail: str) -> Any:
    """names: the name only · columns: name, semantic type, meaning · stats: + distinct, null
    rate, datetime range, category values (what hypothesis specs need) · profile: every field the
    catalog carries (SQL generation needs the physical type)."""
    if not isinstance(entry, dict):
        return str(entry)
    if detail == "names":
        return entry.get("name")
    if detail == "profile":
        return {k: _round(v) for k, v in entry.items() if v is not None}
    out = {"name": entry.get("name"), "semantic_type": entry.get("semantic_type") or entry.get("type")}
    if entry.get("meaning"):
        out["meaning"] = entry["meaning"]
    if detail == "stats":
        out.update({k: entry[k] for k in _STATS if entry.get(k) is not None})
        if out["semantic_type"] == "datetime":
            out.update({k: entry[k] for k in ("min", "max") if entry.get(k) is not None})
    return out


def excerpt(text: str, limit: int) -> str:
    """Whole sentences (else whole words) up to `limit` characters — an excerpt, marked with …"""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("; "))
    if stop >= limit // 2:
        return cut[:stop + 1] + " …"
    space = cut.rfind(" ")
    return (cut[:space] if space > 0 else cut) + " …"


# ------------------------------------------------------------------------------------ compile
def compile_context(purpose: str, profile: PurposeProfile, *, objective: str, required: dict[str, Any],
                    catalog: list[dict[str, Any]] | None = None, knowledge: Iterable[KnowledgeItem] = (),
                    header: dict[str, Any] | None = None, reference_text: str | None = None,
                    limit_chars: int | None = None, min_relevance: float = 0.15,
                    generic_terms: Iterable[str] = ()) -> CompiledContext:
    """Select the prompt context for one call. Raises ContextOverBudget when `required` (plus the
    minimal catalog when the profile has one) cannot fit, instead of silently cutting it."""
    budget = min(profile.max_chars, limit_chars) if limit_chars else profile.max_chars
    header = dict(header or {})
    query = terms(objective) | terms(reference_text)
    catalog = [t for t in (catalog or []) if isinstance(t, dict)] if "catalog" in profile.sections else []
    in_scope = {_col_name(c).lower() for t in catalog for c in t.get("columns") or []}
    # Table names occur in every column description and most knowledge of a domain (a table named "orders" in a retail workspace),
    # so they select tables but carry no signal for columns or knowledge.
    generic = set(generic_terms) | {w for t in catalog for w in terms(str(t.get("asset") or "").split(".")[-1])}
    focus = (query - generic) or query

    # knowledge candidates, scored; matching items' mapped columns boost the column ranking
    scored: dict[str, list[tuple[float, KnowledgeItem]]] = {s: [] for s in profile.sections if s in KNOWLEDGE_SECTIONS}
    for item in knowledge:
        if item.section in scored:
            score = item_score(item, focus, in_scope)
            if score >= min_relevance and score > 0:
                scored[item.section].append((score, item))
    boost = {_col_ref(c) for items in scored.values() for _, i in items for c in i.mapped_columns}

    receipts: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    no_match: list[str] = []
    body: dict[str, Any] = dict(required)

    # catalog: rank tables and columns, cap per profile; the names-only form is mandatory
    tables = _select_tables(catalog, query, focus, boost, profile, reference_text, omitted, no_match)
    minimal: list[dict[str, Any]] = []
    full: list[dict[str, Any]] = []
    for t, cols in tables:
        head = {k: t[k] for k in ("asset", "business_name", "row_count") if t.get(k) is not None}
        minimal.append({**head, "columns": [_render_column(c, "names") for c in cols]})
        full.append({**head, "columns": [_render_column(c, profile.catalog_detail) for c in cols]})
    if tables:
        body["catalog"] = minimal
    mandatory = (_size(header) if header else 0) + _size(body)
    if mandatory > budget:
        raise ContextOverBudget(
            f"mandatory context for {purpose} is {mandatory} chars, above its budget of {budget}",
            details={"purpose": purpose, "mandatory_chars": mandatory, "budget_chars": budget,
                     "required_keys": sorted(required)})

    def used() -> int:
        return (_size(header) if header else 0) + _size(body)

    # upgrade tables to the profile's detail, most relevant first, while they fit
    for i, (t, _) in enumerate(tables):
        if full[i] != minimal[i]:
            body["catalog"][i] = full[i]
            if used() > budget:
                body["catalog"][i] = minimal[i]
                omitted.append({"section": "catalog", "asset": t.get("asset"), "detail": f"{profile.catalog_detail} (names only sent)"})
        entry = body["catalog"][i]
        receipts.append({"id": f"asset:{t.get('asset')}", "section": "catalog", "kind": "table", "source": "catalog",
                         "columns": len(entry["columns"]), "sha256": _sha(entry)})

    # knowledge sections in profile order: best score first, whole items, capped
    for section in profile.sections:
        if section not in scored:
            continue
        ranked = sorted(scored[section], key=lambda si: (-si[0], si[1].name))
        if not ranked:
            body[section] = NO_MATCH
            no_match.append(section)
            continue
        body[section] = []
        for n, (score, item) in enumerate(ranked):
            rendered = {"id": item.id, "name": item.name, "text": excerpt(item.text, profile.item_chars)}
            if item.mapped_columns:
                rendered["columns"] = list(item.mapped_columns)[:8]
            if n >= profile.max_items_per_section:
                omitted.append({"section": section, "id": item.id, "name": item.name, "reason": "section item cap"})
                continue
            body[section].append(rendered)
            if used() > budget:
                body[section].pop()
                omitted.append({"section": section, "id": item.id, "name": item.name, "reason": "budget"})
                continue
            receipts.append({"id": item.id, "section": section, "kind": section, "name": item.name, "source": item.source,
                             "score": score, "trusted": item.trusted, "sha256": _sha(item.text),
                             "excerpt": len(rendered["text"]) < len(" ".join(item.text.split()))})
        if not body[section]:
            body[section] = NO_MATCH  # candidates existed but none fit: say so, the omitted list says why
            no_match.append(section)

    if omitted:
        body["omitted"] = _omitted_for_model(omitted)
        # the omitted note itself must fit: give back the lowest-ranked knowledge items until it does
        while used() > budget and _drop_last_knowledge(body, profile, receipts, omitted):
            body["omitted"] = _omitted_for_model(omitted)
    compiled = CompiledContext(purpose=purpose, header=header, body=body, receipts=receipts, omitted=omitted,
                               no_match=no_match, budget_chars=budget, mandatory_chars=mandatory)
    return compiled


def _select_tables(catalog: list[dict[str, Any]], query: set[str], focus: set[str], boost: set[tuple[str | None, str]],
                   profile: PurposeProfile, reference_text: str | None, omitted: list[dict[str, Any]],
                   no_match: list[str]) -> list[tuple[dict[str, Any], list[Any]]]:
    ref = (reference_text or "").lower()
    ranked = []
    drop = set(profile.drop_semantic_types)
    for pos, t in enumerate(catalog):
        asset = str(t.get("asset") or "")
        table = asset.split(".")[-1].lower()
        cols = [c for c in t.get("columns") or [] if not (isinstance(c, dict) and c.get("semantic_type") in drop)]
        if len(cols) < len(t.get("columns") or []):
            omitted.append({"section": "catalog", "asset": asset, "reason": f"semantic types {sorted(drop)} not used by this purpose",
                            "columns": [_col_name(c) for c in t["columns"] if c not in cols]})
        col_rel = sum(_col_relevance(table, c, focus, boost) for c in cols)
        name_rel = len(terms(f"{asset.split('.', 1)[-1]} {t.get('business_name') or ''}") & query)
        named = bool(asset) and (asset.lower() in ref or f" {asset.split('.', 1)[-1].lower()}" in f" {ref}")
        ranked.append((pos, t, cols, col_rel + 2 * name_rel + (10 if named else 0), named))
    if profile.referenced_only and ranked:
        hits = [r for r in ranked if r[3] > 0]
        if hits:
            for r in ranked:
                if r[3] <= 0:
                    omitted.append({"section": "catalog", "asset": r[1].get("asset"), "reason": "not referenced"})
            ranked = hits
        else:
            no_match.append("catalog")  # nothing named or matched: every authorized table, names first
    ranked.sort(key=lambda r: (-r[3], r[0]))
    out = []
    for _, t, cols, _, _ in ranked:
        table = str(t.get("asset") or "").split(".")[-1].lower()
        order = sorted(range(len(cols)), key=lambda i: _col_key_sort(table, cols[i], focus, boost, i))
        keep = [cols[i] for i in order[:profile.max_columns_per_table]]
        dropped = [_col_name(cols[i]) for i in order[profile.max_columns_per_table:]]
        if dropped:
            omitted.append({"section": "catalog", "asset": t.get("asset"), "columns": dropped, "reason": "column cap"})
        out.append((t, keep))
    return out


def _omitted_for_model(omitted: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact form for the prompt: the model needs to know the context is partial, not the ids."""
    note: dict[str, Any] = {}
    for o in omitted:
        if o["section"] == "catalog":
            if o.get("columns"):
                note.setdefault("columns_not_sent", {})[str(o.get("asset"))] = len(o["columns"])
            elif o.get("detail"):
                note.setdefault("tables_names_only", []).append(str(o.get("asset")))
            else:
                note.setdefault("tables_not_sent", []).append(str(o.get("asset")))
        else:
            note[o["section"]] = note.get(o["section"], 0) + 1
    return note


def _drop_last_knowledge(body: dict[str, Any], profile: PurposeProfile, receipts: list[dict[str, Any]],
                         omitted: list[dict[str, Any]]) -> bool:
    for section in reversed(profile.sections):
        items = body.get(section)
        if isinstance(items, list) and items:
            item = items.pop()
            receipts[:] = [r for r in receipts if not (r["id"] == item["id"] and r["section"] == section)]
            omitted.append({"section": section, "id": item["id"], "name": item["name"], "reason": "budget"})
            if not items:
                body[section] = NO_MATCH
            return True
    return False


# ------------------------------------------------------------------------------------ knowledge
def load_knowledge(session: Any, workspace_id: str, sections: Iterable[str], *, run_id: str | None = None,
                   per_kind: int = 300) -> list[KnowledgeItem]:
    """Candidate items for the requested sections: the workspace's context entries, the knowledge
    packs it sees (the platform pack holds installed domain-pack knowledge; P4-K01), verified
    findings and rejected hypotheses of *other* runs."""
    from sqlalchemy import select

    from analystos.db.models import Hypothesis, Insight
    from analystos.knowledge.entries import pack_entries, workspace_rows

    wanted = set(sections)
    out: list[KnowledgeItem] = []
    kinds = [k for k, s in KIND_SECTIONS.items() if s in wanted]
    for kind in kinds:
        limit = 20 if kind == "episode" else per_kind
        entries = workspace_rows(session, workspace_id, kinds=[kind])[:limit]
        entries += pack_entries(session, workspace_id, kinds=[kind])[:per_kind]
        for e in entries:
            out.append(KnowledgeItem(id=e.id, section=KIND_SECTIONS[kind], name=e.name,
                                     text=" ".join([e.body or "", *(f"({s})" for s in e.synonyms)]),
                                     source=e.origin or "user", mapped_columns=tuple(e.mapped_columns),
                                     trusted=bool(e.trusted)))
    if "prior_findings" in wanted:
        q = select(Insight).where(Insight.workspace_id == workspace_id, Insight.status == "verified")
        if run_id:
            q = q.where(Insight.run_id != run_id)
        for i in session.scalars(q.order_by(Insight.created_at.desc()).limit(50)):
            out.append(KnowledgeItem(id=f"insight:{i.id}", section="prior_findings", name=i.title, text=i.finding,
                                     source=f"run:{i.run_id}"))
    if "negative_knowledge" in wanted:
        q = select(Hypothesis).where(Hypothesis.workspace_id == workspace_id, Hypothesis.status == "rejected")
        if run_id:
            q = q.where(Hypothesis.run_id != run_id)
        seen: set[str] = set()
        for h in session.scalars(q.order_by(Hypothesis.created_at.desc()).limit(100)):
            if h.statement in seen:
                continue
            seen.add(h.statement)
            spec = h.spec or {}
            cols = tuple(str(spec[k]) for k in ("outcome", "segment") if isinstance(spec.get(k), str))
            out.append(KnowledgeItem(id=f"hypothesis:{h.id}", section="negative_knowledge", name=h.statement[:160],
                                     text=f"{h.statement} — tested, not supported.", source=f"run:{h.run_id}",
                                     mapped_columns=cols))
    return out


__all__ = ["KIND_SECTIONS", "NO_MATCH", "CompiledContext", "KnowledgeItem", "compile_context", "excerpt", "item_score",
           "load_knowledge", "terms"]
