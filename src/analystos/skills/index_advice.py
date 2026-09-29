"""Index, partitioning and clustering advice from the gateway's own query history (N-11, DataPilot
`index_advisor.py` ported by behaviour, ADR-0018).

Deterministic and advisory only. Inputs are what the platform already holds: audited executions
(`query_execution`: fingerprint, duration, SQL), dry EXPLAIN plans obtained through
`QueryGateway.explain` (never ANALYZE), and crawler table/column statistics. Output is a ranked list of
recommendations, each with its evidence (which query fingerprints, which plan nodes), an estimated
benefit and DDL *text* in the source's dialect for a person to review. Nothing here executes anything,
and the gateway validator keeps rejecting every statement this module writes.

How a recommendation is formed:
1. A query fingerprint is a candidate when its mean duration reaches `min_ms` (cache hits excluded).
2. Columns used to filter (equality or range), join, group or sort are resolved against the caller's
   scope (DataScope assets and columns; denied columns are never named) and weighted 3/3/2/1.
3. A plan that scans a relation sequentially with that column in its filter corroborates the use; a plan
   that already uses an index on it marks the column covered (no recommendation).
4. Crawler stats rule out small tables and low-selectivity single columns.
5. The dialect decides the kind: B-tree style indexes where the engine has them, clustering / sort keys
   where it does not, range partitioning for large tables filtered by a date range.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from analystos.core.ids import stable_hash

ROLE_WEIGHTS = {"eq": 3, "range": 3, "join": 3, "group": 2, "order": 1}
INDEX_DIALECTS = {"postgres", "tsql", "mysql", "oracle", "sqlite", "clickhouse"}
CLUSTER_DIALECTS = {"snowflake", "bigquery", "databricks", "redshift", "duckdb"}
PARTITION_DIALECTS = {"postgres", "tsql", "mysql", "oracle", "bigquery", "databricks"}
SCAN_NODES = {"Seq Scan", "Parallel Seq Scan"}
INDEX_NODES = {"Index Scan", "Index Only Scan", "Bitmap Index Scan", "Bitmap Heap Scan"}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,127}$")
_DATETIME = re.compile(r"date|time", re.I)
MAX_INDEX_COLUMNS = 3
MAX_CLUSTER_COLUMNS = 4


@dataclass
class QueryStat:
    """One query fingerprint over the window (no SQL text leaves the service in the evidence)."""

    fingerprint: str
    source_id: str
    dialect: str
    sql: str
    count: int
    avg_ms: float
    max_ms: float
    query_ids: list[str] = field(default_factory=list)

    @property
    def total_ms(self) -> float:
        return self.avg_ms * self.count


@dataclass
class AssetStats:
    """Crawler statistics for one asset: row count and per-column distinct count / declared type."""

    row_count: int | None = None
    distinct: dict[str, int] = field(default_factory=dict)
    types: dict[str, str] = field(default_factory=dict)
    execution_mode: str = "pushdown"


# ------------------------------------------------------------------------------ parsing
def column_uses(sql: str, dialect: str, assets: list[str], columns: dict[str, list[str]],
                denied: set[str] | None = None) -> list[tuple[str, str, str]]:
    """(asset "schema.table", column, role) for every column the statement filters, joins, groups or sorts
    on, resolved against the caller's assets only; roles are eq | range | join | group | order."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 - unparseable history rows are skipped, never guessed
        return []
    if tree is None:
        return []
    by_name: dict[str, list[str]] = defaultdict(list)
    for fq in assets:
        by_name[fq.rsplit(".", 1)[-1].lower()].append(fq)
    known = {fq.lower(): fq for fq in assets}
    aliases: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        name, db = str(table.name).lower(), str(table.db or "").lower()
        fq = known.get(f"{db}.{name}") if db else (by_name[name][0] if len(by_name.get(name, [])) == 1 else None)
        if fq is None:
            continue
        aliases[str(table.alias_or_name).lower()] = fq
        aliases.setdefault(name, fq)
    tables = sorted(set(aliases.values()))
    cols_of = {fq: {c.lower(): c for c in columns.get(fq, [])} for fq in tables}
    denied = {d.lower() for d in denied or set()}
    uses: list[tuple[str, str, str]] = []

    def resolve(column: exp.Column) -> tuple[str, str] | None:
        qualifier, name = str(column.table or "").lower(), str(column.name).lower()
        candidates = [aliases[qualifier]] if qualifier in aliases else ([] if qualifier else tables)
        hits = [fq for fq in candidates if name in cols_of.get(fq, {})]
        if len(hits) != 1:
            return None
        fq = hits[0]
        real = cols_of[fq][name]
        return None if f"{fq}.{real}".lower() in denied else (fq, real)

    def add(node: exp.Expression | None, role: str) -> None:
        if node is None:
            return
        for column in node.find_all(exp.Column):
            hit = resolve(column)
            if hit:
                uses.append((hit[0], hit[1], role))

    for where in tree.find_all(exp.Where):
        for pred in _predicates(where.this):
            add(pred, "range" if isinstance(pred, exp.GT | exp.GTE | exp.LT | exp.LTE | exp.Between) else "eq")
    for join in tree.find_all(exp.Join):
        add(join.args.get("on"), "join")
    for group in tree.find_all(exp.Group):
        add(group, "group")
    for order in tree.find_all(exp.Order):
        add(order, "order")
    return uses


