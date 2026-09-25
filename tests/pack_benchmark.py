"""Domain-pack benchmark runner (spec v3 §3.6): a pack's dataset with planted effects through the
deterministic pipeline, with no services and no model.

generator -> DuckDB -> profiling (skills/profiling) -> crawler roles (skills/catalog) -> the pack's
hypothesis templates + the core role playbook (investigator) -> AnalysisSpec validation against a
scope -> run_analysis -> Benjamini-Hochberg over every test -> verify_analysis (independent method).

A hypothesis is *found* when its primary test is supported, stays significant after BH and the second
method agrees: the same gates the critic applies before a finding may be called verified.
"""
from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analystos.capabilities.packs import DomainPack  # noqa: E402
from analystos.connectors.base import DiscoveredAsset, DiscoveredColumn  # noqa: E402
from analystos.contracts.analysis import AnalysisSpec  # noqa: E402
from analystos.skills import catalog as cat  # noqa: E402
from analystos.skills.hypothesis_templates import Col  # noqa: E402
from analystos.skills.profiling import profile_asset  # noqa: E402
from skills_fixtures import DuckRunSQL  # noqa: E402

ALPHA = 0.05


def _generator(pack: DomainPack) -> Any:
    ref = pack.benchmark["dataset"]["generator"]
    if ref.startswith("python:"):
        module, _, attr = ref[len("python:"):].partition(":")
        return getattr(importlib.import_module(module), attr)
    file, _, attr = ref.partition(":")
    path = (pack.root / "benchmark" / file).resolve()
    spec = importlib.util.spec_from_file_location(f"{pack.name}_benchmark_generator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, attr)


def load_dataset(pack: DomainPack) -> DuckRunSQL:
    """Generated tables in an in-memory DuckDB under schema `raw`, then the pack's `prepare` SQL."""
    import duckdb
    import pandas as pd
    import pyarrow as pa

    ds = pack.benchmark["dataset"]
    tables = _generator(pack)(**(ds.get("args") or {}))
    con = duckdb.connect()
    con.execute("CREATE SCHEMA raw")
    con.execute(f"CREATE SCHEMA {ds.get('schema', 'bench')}")
    for name, t in tables.items():
        if isinstance(t, pa.Table):
            con.register("_tmp", t)
            con.execute(f"CREATE TABLE raw.{name} AS SELECT * FROM _tmp")
            con.unregister("_tmp")
        else:
            ddl = ", ".join(f"{c} {re.sub(r' REFERENCES .*$', '', ty)}" for c, ty in t["columns"])  # keys are not the point here
            con.execute(f"CREATE TABLE raw.{name} ({ddl})")
            con.register("_tmp", pd.DataFrame(t["rows"], columns=[c for c, _ in t["columns"]]))
            con.execute(f"INSERT INTO raw.{name} SELECT * FROM _tmp")
            con.unregister("_tmp")
    for stmt in ds.get("prepare") or []:
        con.execute(stmt)
    return DuckRunSQL(con)


def describe_table(run_sql: DuckRunSQL, fq: str) -> tuple[list[Col], int]:
    """What the crawler and profiler would have stored for `fq`: semantic types, roles, profiles."""
    schema, name = fq.split(".")
    info = run_sql.con.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = ? "
                               "AND table_name = ? ORDER BY ordinal_position", [schema, name]).fetchall()
    prof = profile_asset(run_sql, fq, [{"name": c, "data_type": t} for c, t in info])
    asset = DiscoveredAsset(source_name=name, name=name, columns=[DiscoveredColumn(name=c, data_type=cat.normalize_type(t))
                                                                  for c, t in info])
    cols = []
    for c in asset.columns:
        p = prof.column(c.name)
        role = cat.infer_column_semantics(c, asset).semantic_role
        cols.append(Col(name=c.name, semantic_type=p.semantic_type if p else None, role=role,
                        profile=p.model_dump(mode="json") if p else {}))
    return cols, prof.row_count


@dataclass
class Tested:
    proposal: dict[str, Any]
    spec: AnalysisSpec
    supported: bool
    p_value: float | None
    top: Any
    highlights: dict[str, Any] = field(default_factory=dict)
    p_adjusted: float | None = None
    agrees: bool | None = None

    @property
    def found(self) -> bool:
        return bool(self.supported and self.p_adjusted is not None and self.p_adjusted < ALPHA and self.agrees)


