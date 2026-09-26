"""Where a recipe runs, and running it (ADR-0023 decisions 2-4).

Pushdown first (ADR-0004): when every source of the recipe is one source of the scope and the
source's dialect has a recipe compiler, the recipe compiles to that dialect and runs through
`QueryGateway.execute` (one gateway, CLAUDE.md rule 4). Otherwise it falls back to a snapshot:
each input is read through the gateway with its own source's scope, stored as an immutable,
content-addressed snapshot, and the DuckDB form of the recipe runs over those snapshots only, in the
sandboxed in-memory DuckDB engine on the `compute` workload queue (isolated pools are P7-06). The
plan records why it fell back.

A snapshot is `{columns, rows}` with the rows sorted, named by the SHA-256 of that content: the same
data is the same snapshot, and a file whose content no longer matches its name is refused.
"""
from __future__ import annotations

import gzip
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlglot import exp

from analystos.contracts.policy import DataScope
from analystos.contracts.recipe import ValidatedRecipe, family
from analystos.core.errors import Conflict, Forbidden, InvalidInput
from analystos.core.ids import stable_hash
from analystos.recipes.compiler import COMPILER_DIALECTS, Compiler, col

SNAPSHOT_DIALECT = "duckdb"


@dataclass
class ExecutionPlan:
    engine: str  # "sql" (pushdown through the gateway) | "duckdb" (snapshot on the compute queue)
    dialect: str
    source_id: str | None
    sources: dict[str, str]  # asset -> source id
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"engine": self.engine, "dialect": self.dialect, "source_id": self.source_id,
                "sources": dict(sorted(self.sources.items())), "pushdown": self.engine == "sql",
                "fallback_reasons": list(self.reasons)}


def check_scope(validated: ValidatedRecipe, scope: DataScope) -> dict[str, str]:
    """Every source asset is authorized and every declared source column exists and is readable."""
    sources: dict[str, str] = {}
    denied = set(scope.denied_columns)
    for node in validated.sources():
        if node.asset not in scope.assets:
            raise Forbidden(f"asset {node.asset} (node {node.id}) is not in the authorized scope")
        known = set(scope.columns.get(node.asset) or [])
        missing = [c.name for c in node.output_schema or [] if c.name not in known]
        if missing:
            raise InvalidInput(f"node {node.id}: {node.asset} has no column {', '.join(missing)} (the source changed or "
                               "the recipe is wrong)", details={"node": node.id, "missing": missing})
        blocked = [c.name for c in node.output_schema or [] if f"{node.asset}.{c.name}" in denied or f"*.{c.name}" in denied]
        if blocked:
            raise Forbidden(f"node {node.id}: column {', '.join(blocked)} of {node.asset} is not readable under the "
                            "workspace policy")
        sources[node.asset] = scope.asset_sources.get(node.asset) or (scope.source_ids[0] if scope.source_ids else "")
    return sources


def plan_execution(validated: ValidatedRecipe, scope: DataScope, *, prefer: str | None = None) -> ExecutionPlan:
    sources = check_scope(validated, scope)
    distinct = sorted(set(sources.values()))
    reasons: list[str] = []
    dialect = scope.source_dialects.get(distinct[0], "postgres") if len(distinct) == 1 else None
    if prefer == "duckdb":
        reasons.append("the DuckDB snapshot engine was requested")
    elif prefer not in (None, "sql", "auto"):
        raise InvalidInput(f"engine must be sql, duckdb or auto (got {prefer})")
    if len(distinct) > 1:
        reasons.append(f"the inputs span {len(distinct)} sources ({', '.join(distinct)}); no single source can run the "
                       "recipe in place")
    elif dialect not in COMPILER_DIALECTS:
        reasons.append(f"there is no recipe compiler for the source dialect {dialect}")
    elif dialect == "tsql":
        booleans = [f"{nid}.{c.name}" for nid, cols in validated.schemas.items() for c in cols if family(c.type) == "boolean"]
        if booleans:
            reasons.append(f"tsql has no boolean values in a select list ({', '.join(booleans[:3])})")
    if reasons:
        if prefer == "sql":
            raise InvalidInput("the recipe cannot run in place: " + "; ".join(reasons))
        return ExecutionPlan("duckdb", SNAPSHOT_DIALECT, None, sources, reasons)
    return ExecutionPlan("sql", dialect or "postgres", distinct[0], sources, [])


