"""Data-quality gates and schema policy on recipe outputs (ADR-0023 decision 4, P6-05).

Row gates are the rule types of DataPilot's `quality.py` (not_null, unique, accepted_values,
range; AienginnerAgentOs@15235dc:apps/api/app/quality.py), rewritten: DataPilot ran them on a raw
SQLAlchemy engine and wrote quarantine with that same connection (breaking CLAUDE.md rule 4). Here
the compiler turns each gate into a 0/1 flag column of the output statement itself, so the flags are
computed by the engine that computed the rows, in the same read through `QueryGateway.execute` (or
the same DuckDB statement over the snapshot). Quarantine is written by the staging loader and read
back through the gateway (`services/recipes.py`).

Severity: `fail` blocks the output (the candidate is quarantined whole and the last good output
stays), `warn` is evidence only, `drop` moves the failing rows to quarantine, counted and shown, and
the rest is published. Table gates (`row_count_delta`) compare with the last good output and can
only fail or warn. `unique` is judged over the candidate before any row is dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from analystos.contracts.recipe import ROW_GATES, Column, Gate, OutputNode
from analystos.recipes.compiler import FLAG_PREFIX

SAMPLE_ROWS = 5


def row_gates(output: OutputNode) -> list[Gate]:
    return [g for g in output.all_gates() if g.type in ROW_GATES]


@dataclass
class GateOutcome:
    columns: list[str]
    kept: list[list[Any]]
    dropped: list[tuple[list[Any], list[str]]]  # (row, gate keys that dropped it)
    results: list[dict[str, Any]]
    blocked: bool
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {"blocked": self.blocked, "kept_rows": len(self.kept), "dropped_rows": len(self.dropped),
                "gates": self.results, "warnings": self.warnings}


def evaluate(output: OutputNode, columns: list[str], rows: list[list[Any]], *,
             previous_rows: int | None = None) -> GateOutcome:
    """Split a candidate (the output statement's rows, gate flag columns last) by its gates."""
    gates = row_gates(output)
    width = len(columns) - len(gates)
    data_cols = columns[:width]
    if [c for c in columns[width:]] != [f"{FLAG_PREFIX}{i}" for i in range(len(gates))]:
        raise ValueError("the candidate does not carry the expected gate flag columns")
    drop_idx = [i for i, g in enumerate(gates) if g.severity == "drop"]
    kept: list[list[Any]] = []
    dropped: list[tuple[list[Any], list[str]]] = []
    for row in rows:
        flags = row[width:]
        hit = [gates[i].key() for i in drop_idx if int(flags[i] or 0)]
        (dropped.append((list(row[:width]), hit)) if hit else kept.append(list(row)))
    results: list[dict[str, Any]] = []
    blocked = False
    warnings: list[str] = []
    for i, g in enumerate(gates):
        judged = rows if g.severity == "drop" else kept
        failing = [r for r in judged if int(r[width + i] or 0)]
        status = "passed"
        if failing:
            status = {"fail": "failed", "warn": "warned", "drop": "dropped"}[g.severity]
        if status == "failed":
            blocked = True
        if status == "warned":
            warnings.append(f"{g.key()}: {len(failing)} row(s) fail")
        results.append({"gate": g.key(), "type": g.type, "severity": g.severity, "columns": g.targets(),
                        "checked_rows": len(judged), "failed_rows": len(failing), "status": status,
                        "sample": [dict(zip(data_cols, r[:width], strict=True)) for r in failing[:SAMPLE_ROWS]]})
    kept_rows = [r[:width] for r in kept]
    for g in output.all_gates():
        if g.type != "row_count_delta":
            continue
        entry: dict[str, Any] = {"gate": g.key(), "type": g.type, "severity": g.severity, "columns": [],
                                 "checked_rows": len(kept_rows), "failed_rows": 0, "previous_rows": previous_rows}
        if previous_rows is None:
            entry.update(status="passed", note="no previous good output to compare with")
        else:
            change = (abs(len(kept_rows) - previous_rows) / previous_rows * 100) if previous_rows else (
                0.0 if not kept_rows else 100.0)
            entry["change_pct"] = round(change, 4)
            if change > float(g.max_change_pct or 0):
                entry["status"] = "failed" if g.severity == "fail" else "warned"
                blocked = blocked or g.severity == "fail"
                if g.severity == "warn":
                    warnings.append(f"{g.key()}: row count changed {change:.1f}% (limit {g.max_change_pct}%)")
            else:
                entry["status"] = "passed"
        results.append(entry)
    return GateOutcome(columns=data_cols, kept=kept_rows, dropped=dropped, results=results, blocked=blocked,
                       warnings=warnings)


def schema_policy(policy: str, declared: list[Column], previous: list[dict[str, str]] | None,
                  upstream: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """What a schema change does to an output. `previous` is the last good output's columns,
    `upstream` the drift between a source's declared columns and its catalog (`type_changed` /
    `column_added`). `evolve` accepts, `warn` accepts with evidence, `strict` blocks."""
    changes: list[dict[str, Any]] = []
    if previous is not None:
        before = {c["name"]: c["type"] for c in previous}
        now = {c.name: c.type for c in declared}
        changes += [{"change": "column_added", "column": n, "type": t} for n, t in now.items() if n not in before]
        changes += [{"change": "column_removed", "column": n, "type": t} for n, t in before.items() if n not in now]
        changes += [{"change": "type_changed", "column": n, "from": before[n], "to": t}
                    for n, t in now.items() if n in before and before[n] != t]
        if not changes and [c["name"] for c in previous] != [c.name for c in declared]:
            changes.append({"change": "columns_reordered"})
    changes += [dict(u, upstream=True) for u in upstream or []]
    action = {"evolve": "accept", "warn": "warn", "strict": "block"}[policy] if changes else "none"
    return {"policy": policy, "changes": changes, "action": action}
