"""Run an `AnalysisSpec` end to end: compile -> governed SQL via `run_sql` -> deterministic statistics.

`run_analysis` computes the primary statistic for the spec's method. `verify_analysis` re-derives
the finding with the method's *independent second method* (different estimator / resampling scheme
on the same governed data) and reports whether it agrees with the primary verdict. What each method
computes is documented in its module under `analystos.methods`; this module is the plumbing: every
query a method declares is compiled by `sqlbuild.compile_spec`, run through `run_sql` (the gateway
path for agents), and the rows are handed to the method. A method never opens a connection.

`agrees` is True when (primary supported) == (verification supported) and, if supported, the
verification points in the same direction (same top segment / sign).
"""
from __future__ import annotations

from typing import Any

from analystos import methods
from analystos.contracts.analysis import AnalysisSpec, StatResult
from analystos.methods.base import OTHER, TABLE_MAX_ROWS, AnalysisOutcome, py
from analystos.methods.driver_model import build_design
from analystos.skills.base import RunSQL
from analystos.skills.sqlbuild import compile_spec

__all__ = ["OTHER", "TABLE_MAX_ROWS", "AnalysisOutcome", "build_design", "run_analysis", "verify_analysis"]


class _Runner:
    def __init__(self, run_sql: RunSQL, spec: AnalysisSpec, sample_rows: int):
        self.run_sql = run_sql
        self.spec = spec
        self.sample_rows = sample_rows
        self.dialect = getattr(run_sql, "dialect", "duckdb")
        self.query_ids: list[str] = []
        self.sql: list[str] = []

    def fetch(self) -> dict[str, list[dict[str, Any]]]:
        """Every query the method declares, in its order, through the governed `run_sql`."""
        rows: dict[str, list[dict[str, Any]]] = {}
        for purpose in methods.get(self.spec.method).purposes:
            cq = compile_spec(self.spec, self.dialect, purpose=purpose, sample_rows=self.sample_rows)
            res = self.run_sql(cq.sql, purpose=f"analysis.{self.spec.method}.{purpose}", max_rows=cq.max_rows)
            self.query_ids.append(res.query_id)
            self.sql.append(cq.sql)
            rows[purpose] = [{k.lower(): py(v) for k, v in r.items()} for r in res.records()]
        return rows

    def attach(self, out: AnalysisOutcome) -> AnalysisOutcome:
        out.method = self.spec.method
        out.stat.method = self.spec.method  # a method stamps its own name on every result it returns
        out.query_ids, out.sql = list(self.query_ids), list(self.sql)
        return out


def run_analysis(spec: AnalysisSpec, run_sql: RunSQL, *, alpha: float = 0.05, sample_rows: int = 50000) -> AnalysisOutcome:
    """Compile `spec`, fetch aggregates/samples through `run_sql`, and compute the primary statistic."""
    method = methods.get(spec.method)
    runner = _Runner(run_sql, spec, sample_rows)
    return runner.attach(method.test(runner.fetch(), spec, alpha=alpha))


def verify_analysis(spec: AnalysisSpec, run_sql: RunSQL, primary: StatResult, *, alpha: float = 0.05,
                    sample_rows: int = 50000) -> AnalysisOutcome:
    """Re-derive the finding with an independent second method; `outcome.agrees` is the verdict."""
    method = methods.get(spec.method)
    runner = _Runner(run_sql, spec, sample_rows)
    return runner.attach(method.verify(runner.fetch(), spec, primary, alpha=alpha))
