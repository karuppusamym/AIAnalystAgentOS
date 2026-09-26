"""`skill.ml_propose_spec` (P5-06): a proposed `MLSpec` for one in-scope table, from the catalog only.

This is what the `ml_practitioner` agent calls. A model may choose the inputs (target, task, features,
estimators); without one (`off` mode) the rules below choose them from the objective's words and the
catalog. Either way the result is validated here (columns exist and are visible, the allowlist, the
`MLSpec` contract) and is only a *proposal*: nothing trains until a person publishes it as an `ml_spec`
definition, and training re-runs every readiness and leakage check on the data.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from analystos.contracts.work import ESTIMATORS, MLSpec

_TARGET_WORDS = re.compile(r"(churn|label|target|outcome|converted|defaulted|fraud|breach|is_|has_|flag)", re.I)
_FORECAST_WORDS = ("forecast", "predict next", "next week", "next month", "projection", "demand")


def _tokens(text: str | None) -> set[str]:
    return {w.strip(",.?!:;()").lower() for w in (text or "").replace("_", " ").split() if len(w) > 3}


def _family(col: dict[str, Any]) -> str:
    from analystos.ml.data import catalog_family

    sem = col.get("semantic_type")
    if sem in ("numeric", "boolean", "datetime", "categorical"):
        return sem
    return catalog_family(col.get("data_type")) or "categorical"


def propose(columns: list[dict[str, Any]], *, asset: str, row_count: int | None = None, objective: str | None = None,
            target: str | None = None, task: str | None = None, features: list[str] | None = None,
            estimators: list[str] | None = None, time_column: str | None = None, horizon: int | None = None) -> dict[str, Any]:
    """`columns`: visible catalog columns `{name, data_type, semantic_type, is_key, distinct}`. Returns the
    proposal (an `MLSpec` as JSON, or None) with the problems that stopped or trimmed it."""
    problems: list[str] = []
    by_name = {c["name"]: c for c in columns}
    words = _tokens(objective)
    for name, value in (("target", target), ("time_column", time_column)):
        if value is not None and value not in by_name:
            problems.append(f"{name} {value} is not a visible column of {asset}")
    if problems:
        return {"proposal": None, "problems": problems, "source": "inputs"}
    source = "inputs" if any(v is not None for v in (target, task, features, estimators)) else "rules"
    keys = [c["name"] for c in columns if c.get("is_key")]
    if target is None and task not in ("cluster",):
        scored = sorted(((len(_tokens(c["name"]) & words) * 2 + bool(_TARGET_WORDS.search(c["name"])) +
                          (_family(c) == "boolean"), c["name"]) for c in columns if c["name"] not in keys),
                        key=lambda t: (-t[0], t[1]))
        target = scored[0][1] if scored and scored[0][0] > 0 else None
    fam = _family(by_name[target]) if target else None
    if time_column is None and any(w in (objective or "").lower() for w in _FORECAST_WORDS):
        time_column = next((c["name"] for c in columns if _family(c) == "datetime"), None)
    if task is None:
        if target is None:
            task = "cluster"
        elif time_column and fam == "numeric" and any(w in (objective or "").lower() for w in _FORECAST_WORDS):
            task = "forecast"
        elif fam in ("boolean", "categorical"):
            task = "classify"
        else:
            task = "regress"
    if features is None:
        features = [c["name"] for c in columns
                    if c["name"] not in {target, time_column, *keys} and _family(c) in ("numeric", "boolean", "categorical")
                    and not (_family(c) == "categorical" and row_count and (c.get("distinct") or 0) > 0.5 * row_count)]
        if task == "anomaly":
            features = [f for f in features if _family(by_name[f]) == "numeric"]
    else:
        unknown = [f for f in features if f not in by_name]
        if unknown:
            problems.append(f"features not visible in {asset}: {', '.join(unknown)} (dropped)")
        features = [f for f in features if f in by_name and f != target]
    allowed = set(ESTIMATORS.get(task, ()))
    if estimators is not None:
        bad = [e for e in estimators if e not in allowed]
        if bad:
            problems.append(f"estimators {bad} are not allowlisted for {task} (dropped)")
        estimators = [e for e in estimators if e in allowed]
    body: dict[str, Any] = {"task": task, "dataset": {"asset": asset}, "target": target if task != "cluster" else None,
                            "features": [{"column": f} for f in features if task != "forecast"],
                            "entity_keys": keys[:1], "estimators": estimators or [], "time_column": time_column,
                            "horizon": horizon or (4 if task == "forecast" else None)}
    if task not in ("forecast",) and not time_column:
        distinct = (by_name.get(keys[0], {}).get("distinct") if keys else None)
        if keys and row_count and distinct == row_count:
            body["split"] = {"strategy": "random", "independence_justification":
                             f"one row per {keys[0]} (catalog key, {row_count} distinct rows)"}
        elif keys:
            body["group_keys"] = keys[:1]
            body["split"] = {"strategy": "group"}
        elif task in ("classify", "regress"):
            problems.append("no time column and no entity key: declare the split strategy and why rows are independent")
    try:
        spec = MLSpec.model_validate(body)
    except ValidationError as exc:
        problems += [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5]]
        return {"proposal": None, "problems": problems, "source": source}
    problems += spec.executable_problems()
    return {"proposal": spec.model_dump(mode="json", exclude_none=True), "problems": problems, "source": source,
            "note": "a proposal only: publish it as an ml_spec definition to train; readiness and leakage checks run on the data"}


def propose_spec(ctx: Any, asset: str, objective: str | None = None, target: str | None = None, task: str | None = None,
                 features: list[str] | None = None, estimators: list[str] | None = None, time_column: str | None = None,
                 horizon: int | None = None) -> dict[str, Any]:
    from analystos.agents.common import asset_rows
    from analystos.core.errors import PolicyDenied

    if asset not in ctx.scope.assets:
        raise PolicyDenied(f"{asset} is outside the authorized scope of this run")
    denied = set(ctx.scope.denied_columns)
    for a, cols in asset_rows(ctx):
        if f"{a.schema_name}.{a.name}" != asset:
            continue
        visible = [{"name": c.name, "data_type": c.data_type, "semantic_type": c.semantic_type, "is_key": bool(c.is_key),
                    "distinct": (c.profile or {}).get("distinct")} for c in cols
                   if f"{asset}.{c.name}" not in denied and not ("pii" in (c.tags or []) and
                                                              ctx.agent.policies.pii_access == "none")]
        return propose(visible, asset=asset, row_count=a.row_count, objective=objective or ctx.run.objective, target=target,
                       task=task, features=features, estimators=estimators, time_column=time_column, horizon=horizon)
    return {"proposal": None, "problems": [f"{asset} has no catalog entry"], "source": "rules"}
