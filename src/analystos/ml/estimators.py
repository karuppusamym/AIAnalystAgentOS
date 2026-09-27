"""The allowlisted estimators, their bounded search grids and the preprocessing pipeline (ADR-0024).

Only scikit-learn (and statsmodels for forecasts) estimators named in `contracts.work.ESTIMATORS` can be
built; every size parameter is clamped by `HARD_LIMITS`, and every estimator runs single-threaded with
the spec's seed. Gradient boosting is the classic (not the histogram, OpenMP) implementation: on a shared
worker an OpenMP pool can stall a small fit for minutes. The search order interleaves estimator families (round robin over their grids) so a
small trial budget still tries each family; the order is fixed, so a re-run tries the same trials.
"""
from __future__ import annotations

from itertools import product
from typing import Any

from analystos.contracts.work import ESTIMATORS, MLSpec

HARD_LIMITS = {"n_estimators": 300, "max_depth": 12, "max_iter": 300, "n_clusters": 20}

GRIDS: dict[str, list[dict[str, Any]]] = {
    "logistic": [{"C": c} for c in (0.1, 1.0, 10.0, 0.01)],
    "linear": [{"alpha": a} for a in (1.0, 10.0, 0.1, 100.0)],
    "gradient_boosting": [{"learning_rate": lr, "max_depth": d, "n_estimators": n}
                          for lr, d, n in product((0.1, 0.05), (3, 5), (100, 200))],
    "random_forest": [{"n_estimators": n, "max_depth": d, "min_samples_leaf": leaf}
                      for n, d, leaf in product((100, 200), (8, 12), (1, 5))],
    "ets": [{"trend": None, "damped": False}, {"trend": "add", "damped": True}, {"trend": "add", "damped": False}],
    "arima": [{"order": o} for o in ((0, 1, 1), (1, 1, 0), (1, 0, 0), (1, 1, 1), (2, 1, 0))],
    "kmeans": [],  # filled from k_range
    "gmm": [],
    "seasonal_residual": [{"z": None}],
    "isolation_forest": [{"n_estimators": n, "max_samples": m} for n, m in product((100, 200), ("auto", 256))],
}


def clamp(params: dict[str, Any]) -> dict[str, Any]:
    return {k: (min(v, HARD_LIMITS[k]) if k in HARD_LIMITS and isinstance(v, int) else v) for k, v in params.items()}


def grid(spec: MLSpec, estimator: str) -> list[dict[str, Any]]:
    if estimator in ("kmeans", "gmm"):
        lo, hi = spec.k_range
        return [{"n_clusters": k} for k in range(lo, hi + 1)]
    return [clamp(p) for p in GRIDS.get(estimator, [{}])] or [{}]


def search_order(spec: MLSpec) -> list[tuple[str, dict[str, Any]]]:
    """Candidate trials (baseline excluded), interleaved across estimator families."""
    grids = [(e, grid(spec, e)) for e in spec.candidates if e in ESTIMATORS[spec.task]]
    out: list[tuple[str, dict[str, Any]]] = []
    i = 0
    while any(i < len(g) for _, g in grids):
        out += [(e, g[i]) for e, g in grids if i < len(g)]
        i += 1
    return out


# ------------------------------------------------------------------------------------ builders (sklearn)
def preprocessor(schema: list[dict[str, Any]], spec: MLSpec) -> Any:
    from sklearn.compose import ColumnTransformer
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    pp = spec.preprocessing
    numeric = [f["name"] for f in schema if f["family"] != "categorical"]
    categorical = [f["name"] for f in schema if f["family"] == "categorical"]
    impute = {"median": SimpleImputer(strategy="median"), "mean": SimpleImputer(strategy="mean"),
              "zero": SimpleImputer(strategy="constant", fill_value=0.0)}[pp.numeric_impute]
    num_steps: list[tuple[str, Any]] = [("impute", impute)]
    if pp.scale:
        num_steps.append(("scale", StandardScaler()))
    cat_impute = SimpleImputer(strategy="most_frequent") if pp.categorical_impute == "most_frequent" else \
        SimpleImputer(strategy="constant", fill_value="__missing__")
    parts = []
    if numeric:
        parts.append(("num", Pipeline(num_steps), numeric))
    if categorical:
        parts.append(("cat", Pipeline([("impute", cat_impute),
                                       ("onehot", OneHotEncoder(handle_unknown="infrequent_if_exist",
                                                                max_categories=pp.max_categories,
                                                                sparse_output=False))]), categorical))
    return ColumnTransformer(parts, remainder="drop", sparse_threshold=0.0)


def estimator(task: str, name: str, params: dict[str, Any], seed: int) -> Any:
    from sklearn import dummy, ensemble, linear_model

    p = clamp(params)
    if task == "classify":
        if name == "dummy_prior":
            return dummy.DummyClassifier(strategy="prior")
        if name == "logistic":
            return linear_model.LogisticRegression(C=p.get("C", 1.0), max_iter=1000)
        if name == "gradient_boosting":
            return ensemble.GradientBoostingClassifier(learning_rate=p["learning_rate"], max_depth=p["max_depth"],
                                                       n_estimators=p["n_estimators"], random_state=seed)
        if name == "random_forest":
            return ensemble.RandomForestClassifier(n_estimators=p["n_estimators"], max_depth=p["max_depth"],
                                                   min_samples_leaf=p["min_samples_leaf"], n_jobs=1, random_state=seed)
    if task == "regress":
        if name == "dummy_mean":
            return dummy.DummyRegressor(strategy="mean")
        if name == "linear":
            return linear_model.Ridge(alpha=p.get("alpha", 1.0))
        if name == "gradient_boosting":
            return ensemble.GradientBoostingRegressor(learning_rate=p["learning_rate"], max_depth=p["max_depth"],
                                                      n_estimators=p["n_estimators"], random_state=seed)
        if name == "random_forest":
            return ensemble.RandomForestRegressor(n_estimators=p["n_estimators"], max_depth=p["max_depth"],
                                                  min_samples_leaf=p["min_samples_leaf"], n_jobs=1, random_state=seed)
    raise ValueError(f"{name} is not an allowlisted {task} estimator")


def pipeline(task: str, name: str, params: dict[str, Any], schema: list[dict[str, Any]], spec: MLSpec, seed: int) -> Any:
    from sklearn.pipeline import Pipeline

    return Pipeline([("prep", preprocessor(schema, spec)), ("model", estimator(task, name, params, seed))])