def _predicates(node: exp.Expression) -> list[exp.Expression]:
    """Conjuncts of a WHERE (an OR group stays one predicate: it filters, but not as a range)."""
    if isinstance(node, exp.And):
        return _predicates(node.left) + _predicates(node.right)
    if isinstance(node, exp.Paren):
        return _predicates(node.this)
    return [node]


# ------------------------------------------------------------------------------ DDL text (never executed)
def _q(name: str, dialect: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"unsafe identifier {name!r} in advice")
    return exp.to_identifier(name, quoted=True).sql(dialect=dialect)


def _target(asset: str, dialect: str) -> str:
    schema, _, table = asset.rpartition(".")
    if dialect == "bigquery":
        for part in (schema, table):
            if part and not _IDENT.match(part):
                raise ValueError(f"unsafe identifier {part!r} in advice")
        return f"`{schema + '.' if schema else ''}{table}`"
    if dialect == "sqlite" or not schema:
        return _q(table, dialect)
    return f"{_q(schema, dialect)}.{_q(table, dialect)}"


def index_name(asset: str, columns: list[str], prefix: str = "ix_aos") -> str:
    table = asset.rsplit(".", 1)[-1]
    base = re.sub(r"[^a-z0-9_]", "_", f"{prefix}_{table}_{'_'.join(columns)}".lower())
    return f"{base[:54]}_{stable_hash([asset, columns])[:8]}"