# ------------------------------------------------------------------------------------ snapshots
def _sort_key(row: list[Any]) -> str:
    return json.dumps(row, sort_keys=True, default=str)


class SnapshotStore:
    """Content-addressed, write-once snapshot files under `root` (shared by the API and compute workers)."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def path(self, digest: str) -> Path:
        if not (len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)):
            raise InvalidInput("not a snapshot id")
        return self.root / digest[:2] / f"{digest}.json.gz"

    def put(self, columns: list[str], rows: list[list[Any]]) -> str:
        body = {"columns": list(columns), "rows": sorted((list(r) for r in rows), key=_sort_key)}
        digest = stable_hash(body)
        path = self.path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".tmp{os.getpid()}")
            tmp.write_bytes(gzip.compress(json.dumps(body, default=str).encode(), mtime=0))
            os.replace(tmp, path)
        return digest

    def get(self, digest: str) -> tuple[list[str], list[list[Any]]]:
        path = self.path(digest)
        if not path.exists():
            raise Conflict(f"snapshot {digest[:12]} is no longer available; run the recipe again")
        body = json.loads(gzip.decompress(path.read_bytes()))
        if stable_hash(body) != digest:
            raise Conflict(f"snapshot {digest[:12]} does not match its content hash; it was modified and is refused")
        return body["columns"], body["rows"]


def default_store(settings: Any) -> SnapshotStore:
    return SnapshotStore(Path(settings.artifact_dir) / "recipe_snapshots")


def run_snapshot_job(job: dict[str, Any], *, store: SnapshotStore | None = None) -> dict[str, Any]:
    """The compute-queue job: the DuckDB statement over verified snapshots, result stored as a snapshot.
    `job` = {sql, tables: {asset: digest}, max_rows, timeout_seconds, artifact_dir}."""
    from analystos.engines.base import Limits
    from analystos.engines.duckdb import DuckDBEngine
    from analystos.gateway.types import ValidatedSQL

    store = store or SnapshotStore(Path(job["artifact_dir"]) / "recipe_snapshots")
    tables = {asset: store.get(digest) for asset, digest in sorted(job["tables"].items())}
    sql = job["sql"]
    stmt = ValidatedSQL(original_sql=sql, executable_sql=sql, dialect=SNAPSHOT_DIALECT, source_id="recipe.snapshot",
                        referenced_assets=sorted(tables), fingerprint=stable_hash(sql))
    max_rows = int(job["max_rows"])
    columns, rows = DuckDBEngine().federate(tables, stmt, limits=Limits(max_rows=max_rows,
                                                                         timeout_seconds=int(job["timeout_seconds"])))
    truncated = len(rows) > max_rows
    digest = store.put(columns, rows[:max_rows])
    return {"result": digest, "columns": columns, "row_count": min(len(rows), max_rows), "truncated": truncated}


# ------------------------------------------------------------------------------------ executor
@dataclass
class Result:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    engine: str
    query_ids: list[str] = field(default_factory=list)
    snapshot: str | None = None  # result snapshot id (duckdb engine)


class RecipeExecutor:
    """Runs compiled statements of one recipe under one plan. Every read of source data is a
    `QueryGateway.execute` call; the snapshot engine never opens a source connection."""

    def __init__(self, gateway: Any, scope: DataScope, validated: ValidatedRecipe, plan: ExecutionPlan, *, actor: str,
                 store: SnapshotStore, compute: Any = None, run_id: str | None = None, max_rows: int | None = None,
                 timeout_seconds: int | None = None) -> None:
        self.gateway, self.scope, self.v, self.plan = gateway, scope, validated, plan
        self.actor, self.store, self.run_id = actor, store, run_id
        self.compute = compute or (lambda job: run_snapshot_job(job, store=store))
        settings = gateway.settings
        self.max_rows = min(x for x in (max_rows, scope.max_rows, settings.query_max_rows) if x)
        self.timeout = min(x for x in (timeout_seconds, scope.timeout_seconds, settings.query_timeout_seconds) if x)
        self.compiler = Compiler(validated, plan.dialect)
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.query_ids: list[str] = []

    # -------------------------------------------------------------- snapshot inputs
    def take_snapshots(self) -> dict[str, dict[str, Any]]:
        """Each source asset read once through the gateway (the declared columns of every node that
        reads it), stored as an immutable snapshot. A truncated read is refused: a partial input changes
        joins and aggregates, so a recipe never runs on one."""
        if self.snapshots:
            return self.snapshots
        wanted: dict[str, list[str]] = {}
        pins: dict[str, str] = {}
        for node in self.v.sources():
            cols = wanted.setdefault(node.asset, [])
            cols += [c.name for c in node.output_schema or [] if c.name not in cols]
            if node.snapshot:
                pins[node.asset] = node.snapshot
        for asset, cols in sorted(wanted.items()):
            source_id = self.plan.sources[asset]
            dialect = self.scope.source_dialects.get(source_id, "postgres")
            schema, table = asset.split(".", 1)
            stmt = exp.select(*[col(c) for c in cols]).from_(exp.Table(this=exp.to_identifier(table, quoted=True),
                                                                        db=exp.to_identifier(schema, quoted=True)))
            runner = self.gateway.run_sql_for(self.scope, actor=self.actor, run_id=self.run_id, source_id=source_id)
            res = runner(stmt.sql(dialect=dialect), purpose="recipe.snapshot", max_rows=self.max_rows, use_cache=False)
            self.query_ids.append(res.query_id)
            if res.truncated:
                raise InvalidInput(f"{asset} has more than {self.max_rows} rows, the snapshot limit; a partial input would "
                                   "change the result, so the recipe was not run. Filter the source or run it in place.")
            digest = self.store.put(res.columns, res.rows)
            if asset in pins and pins[asset] != digest:
                raise Conflict(f"{asset} changed since the recipe pinned snapshot {pins[asset][:12]} (now {digest[:12]})")
            self.snapshots[asset] = {"snapshot": digest, "rows": res.row_count, "columns": res.columns,
                                     "query_id": res.query_id}
        return self.snapshots

    # -------------------------------------------------------------- statements
    def query(self, tree: exp.Select, *, purpose: str, max_rows: int | None = None, use_cache: bool = False) -> Result:
        cap = min(max_rows or self.max_rows, self.max_rows)
        if self.plan.engine == "sql":
            runner = self.gateway.run_sql_for(self.scope, actor=self.actor, run_id=self.run_id, source_id=self.plan.source_id)
            res = runner(self.compiler.sql(tree), purpose=purpose, max_rows=cap, use_cache=use_cache)
            self.query_ids.append(res.query_id)
            return Result(res.columns, res.rows, res.truncated, "sql", [res.query_id])
        snaps = self.take_snapshots()
        job = {"sql": self.compiler.sql(tree), "tables": {a: s["snapshot"] for a, s in snaps.items()}, "max_rows": cap,
               "timeout_seconds": self.timeout, "artifact_dir": str(self.store.root.parent)}
        out = self.compute(job)
        columns, rows = self.store.get(out["result"])
        return Result(columns, rows, bool(out["truncated"]), "duckdb", [], snapshot=out["result"])

    def preflight(self, join_id: str) -> dict[str, Any]:
        """Measure a join before it runs; refuse when the observed cardinality violates the declared one."""
        node = self.v.nodes[join_id]
        res = self.query(self.compiler.preflight_query(join_id), purpose=f"recipe.preflight:{join_id}", max_rows=1)
        stats = {k: int(v or 0) for k, v in zip(res.columns, res.rows[0], strict=True)} if res.rows else {}
        return judge_join(join_id, node.expected_cardinality, stats)


def judge_join(join_id: str, declared: str, stats: dict[str, int]) -> dict[str, Any]:
    left_unique = stats.get("left_keys", 0) == stats.get("left_key_rows", 0)
    right_unique = stats.get("right_keys", 0) == stats.get("right_key_rows", 0)
    observed = {(True, True): "one_to_one", (False, True): "many_to_one", (True, False): "one_to_many",
                (False, False): "many_to_many"}[(left_unique, right_unique)]
    allowed = {"one_to_one": {"one_to_one"}, "many_to_one": {"one_to_one", "many_to_one"},
               "one_to_many": {"one_to_one", "one_to_many"},
               "many_to_many": {"one_to_one", "many_to_one", "one_to_many", "many_to_many"}}[declared]
    matched = stats.get("matched_left_rows", 0)
    left_rows = stats.get("left_rows", 0)
    result = {"join": join_id, "declared": declared, "observed": observed, "ok": observed in allowed, **stats,
              "unmatched_left_rows": left_rows - matched,
              "left_overlap_pct": round(100.0 * stats.get("matched_keys", 0) / stats["left_keys"], 2) if stats.get("left_keys") else None,
              "row_multiplication": round(stats.get("joined_rows", 0) / matched, 4) if matched else None}
    return result
