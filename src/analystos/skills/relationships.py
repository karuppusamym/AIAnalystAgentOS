"""Relationship (join key) discovery across a given set of assets, validated with SQL.

Candidate sources, in order of prior confidence:
  declared        a column's `references` ("table", "schema.table" or "table.column"; a bare table
                  resolves to the installed domain packs' key column, then the declared key, then `id`)        prior 0.9
  name_heuristic  `x_id` -> table `x` / `xs` / `xes` / `x`->`ies`, column `id` (or `x_id`)  prior 0.6
  same_name_key   a column named like another asset's declared key column                   prior 0.5

Every candidate is validated with one containment query (share of non-null FK values found among the
target's values) plus one uniqueness query per target column (cached). Cardinality:
many_to_one when the target column is unique, one_to_one when both sides are unique, else
many_to_many. confidence = (0.35 * prior + 0.65 * containment) x (1.0 if target unique else 0.8).
Heuristic candidates with containment < 0.5 are dropped; declared ones are always returned (with
their measured containment) because a broken declared reference is itself a finding.
Only assets from the given list are considered as targets.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlglot import exp

from analystos.skills.base import RunSQL
from analystos.skills.profiling import type_family
from analystos.skills.sqlbuild import (
    _check_dialect,
    case,
    cast,
    col,
    count_star,
    ident,
    is_null,
    not_null,
    num,
    table,
    to_sql,
)

PRIORS = {"declared": 0.9, "name_heuristic": 0.6, "same_name_key": 0.5}
MIN_CONTAINMENT = 0.5


class RelationshipCandidate(BaseModel):
    from_asset: str
    from_column: str
    to_asset: str
    to_column: str
    cardinality: str  # many_to_one | one_to_one | many_to_many | unknown
    confidence: float
    evidence: dict[str, Any] = Field(default_factory=dict)


def _short(asset: str) -> str:
    return asset.split(".")[-1].lower()


def _plural_forms(stem: str) -> list[str]:
    s = stem.lower()
    forms = [s, s + "s", s + "es"]
    if s.endswith("y"):
        forms.append(s[:-1] + "ies")
    return forms


def _candidates(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from analystos.capabilities.packs import hints

    key_columns = [k.lower() for k in hints().key_columns]
    by_full = {a["asset"].lower(): a for a in assets}
    by_short: dict[str, list[dict[str, Any]]] = {}
    for a in assets:
        by_short.setdefault(_short(a["asset"]), []).append(a)

    def cols(a: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {c["name"].lower(): c for c in a.get("columns", [])}

    def resolve(ref: str) -> tuple[dict[str, Any] | None, str | None]:
        r = ref.strip().lower()
        if r in by_full:
            return by_full[r], None
        parts = r.split(".")
        if len(parts) >= 2:
            head, last = ".".join(parts[:-1]), parts[-1]
            if head in by_full and last in cols(by_full[head]):
                return by_full[head], last
            if head in by_short and len(by_short[head]) == 1 and last in cols(by_short[head][0]):
                return by_short[head][0], last
        if parts[-1] in by_short and len(by_short[parts[-1]]) == 1:
            return by_short[parts[-1]][0], None
        return None, None

    def target_key(t: dict[str, Any]) -> str | None:
        tc = cols(t)
        for key in key_columns:  # a domain's surrogate key (installed packs' hints) wins over declared keys
            if key in tc:
                return tc[key]["name"]
        keys = [c["name"] for c in t.get("columns", []) if c.get("is_key")]
        if keys:
            return keys[0]
        if "id" in tc:
            return tc["id"]["name"]
        return None

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(a: dict[str, Any], c: dict[str, Any], t: dict[str, Any], tcol: str, source: str) -> None:
        key = (a["asset"], c["name"], t["asset"], tcol)
        if key in seen or (a["asset"] == t["asset"] and c["name"] == tcol):
            return
        seen.add(key)
        tc = cols(t)[tcol.lower()]
        out.append({"from": a, "col": c, "to": t, "to_col": tc, "source": source})

    for a in assets:
        for c in a.get("columns", []):
            ref = c.get("references")
            if ref:
                t, tcol = resolve(str(ref))
                if t is not None:
                    tcol = tcol or target_key(t)
                    if tcol:
                        add(a, c, t, cols(t)[tcol.lower()]["name"], "declared")
                continue
            if c.get("is_key"):
                continue
            name = c["name"].lower()
            if name.endswith("_id") and len(name) > 3:
                stem = name[:-3]
                for form in _plural_forms(stem):
                    for t in by_short.get(form, []):
                        tc = cols(t)
                        tcol = "id" if "id" in tc else name if name in tc else target_key(t)
                        if tcol:
                            add(a, c, t, tc[tcol.lower()]["name"] if tcol.lower() in tc else tcol, "name_heuristic")
            for t in assets:
                if t is a:
                    continue
                tc = cols(t)
                if name in tc and tc[name].get("is_key"):
                    add(a, c, t, tc[name]["name"], "same_name_key")
    return out


def discover_relationships(run_sql: RunSQL, assets: list[dict[str, Any]]) -> list[RelationshipCandidate]:
    """assets: [{"asset": "schema.table", "columns": [{name, data_type, is_key, references}], "row_count"}]."""
    dialect = _check_dialect(getattr(run_sql, "dialect", "duckdb"))
    uniq_cache: dict[tuple[str, str], dict[str, Any]] = {}
    results: list[RelationshipCandidate] = []
    for cand in _candidates(assets):
        a, c, t, tc = cand["from"], cand["col"], cand["to"], cand["to_col"]
        fam_f, fam_t = type_family(c.get("data_type", "")), type_family(tc.get("data_type", ""))
        mismatch = fam_f != fam_t

        def key_expr(e: exp.Expression, _m: bool = mismatch) -> exp.Expression:
            return cast(e, "text", dialect) if _m else e

        ukey = (t["asset"], tc["name"])
        if ukey not in uniq_cache:
            uq = exp.select(exp.Count(this=col(tc["name"])).as_(ident("n")),
                            exp.Count(this=exp.Distinct(expressions=[col(tc["name"])])).as_(ident("nd"))).from_(table(t["asset"]))
            sql_u = to_sql(uq, dialect)
            r = run_sql(sql_u, purpose="relationships.target_uniqueness", max_rows=1).records()
            row = {k.lower(): v for k, v in r[0].items()} if r else {}
            uniq_cache[ukey] = {"rows": int(row.get("n") or 0), "distinct": int(row.get("nd") or 0), "sql": sql_u}
        u = uniq_cache[ukey]
        parent = (exp.select(key_expr(col(tc["name"])).as_(ident("k"))).distinct().from_(table(t["asset"]))
                  .where(not_null(col(tc["name"]))))
        f = exp.column(c["name"], table="f", quoted=True)
        pk = exp.column("k", table="p", quoted=True)
        q = (exp.select(count_star().as_(ident("fk_rows")),
                        exp.Count(this=exp.Distinct(expressions=[f.copy()])).as_(ident("fk_distinct")),
                        exp.Sum(this=case([(is_null(pk.copy()), num(0))], num(1))).as_(ident("matched")))
             .from_(table(a["asset"], alias="f"))
             .join(exp.Subquery(this=parent, alias=exp.TableAlias(this=ident("p"))),
                   on=exp.EQ(this=key_expr(f.copy()), expression=pk.copy()), join_type="left")
             .where(not_null(f.copy())))
        sql = to_sql(q, dialect)
        rr = run_sql(sql, purpose="relationships.containment", max_rows=1).records()
        row = {k.lower(): v for k, v in rr[0].items()} if rr else {}
        fk_rows, fk_distinct, matched = int(row.get("fk_rows") or 0), int(row.get("fk_distinct") or 0), int(row.get("matched") or 0)
        if fk_rows == 0:
            continue
        containment = matched / fk_rows
        if cand["source"] != "declared" and containment < MIN_CONTAINMENT:
            continue
        pk_unique = u["rows"] > 0 and u["rows"] == u["distinct"]
        fk_unique = fk_distinct == fk_rows
        cardinality = ("one_to_one" if fk_unique else "many_to_one") if pk_unique else "many_to_many"
        prior = PRIORS[cand["source"]]
        conf = (0.35 * prior + 0.65 * containment) * (1.0 if pk_unique else 0.8)
        results.append(RelationshipCandidate(
            from_asset=a["asset"], from_column=c["name"], to_asset=t["asset"], to_column=tc["name"], cardinality=cardinality,
            confidence=round(conf, 3),
            evidence={"source": cand["source"], "containment": round(containment, 6), "fk_rows": fk_rows,
                      "fk_distinct": fk_distinct, "matched_rows": matched, "orphan_rows": fk_rows - matched,
                      "target_unique": pk_unique, "target_rows": u["rows"], "target_distinct": u["distinct"],
                      "type_cast": mismatch, "sql": [sql, u["sql"]]}))
    results.sort(key=lambda r: (-r.confidence, r.from_asset, r.from_column))
    return results