def ddl(kind: str, dialect: str, asset: str, columns: list[str]) -> str:
    """DDL text for a person to review; identifiers are validated and quoted in the dialect. The platform
    never runs it (the gateway validator rejects DDL; no code path executes advice)."""
    target = _target(asset, dialect)
    cols = ", ".join(_q(c, dialect) for c in columns)
    name = index_name(asset, columns)
    header = "-- Advisory only: review, test and apply through your change process. AnalystOS never runs this.\n"
    if kind == "index":
        if dialect == "postgres":
            body = f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_q(name, dialect)} ON {target} ({cols});"
        elif dialect == "tsql":
            body = f"CREATE NONCLUSTERED INDEX {_q(name, dialect)} ON {target} ({cols}) WITH (ONLINE = ON);"
        elif dialect == "mysql":
            body = f"CREATE INDEX {_q(name, dialect)} ON {target} ({cols}) ALGORITHM=INPLACE LOCK=NONE;"
        elif dialect == "oracle":
            body = f"CREATE INDEX {_q(name, dialect)} ON {target} ({cols}) ONLINE;"
        elif dialect == "sqlite":
            body = f"CREATE INDEX IF NOT EXISTS {_q(name, dialect)} ON {target} ({cols});"
        elif dialect == "clickhouse":
            body = f"ALTER TABLE {target} ADD INDEX {_q(name, dialect)} ({cols}) TYPE minmax GRANULARITY 4;"
        else:
            raise ValueError(f"no index DDL for {dialect}")
    elif kind == "clustering":
        if dialect in ("snowflake", "databricks"):
            body = f"ALTER TABLE {target} CLUSTER BY ({cols});"
        elif dialect == "redshift":
            body = f"ALTER TABLE {target} ALTER COMPOUND SORTKEY ({cols});"
        elif dialect == "bigquery":
            body = (f"-- BigQuery sets clustering on a table copy (or `bq update --clustering_fields`):\n"
                    f"CREATE TABLE {_target(asset + '_clustered', dialect)} CLUSTER BY {cols} AS SELECT * FROM {target};")
        elif dialect == "duckdb":
            body = (f"-- DuckDB prunes row groups by min/max: rewrite the table sorted on the filter columns.\n"
                    f"CREATE TABLE {_target(asset + '_sorted', dialect)} AS SELECT * FROM {target} ORDER BY {cols};")
        else:
            raise ValueError(f"no clustering DDL for {dialect}")
    elif kind == "partition":
        col = _q(columns[0], dialect)
        if dialect == "postgres":
            body = (f"-- An existing Postgres table cannot be partitioned in place: create a partitioned copy,\n"
                    f"-- add range partitions (e.g. monthly), move the rows, then swap names.\n"
                    f"CREATE TABLE {_target(asset + '_part', dialect)} (LIKE {target} INCLUDING DEFAULTS) "
                    f"PARTITION BY RANGE ({col});")
        elif dialect == "tsql":
            body = (f"-- Choose boundaries, then rebuild the clustered index on the scheme.\n"
                    f"CREATE PARTITION FUNCTION {_q('pf_' + name, dialect)} (datetime2) AS RANGE RIGHT FOR VALUES (/* boundaries */);")
        elif dialect == "mysql":
            body = f"ALTER TABLE {target} PARTITION BY RANGE COLUMNS({col}) (/* partitions */);"
        elif dialect == "oracle":
            body = f"ALTER TABLE {target} MODIFY PARTITION BY RANGE ({col}) INTERVAL (NUMTOYMINTERVAL(1, 'MONTH')) ONLINE;"
        elif dialect == "bigquery":
            body = f"CREATE TABLE {_target(asset + '_part', dialect)} PARTITION BY DATE({col}) AS SELECT * FROM {target};"
        elif dialect == "databricks":
            body = (f"-- Prefer liquid clustering on the date column over Hive-style partitions for most tables.\n"
                    f"ALTER TABLE {target} CLUSTER BY ({col});")
        else:
            raise ValueError(f"no partition DDL for {dialect}")
    else:
        raise ValueError(f"unknown advice kind {kind}")
    return header + body


# ------------------------------------------------------------------------------ recommendation
def _selectivity(role: str, distinct: int | None) -> float:
    """Estimated fraction of rows a predicate keeps: 1/distinct for equality, fixed guesses otherwise."""
    if role == "eq":
        return 1.0 / distinct if distinct and distinct > 0 else 0.1
    if role == "range":
        return 0.3
    if role == "join":
        return 0.1
    return 0.5