@dataclass
class BenchmarkResult:
    tested: list[Tested]
    rejected_by_validation: list[dict[str, Any]] = field(default_factory=list)
    planted: dict[str, bool] = field(default_factory=dict)
    nulls_rejected: dict[str, bool] = field(default_factory=dict)


def _top(stat: Any) -> Any:
    hl = stat.highlights or {}
    if hl.get("top_segment") is not None:
        return hl["top_segment"]
    groups = [g for g in stat.groups or [] if g.get("segment") is not None]
    for key in ("median", "mean", "rate"):
        vals = [g for g in groups if g.get(key) is not None]
        if vals:
            return max(vals, key=lambda g: g[key])["segment"]
    return groups[0]["segment"] if groups else None


def _matches(spec: dict[str, Any], match: dict[str, Any]) -> bool:
    col = lambda d: (d or {}).get("column")  # noqa: E731
    return (spec.get("method") == match.get("method")
            and ("outcome" not in match or col(spec.get("outcome")) == match["outcome"])
            and ("outcome_type" not in match or (spec.get("outcome") or {}).get("type") == match["outcome_type"])
            and ("segment" not in match or col(spec.get("segment")) == match["segment"])
            and ("segment_type" not in match or (spec.get("segment") or {}).get("type") == match["segment_type"])
            and bool(spec.get("filters")) == bool(match.get("filtered", False)))


def run_benchmark(pack: DomainPack) -> BenchmarkResult:
    from analystos.agents.investigator import proposals_for_table, validate_spec
    from analystos.skills.analysis import run_analysis, verify_analysis
    from analystos.skills.stats import benjamini_hochberg

    bench = pack.benchmark
    run_sql = load_dataset(pack)
    schema = bench["dataset"].get("schema", "bench")
    proposals, types, columns = [], {}, {}
    for table in bench["tables"]:
        fq = f"{schema}.{table}"
        cols, _ = describe_table(run_sql, fq)
        types[fq] = {c.name: c.semantic_type for c in cols}
        columns[fq] = [c.name for c in cols]
        proposals += proposals_for_table(fq, cols, [pack])
    for n in bench.get("null_controls") or []:
        proposals += [{"statement": f"null control {n['id']}", "spec": s, "null_control": n["id"]} for s in n.get("specs") or []]
    scope = SimpleNamespace(assets=list(columns), columns=columns, denied_columns=[])
    result = BenchmarkResult(tested=[])
    seen = set()
    for p in proposals:
        spec = AnalysisSpec.model_validate(p["spec"])
        errors = validate_spec(spec, scope, types)
        if errors:
            result.rejected_by_validation.append({"statement": p.get("statement"), "errors": errors})
            continue
        key = spec.model_dump_json()
        if key in seen:
            continue
        seen.add(key)
        out = run_analysis(spec, run_sql, alpha=ALPHA)
        result.tested.append(Tested(proposal=p, spec=spec, supported=bool(out.stat.supported), p_value=out.stat.p_value,
                                    top=_top(out.stat), highlights=dict(out.stat.highlights or {})))
    with_p = [t for t in result.tested if t.p_value is not None]
    for t, q in zip(with_p, benjamini_hochberg([t.p_value for t in with_p]), strict=True):
        t.p_adjusted = float(q)
    for t in result.tested:
        if t.supported and t.p_adjusted is not None and t.p_adjusted < ALPHA:
            primary = run_analysis(t.spec, run_sql, alpha=ALPHA).stat
            t.agrees = bool(verify_analysis(t.spec, run_sql, primary, alpha=ALPHA).agrees)
    for pl in bench.get("planted") or []:
        hits = [t for t in result.tested if "null_control" not in t.proposal and _matches(t.spec.model_dump(), pl["match"])]
        result.planted[pl["id"]] = any(t.found and ("expect_top" not in pl or str(t.top) == str(pl["expect_top"])) for t in hits)
    for n in bench.get("null_controls") or []:
        nulls = set(n.get("columns") or [])
        involved = [t for t in result.tested if t.proposal.get("null_control") == n["id"]
                    or (t.spec.segment is not None and t.spec.segment.column in nulls)]
        blamed = [t for t in result.tested if t.found and t.spec.method == "driver_model" and t.highlights.get("top_driver") in nulls]
        result.nulls_rejected[n["id"]] = bool(involved) and not any(t.found for t in involved) and not blamed
    return result
