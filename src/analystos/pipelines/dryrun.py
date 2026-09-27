"""A pipeline dry run (P6-01, workspace spec §5): everything a reviewer needs before any output is written.

* **Per-source scope and snapshot manifest.** For each source the recipe reads: the assets and columns the
  requester is authorized for (the scope the gateway enforces), the identity it is read with (the
  workspace reader role through `QueryGateway.execute`, never a source login of its own), the staged
  content fingerprint of every input and, when the recipe spans sources, the immutable snapshot each input
  was read into. A cross-source recipe is never one federated statement: each side is read through its own
  source's gateway scope into a snapshot, and the validated join runs in the recipe engine over those
  snapshots (ADR-0011: "per-source manifests and a governed staging plan"; the single-source gateway is
  unchanged).
* **Checks.** Input version pins and freshness, the join pre-flight (declared vs observed cardinality),
  fan-out (row multiplication) and unmatched rows against the pipeline's limits, the output's gates, and
  the budgets.
* **Reconciled virtual output.** Input and output counts, unmatched keys per join, rejected rows (dropped by
  a gate, or the whole candidate when a fail gate blocks it) and aggregate reconciliation within tolerance.
  The output rows themselves are the candidate: stored as a content-addressed snapshot, so an approval can
  bind to exactly these rows and the managed writer can materialize exactly them.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from sqlglot import exp

from analystos.contracts.recipe import JoinNode, SourceNode, ValidatedRecipe
from analystos.contracts.work import PipelineSpec
from analystos.core.errors import BudgetExceeded
from analystos.recipes.compiler import cte_name


class Budget:
    """The pipeline's query and wall-clock budget, checked before every governed read."""

    def __init__(self, pipeline: PipelineSpec) -> None:
        self.max_queries = pipeline.budgets.max_queries
        self.max_seconds = pipeline.budgets.max_wall_seconds
        self.started = time.monotonic()
        self.queries = 0

    def wrap(self, executor: Any) -> None:
        inner = executor.query

        def guarded(tree: Any, **kw: Any) -> Any:
            self.queries += 1
            if self.max_queries is not None and self.queries > self.max_queries:
                raise BudgetExceeded(f"the pipeline's query budget ({self.max_queries}) is used up")
            if self.max_seconds is not None and time.monotonic() - self.started > self.max_seconds:
                raise BudgetExceeded(f"the pipeline's wall-clock budget ({self.max_seconds}s) is used up")
            return inner(tree, **kw)
        executor.query = guarded

    def record(self) -> dict[str, Any]:
        return {"queries": self.queries, "max_queries": self.max_queries, "seconds": round(time.monotonic() - self.started, 3),
                "max_wall_seconds": self.max_seconds}


def _num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    try:
        return float(str(v))
    except ValueError:
        return None


def _scalar(executor: Any, v: ValidatedRecipe, node_id: str, expr: exp.Expression, purpose: str) -> Any:
    body = exp.select(exp.alias_(expr, "value")).from_(exp.Table(this=exp.to_identifier(cte_name(node_id), quoted=True)))
    res = executor.query(executor.compiler._with(v.ancestors(node_id), body), purpose=purpose, max_rows=1)
    return res.rows[0][0] if res.rows else None


