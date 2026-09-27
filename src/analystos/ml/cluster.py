"""`ml.cluster`: k-means / Gaussian mixtures against a random-partition baseline, with a stability check.

Selection: mean silhouette of the validation rows assigned by a model fitted on the other folds. The
holdout silhouette of the frozen choice is compared, on identical rows, with a random partition into the
same number of clusters; a clustering is only reported as structure when it beats that baseline *and*
is stable (adjusted Rand index across seeds and bootstrap refits). Clusters describe similarity in the
chosen features; they carry no causal meaning.
"""
from __future__ import annotations

import hashlib
import warnings
from typing import Any

import numpy as np
import pandas as pd

from analystos.contracts.work import MLSpec
from analystos.ml import estimators as E
from analystos.ml import evaluation as V
from analystos.ml.data import model_matrix
from analystos.ml.splits import Split
from analystos.ml.tabular import reference_profile

SILHOUETTE_ROWS = 3000
STABILITY_RUNS = 5
STABILITY_MIN = 0.6


class Cluster:
    def __init__(self, spec: MLSpec, df: pd.DataFrame, fams: dict[str, str], split: Split, seed: int) -> None:
        self.spec, self.split, self.seed = spec, split, seed
        self.task, self.metric = "cluster", "silhouette"
        self.df = df.reset_index(drop=True)
        self.schema = [{"name": f.column, "family": f.type or fams.get(f.column, "categorical")} for f in spec.features]
        self.X, _ = model_matrix(self.df, self.schema)
        self._k_for_baseline = spec.k_range[0]

    def _fit(self, name: str, params: dict[str, Any], rows: np.ndarray, seed: int | None = None) -> tuple[Any, Any]:
        from sklearn.cluster import KMeans
        from sklearn.mixture import GaussianMixture

        prep = E.preprocessor(self.schema, self.spec)
        Z = prep.fit_transform(self.X.iloc[rows])
        k = min(int(params["n_clusters"]), E.HARD_LIMITS["n_clusters"], max(2, len(rows) - 1))
        s = self.seed if seed is None else seed
        model = KMeans(n_clusters=k, n_init=10, random_state=s) if name == "kmeans" else \
            GaussianMixture(n_components=k, covariance_type="diag", random_state=s, reg_covar=1e-4)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model.fit(Z)
        return prep, model

    def _assign(self, fitted: tuple[Any, Any], rows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        prep, model = fitted
        Z = prep.transform(self.X.iloc[rows])
        return Z, np.asarray(model.predict(Z))

    def _random(self, n: int, k: int, salt: int) -> np.ndarray:
        return np.random.default_rng(self.seed + salt).integers(0, k, n)

    def _silhouette(self, Z: np.ndarray, labels: np.ndarray, idx: np.ndarray | None = None) -> float:
        from sklearn.metrics import silhouette_score

        if idx is not None:
            Z, labels = Z[idx], labels[idx]
        if len(np.unique(labels)) < 2 or len(labels) < 3:
            return float("nan")
        if len(labels) > SILHOUETTE_ROWS:
            pick = np.random.default_rng(self.seed).choice(len(labels), SILHOUETTE_ROWS, replace=False)
            Z, labels = Z[pick], labels[pick]
            if len(np.unique(labels)) < 2:
                return float("nan")
        return float(silhouette_score(Z, labels))

    def cv(self, name: str, params: dict[str, Any], **_: Any) -> dict[str, Any]:
        scores = []
        for i, (tr, va) in enumerate(self.split.folds):
            if name == "random_partition":
                fitted = self._fit("kmeans", {"n_clusters": self._k_for_baseline}, tr)
                Z, _ = self._assign(fitted, va)
                labels = self._random(len(va), self._k_for_baseline, i)
            else:
                Z, labels = self._assign(self._fit(name, params, tr), va)
            scores.append(V.finite(self._silhouette(Z, labels)))
        valid = [s for s in scores if s is not None]
        return {"folds": scores, "mean": float(np.mean(valid)) if valid else None}

    def stability(self, best: dict[str, Any]) -> dict[str, Any]:
        from sklearn.metrics import adjusted_rand_score

        tr = self.split.train
        ref = self._assign(self._fit(best["estimator"], best["params"], tr), tr)[1]
        seeds = [adjusted_rand_score(ref, self._assign(self._fit(best["estimator"], best["params"], tr, self.seed + s + 1),
                                                       tr)[1]) for s in range(STABILITY_RUNS)]
        rng = np.random.default_rng(self.seed)
        boots = []
        for _ in range(STABILITY_RUNS):
            sample = np.sort(rng.choice(tr, len(tr), replace=True))
            boots.append(adjusted_rand_score(ref, self._assign(self._fit(best["estimator"], best["params"], sample), tr)[1]))
        score = float(min(np.mean(seeds), np.mean(boots)))
        return {"ari_across_seeds": round(float(np.mean(seeds)), 4), "ari_across_bootstraps": round(float(np.mean(boots)), 4),
                "score": round(score, 4), "minimum": STABILITY_MIN, "stable": score >= STABILITY_MIN}

    def evaluate(self, baseline: dict[str, Any], best: dict[str, Any]) -> dict[str, Any]:
        from sklearn.metrics import silhouette_samples

        ho = self.split.holdout
        fitted = self._fit(best["estimator"], best["params"], self.split.train)
        self._fitted = fitted
        Z, labels = self._assign(fitted, ho)
        k = int(best["params"]["n_clusters"])
        base_labels = self._random(len(ho), k, 999)
        report: dict[str, Any] = {"holdout_rows": int(len(ho)), "metric": "silhouette", "higher_is_better": True,
                                  "baseline": {"silhouette": round(V.finite(self._silhouette(Z, base_labels)) or 0.0, 6),
                                               "clusters": k},
                                  "candidate": {"silhouette": round(V.finite(self._silhouette(Z, labels)) or 0.0, 6),
                                                "clusters": k,
                                                "sizes": {str(c): int((labels == c).sum()) for c in sorted(set(labels.tolist()))}}}
        cap = np.arange(len(ho)) if len(ho) <= SILHOUETTE_ROWS else \
            np.sort(np.random.default_rng(self.seed).choice(len(ho), SILHOUETTE_ROWS, replace=False))
        s_c = silhouette_samples(Z[cap], labels[cap]) if len(set(labels[cap].tolist())) > 1 else np.zeros(len(cap))
        s_b = silhouette_samples(Z[cap], base_labels[cap]) if len(set(base_labels[cap].tolist())) > 1 else np.zeros(len(cap))

        def pair(idx: np.ndarray) -> tuple[float, float]:
            return float(np.mean(s_c[idx])), float(np.mean(s_b[idx]))
        boot = V.paired_bootstrap("silhouette", pair, len(cap), seed=self.seed)
        report["uncertainty"] = boot
        stability = self.stability(best)
        report["stability"] = stability
        decision = V.decide("silhouette", report["candidate"]["silhouette"], report["baseline"]["silhouette"], boot,
                            self.spec.min_improvement)
        if decision["improved"] and not stability["stable"]:
            decision = {**decision, "improved": False,
                        "reason": f"no improvement: clusters are unstable (ARI {stability['score']} < {STABILITY_MIN})"}
        report["decision"] = decision
        report["guardrails"], report["slices"] = [], []
        report["limits"] = "clusters describe similarity in the chosen features; they carry no causal meaning"
        report["predictions_hash"] = hashlib.sha256(labels.astype(np.int64).tobytes()).hexdigest()
        return report

    def reproduce(self, best: dict[str, Any], expected_hash: str) -> dict[str, Any]:
        _, labels = self._assign(self._fit(best["estimator"], best["params"], self.split.train), self.split.holdout)
        got = hashlib.sha256(labels.astype(np.int64).tobytes()).hexdigest()
        return {"check": "reproducible_from_manifest", "outcome": "pass" if got == expected_hash else "fail",
                "reason": "a refit with the seed assigns the holdout identically" if got == expected_hash
                else "a refit assigned the holdout differently"}

    def package(self, best: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
        prep, model = self._fitted
        return {"task": "cluster", "estimator": best["estimator"], "params": best["params"], "schema": self.schema,
                "prep": prep, "model": model, "metric": "silhouette",
                "reference_profile": reference_profile(self.X.iloc[self.split.train], self.schema)}


def score_rows(package: dict[str, Any], df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    X, reasons = model_matrix(df, package["schema"])
    ok = (reasons == "").to_numpy()
    out = pd.DataFrame(index=df.index, data={"prediction_label": None, "prediction_value": np.nan})
    if ok.any():
        labels = np.asarray(package["model"].predict(package["prep"].transform(X[ok])))
        out.loc[ok, "prediction_label"] = [f"cluster_{int(c)}" for c in labels]
        out.loc[ok, "prediction_value"] = labels.astype(float)
    return out, reasons