def recommend(stats: list[QueryStat], uses: dict[str, list[tuple[str, str, str]]],
              plans: dict[str, list[dict[str, Any]]], assets: dict[str, AssetStats], dialects: dict[str, str],
              asset_sources: dict[str, str], *, min_table_rows: int = 10_000, partition_min_rows: int = 10_000_000,
              limit: int = 25) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(recommendations, skipped). `uses` and `plans` are keyed by fingerprint; `dialects` by source id.
    Each recommendation: asset, source_id, dialect, kind, columns, ddl, confidence, estimated_benefit,
    evidence {queries, plan_nodes, stats}, notes, key (stable across re-analysis)."""
    by_fp = {s.fingerprint: s for s in stats}
    col_roles: dict[tuple[str, str], Counter] = defaultdict(Counter)
    col_fps: dict[tuple[str, str], set[str]] = defaultdict(set)
    combos: dict[tuple[str, tuple[str, ...]], set[str]] = defaultdict(set)
    for fp, items in uses.items():
        filters: dict[str, dict[str, str]] = defaultdict(dict)
        for fq, col, role in items:
            col_roles[(fq, col)][role] += 1
            col_fps[(fq, col)].add(fp)
            if role in ("eq", "range"):
                current = filters[fq].get(col)
                filters[fq][col] = "eq" if "eq" in (current, role) else role
        for fq, cols in filters.items():
            if len(cols) >= 2:
                st = assets.get(fq) or AssetStats()
                eqs = sorted((c for c, r in cols.items() if r == "eq"), key=lambda c: (-(st.distinct.get(c) or 0), c))
                ranges = sorted(c for c, r in cols.items() if r == "range")
                ordered = (eqs + ranges[:1])[:MAX_INDEX_COLUMNS]
                if len(ordered) >= 2:
                    combos[(fq, tuple(ordered))].add(fp)

    def plan_evidence(fq: str, cols: list[str], fps: set[str]) -> tuple[list[dict[str, Any]], bool, str | None]:
        table = fq.rsplit(".", 1)[-1].lower()
        nodes, seq, covered = [], False, None
        for fp in sorted(fps):
            for scan in plans.get(fp, []):
                if scan["relation"].lower() not in (fq.lower(), table):
                    continue
                if scan["node"] in SCAN_NODES and set(map(str.lower, scan["filter_columns"])) & {c.lower() for c in cols}:
                    seq = True
                    nodes.append({"fingerprint": fp, **scan})
                elif scan["node"] in INDEX_NODES and scan["index_columns"] and \
                        cols[0].lower() in map(str.lower, scan["index_columns"]):
                    covered = scan.get("index") or "an existing index"
                    nodes.append({"fingerprint": fp, **scan})
        return nodes[:10], seq, covered

    def query_evidence(fps: set[str]) -> list[dict[str, Any]]:
        rows = [by_fp[fp] for fp in fps if fp in by_fp]
        rows.sort(key=lambda s: -s.total_ms)
        return [{"fingerprint": s.fingerprint, "executions": s.count, "avg_ms": round(s.avg_ms, 1),
                 "max_ms": round(s.max_ms, 1), "query_ids": s.query_ids[:5]} for s in rows[:10]]

    recs: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    def build(fq: str, kind: str, cols: list[str], fps: set[str], roles: dict[str, int], reason: str) -> None:
        source_id = asset_sources.get(fq, "")
        dialect = dialects.get(source_id, "postgres")
        st = assets.get(fq) or AssetStats()
        base = {"asset": fq, "columns": cols, "kind": kind, "source_id": source_id, "dialect": dialect}
        if st.row_count is not None and st.row_count < min_table_rows:
            skipped.append({**base, "reason": f"{st.row_count:,} rows: a scan of a table this small is already cheap"})
            return
        nodes, seq, covered = plan_evidence(fq, cols, fps)
        if covered and kind == "index":
            skipped.append({**base, "reason": f"the plan already uses {covered} for {cols[0]}"})
            return
        if kind == "index" and len(cols) == 1 and set(roles) <= {"eq"}:
            distinct = st.distinct.get(cols[0])
            if distinct is not None and st.row_count and distinct <= 10 and distinct / st.row_count < 0.001:
                skipped.append({**base, "reason": f"{cols[0]} has {distinct} distinct values: an index would rarely "
                                                  "beat a scan (consider it only inside a composite or partial index)"})
                return
        try:
            text_ = ddl(kind, dialect, fq, cols)
        except ValueError as exc:
            skipped.append({**base, "reason": str(exc)})
            return
        queries = query_evidence(fps)
        total = sum(by_fp[fp].total_ms for fp in fps if fp in by_fp)
        if kind == "index":
            kept = 1.0
            for c in cols:
                kept *= _selectivity("eq" if roles.get("eq") or len(cols) > 1 else next(iter(roles), "eq"),
                                     st.distinct.get(c))
            share = 0.8 if seq else 0.5
            saving = total * share * (1 - kept)
        elif kind == "partition":
            saving = total * 0.5
        else:
            saving = total * 0.4
        stats_known = st.row_count is not None and all(c in st.distinct for c in cols)
        confidence = "high" if seq and stats_known else "medium" if seq or stats_known else "low"
        notes = [reason]
        if st.execution_mode == "staged":
            notes.append("Staged copy: the loader recreates this table on each load, so apply it in the staging "
                         "build (or at the origin) rather than by hand.")
        if kind == "index" and not covered:
            notes.append("Existing indexes were not read from the source (the gateway allows no catalog queries): "
                         "check them before applying.")
        recs.append({
            **base,
            "ddl": text_,
            "confidence": confidence,
            "estimated_benefit": {"saving_ms_in_window": round(saving, 1), "query_ms_in_window": round(total, 1),
                                  "basis": "heuristic: time of matching queries x scan share x rows skipped"},
            "evidence": {"queries": queries, "plan_nodes": nodes, "roles": roles,
                         "stats": {"row_count": st.row_count, "distinct": {c: st.distinct.get(c) for c in cols}}},
            "notes": notes,
            "key": stable_hash([fq, kind, cols, dialect])[:64],
        })

    index_dialect = {fq: dialects.get(asset_sources.get(fq, ""), "postgres") for fq in {k[0] for k in col_roles}}
    leading: set[tuple[str, str]] = set()
    for (fq, cols), fps in sorted(combos.items(), key=lambda kv: -sum(by_fp[f].total_ms for f in kv[1] if f in by_fp)):
        if index_dialect.get(fq) in INDEX_DIALECTS:
            leading.add((fq, cols[0]))
            build(fq, "index", list(cols), fps, {"eq": len(fps)},
                  f"filtered together in {len(fps)} slow quer{'y' if len(fps) == 1 else 'ies'}")
    for (fq, col), roles in sorted(col_roles.items(), key=lambda kv: -sum(ROLE_WEIGHTS[r] * n for r, n in kv[1].items())):
        dialect = index_dialect.get(fq)
        if dialect not in INDEX_DIALECTS or (fq, col) in leading or not ({"eq", "range", "join"} & set(roles)):
            continue
        used = ", ".join(f"{r} x{n}" for r, n in roles.most_common())
        build(fq, "index", [col], col_fps[(fq, col)], dict(roles), f"used as {used} in slow queries")
    per_asset: dict[str, Counter] = defaultdict(Counter)
    for (fq, col), roles in col_roles.items():
        per_asset[fq][col] += sum(ROLE_WEIGHTS[r] * n for r, n in roles.items() if r in ("eq", "range", "join"))
    for fq, weights in sorted(per_asset.items()):
        dialect = index_dialect.get(fq)
        st = assets.get(fq) or AssetStats()
        fps = set().union(*(col_fps[(fq, c)] for c in weights))
        if dialect in CLUSTER_DIALECTS:
            cols = [c for c, w in weights.most_common(MAX_CLUSTER_COLUMNS) if w > 0]
            if cols:
                build(fq, "clustering", cols, fps, {}, "this engine has no secondary indexes: cluster/sort on the "
                                                       "columns slow queries filter by")
        elif dialect not in INDEX_DIALECTS and weights:
            skipped.append({"asset": fq, "columns": sorted(weights), "kind": "index", "source_id": asset_sources.get(fq, ""),
                            "dialect": dialect, "reason": f"no physical-design advice for {dialect} sources"})
        if dialect in PARTITION_DIALECTS and st.row_count is not None and st.row_count >= partition_min_rows:
            for col in sorted(weights):
                if col_roles[(fq, col)].get("range") and _DATETIME.search(st.types.get(col, "")):
                    build(fq, "partition", [col], col_fps[(fq, col)], dict(col_roles[(fq, col)]),
                          f"{st.row_count:,} rows filtered by a range on {col}")
                    break
    recs.sort(key=lambda r: (-r["estimated_benefit"]["saving_ms_in_window"], r["asset"], r["columns"]))
    return recs[:limit], skipped