def execute(pipeline: PipelineSpec, v: ValidatedRecipe, executor: Any, *, inputs: dict[str, dict[str, Any]],
            previous_rows: int | None, run_id: str, now: Any) -> dict[str, Any]:
    """Run the dry run of the pipeline's output through `executor` (every read a governed gateway query or a
    snapshot statement). `inputs` is the catalog's view of each input asset (fingerprint, staged_at)."""
    from analystos.recipes.gates import evaluate, row_gates

    budget = Budget(pipeline)
    budget.wrap(executor)
    out = next(o for o in v.outputs() if o.name == pipeline.output.output)
    ancestry = v.ancestors(out.id)
    checks: list[dict[str, Any]] = []

    for i in pipeline.inputs:  # input versions and freshness
        seen = inputs.get(i.asset) or {}
        if i.fingerprint:
            ok = seen.get("content_fingerprint") == i.fingerprint
            checks.append({"check": "input_version", "asset": i.asset, "pinned": i.fingerprint,
                           "current": seen.get("content_fingerprint"), "ok": ok})
        if i.max_age_hours:
            staged = seen.get("staged_at")
            age = (now - staged).total_seconds() / 3600 if staged else None
            checks.append({"check": "input_freshness", "asset": i.asset, "age_hours": round(age, 3) if age is not None else None,
                           "max_age_hours": i.max_age_hours, "ok": age is not None and age <= i.max_age_hours})

    expectations = {j.node: j for j in pipeline.joins}
    preflight, unmatched = [], {}
    for node in v.recipe.nodes:
        if not isinstance(node, JoinNode) or node.id not in ancestry:
            continue
        p = executor.preflight(node.id)
        preflight.append(p)
        checks.append({"check": "join_cardinality", "join": node.id, "declared": p["declared"], "observed": p["observed"],
                       "ok": p["ok"]})
        left_rows = p.get("left_rows") or 0
        unmatched_pct = round(100.0 * p["unmatched_left_rows"] / left_rows, 4) if left_rows else 0.0
        unmatched[node.id] = {"unmatched_left_rows": p["unmatched_left_rows"], "unmatched_left_pct": unmatched_pct,
                              "unmatched_left_keys": max(p.get("left_keys", 0) - p.get("matched_keys", 0), 0),
                              "unmatched_right_keys": max(p.get("right_keys", 0) - p.get("matched_keys", 0), 0),
                              "row_multiplication": p.get("row_multiplication")}
        e = expectations.get(node.id)
        if e is not None and e.max_unmatched_pct is not None:
            checks.append({"check": "unmatched_rows", "join": node.id, "unmatched_left_pct": unmatched_pct,
                           "max_unmatched_pct": e.max_unmatched_pct, "ok": unmatched_pct <= e.max_unmatched_pct})
        mult = p.get("row_multiplication")
        limit = e.max_row_multiplication if e is not None and e.max_row_multiplication is not None else (
            1.0 if node.expected_cardinality in ("one_to_one", "many_to_one") else None)
        if limit is not None:
            checks.append({"check": "fanout", "join": node.id, "row_multiplication": mult, "max_row_multiplication": limit,
                           "ok": mult is None or mult <= limit + 1e-9})

    input_counts: dict[str, int] = {}
    for node in v.recipe.nodes:
        if isinstance(node, SourceNode) and node.id in ancestry:
            n = _scalar(executor, v, node.id, exp.Count(this=exp.Star()), f"pipeline.dry_run.count:{run_id}")
            input_counts[node.id] = int(n or 0)

    gates = row_gates(out)
    tree = executor.compiler.output_query(out.id, gates=gates)
    res = executor.query(tree, purpose=f"pipeline.dry_run:{run_id}")
    outcome = evaluate(out, res.columns, res.rows, previous_rows=previous_rows)
    for g in outcome.results:
        checks.append({"check": "gate", "gate": g["gate"], "severity": g["severity"], "status": g["status"],
                       "failed_rows": g["failed_rows"], "ok": g["status"] != "failed"})
    checks.append({"check": "complete_output", "truncated": res.truncated, "ok": not res.truncated})

    aggregates = []
    columns = outcome.columns
    for c in pipeline.checks:
        if c.func == "count":
            expr = exp.Count(this=exp.Column(this=exp.to_identifier(c.input_column, quoted=True))) if c.input_column \
                else exp.Count(this=exp.Star())
            out_value = float(len(outcome.kept) if not c.output_column else
                              sum(1 for r in outcome.kept if r[columns.index(c.output_column)] is not None))
        else:
            expr = exp.Sum(this=exp.Column(this=exp.to_identifier(c.input_column or "", quoted=True)))
            idx = columns.index(c.output_column or "")
            out_value = float(sum(_num(r[idx]) or 0.0 for r in outcome.kept))
        in_value = _num(_scalar(executor, v, c.input_node, expr, f"pipeline.reconcile:{run_id}")) or 0.0
        diff = out_value - in_value
        pct = abs(diff) / abs(in_value) * 100 if in_value else (0.0 if diff == 0 else 100.0)
        ok = pct <= c.tolerance_pct + 1e-9
        aggregates.append({"name": c.name, "func": c.func, "input": {"node": c.input_node, "column": c.input_column,
                                                                      "value": in_value},
                           "output": {"column": c.output_column, "value": out_value}, "difference": diff,
                           "difference_pct": round(pct, 6), "tolerance_pct": c.tolerance_pct, "ok": ok})
        checks.append({"check": "aggregate_reconciliation", "name": c.name, "difference_pct": round(pct, 6),
                       "tolerance_pct": c.tolerance_pct, "ok": ok})
    checks.append({"check": "budget", **budget.record(), "ok": True})

    rejected = len(outcome.dropped) + (len(outcome.kept) if outcome.blocked else 0)
    reconciliation = {"inputs": input_counts, "input_rows": sum(input_counts.values()), "output_rows": len(outcome.kept),
                      "rejected_rows": rejected, "dropped_rows": len(outcome.dropped), "blocked": outcome.blocked,
                      "unmatched": unmatched, "aggregates": aggregates}
    sql = {"output": executor.compiler.sql(tree, pretty=True), "dialect": executor.plan.dialect,
           "preflight": {p["join"]: executor.compiler.sql(executor.compiler.preflight_query(p["join"]), pretty=True)
                         for p in preflight}}
    return {"checks": checks, "preflight": preflight, "reconciliation": reconciliation, "sql": sql,
            "outcome": outcome, "ok": all(c["ok"] for c in checks), "budget": budget.record()}
