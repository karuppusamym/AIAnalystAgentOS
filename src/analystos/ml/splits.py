"""Immutable split manifests (P5-01, workspace spec §6).

The split is decided before anything is fitted and is a pure function of (data, spec, seed):

* ``chronological``: the holdout is the latest distinct time values (a forecast: the last `horizon`
  periods); `embargo_periods` distinct time values before it are dropped; validation folds are rolling
  origins (expanding window) inside the training part.
* ``group``: every group (``group_keys``, else ``entity_keys``) is assigned by a seeded hash, so no group
  crosses partitions; folds are group folds.
* ``group_chronological``: both: holdout = holdout groups after the time boundary, training = the other
  groups before it (minus the embargo); rows of either kind on the wrong side are excluded and counted.
* ``random``: a seeded row hash (the spec must justify independence).

The manifest records each partition's size and membership hash (sha256 of its sorted row keys); the
membership lists themselves are stored content-addressed next to it. The manifest hash is what an
experiment, its verdict and its package are bound to.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from analystos.contracts.work import MLSpec, SplitSpec
from analystos.core.ids import stable_hash
from analystos.ml.data import to_datetime

MANIFEST_VERSION = "ml_split.v1"
CLUSTER_JUSTIFICATION = "unsupervised: no label to leak; rows are exchangeable for fit and stability assessment"


@dataclass
class Split:
    manifest: dict[str, Any]
    train: np.ndarray  # positional indices into the prepared frame
    holdout: np.ndarray
    folds: list[tuple[np.ndarray, np.ndarray]] = field(default_factory=list)
    membership: dict[str, list[str]] = field(default_factory=dict)

    @property
    def hash(self) -> str:
        return manifest_hash(self.manifest)


def manifest_hash(manifest: dict[str, Any]) -> str:
    return stable_hash(manifest)


def membership_hash(keys: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(keys)).encode()).hexdigest()


def default_split(spec: MLSpec) -> SplitSpec:
    """The strategy a spec gets when it declares none (never a silent random split for supervised data)."""
    if spec.split is not None:
        return spec.split
    if spec.task == "forecast":
        return SplitSpec(strategy="chronological")
    if spec.group_keys:
        return SplitSpec(strategy="group_chronological" if spec.time_column else "group")
    if spec.time_column:
        return SplitSpec(strategy="chronological")
    if spec.task == "cluster" or (spec.task == "anomaly" and not spec.target):
        return SplitSpec(strategy="random", independence_justification=CLUSTER_JUSTIFICATION)
    raise ValueError("declare split.strategy: without time_column or group_keys the rows' independence is an "
                     "assumption only you can justify (random needs independence_justification)")


def _unit(seed: int, token: str, salt: str = "") -> float:
    h = hashlib.sha256(f"{seed}:{salt}:{token}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2 ** 64


def _group_tokens(df: pd.DataFrame, cols: list[str]) -> list[str]:
    return [json.dumps(list(v), default=str) for v in df[cols].itertuples(index=False, name=None)]


def _rolling_folds(order: np.ndarray, times: pd.Series, n_folds: int, block: int | None = None) -> list[tuple[np.ndarray, np.ndarray]]:
    """Expanding-window folds over distinct times of the rows in `order` (positions)."""
    distinct = np.array(sorted(times.iloc[order].unique()))
    if len(distinct) < n_folds + 1:
        n_folds = max(1, len(distinct) - 1)
    if block is None:
        block = max(1, len(distinct) // (n_folds + 1))
    folds = []
    for i in range(n_folds, 0, -1):
        val_start = len(distinct) - i * block
        if val_start <= 0:
            continue
        val_times = set(distinct[val_start:val_start + block])
        train_times = set(distinct[:val_start])
        t = times.iloc[order]
        tr = order[t.isin(train_times).to_numpy()]
        va = order[t.isin(val_times).to_numpy()]
        if len(tr) and len(va):
            folds.append((tr, va))
    return folds


def build(spec: MLSpec, df: pd.DataFrame, keys: list[str], *, dataset_version: str, seed: int) -> Split:
    """`df` holds only the usable rows, in snapshot order; `keys` their row keys (same order)."""
    s = default_split(spec)
    n = len(df)
    idx = np.arange(n)
    excluded: dict[str, int] = {}
    folds: list[tuple[np.ndarray, np.ndarray]] = []
    boundaries: dict[str, Any] = {}
    times = to_datetime(df[spec.time_column]).reset_index(drop=True) if spec.time_column and spec.time_column in df else None
    group_cols = spec.group_keys or spec.entity_keys

    if s.strategy in ("chronological", "group_chronological"):
        if times is None:
            raise ValueError(f"{s.strategy} split needs time_column")
        distinct = np.array(sorted(times.unique()))
        if spec.task == "forecast":
            n_hold = int(spec.horizon or 1)
        else:
            n_hold = max(1, int(round(len(distinct) * s.holdout_fraction)))
        n_hold = min(n_hold, len(distinct) - 2) if len(distinct) > 2 else 1
        boundary = distinct[-n_hold]
        embargo_start = distinct[max(0, len(distinct) - n_hold - s.embargo_periods)]
        after = (times >= boundary).to_numpy()
        before = (times < embargo_start).to_numpy()
        embargoed = ~after & ~before
        if int(embargoed.sum()):
            excluded["embargo"] = int(embargoed.sum())
        boundaries = {"holdout_from": pd.Timestamp(boundary).isoformat(), "embargo_from": pd.Timestamp(embargo_start).isoformat()
                      if s.embargo_periods else None, "holdout_periods": int(n_hold), "distinct_periods": int(len(distinct))}
        if s.strategy == "group_chronological":
            if not group_cols:
                raise ValueError("group_chronological needs group_keys")
            tokens = _group_tokens(df, group_cols)
            hold_group = np.array([_unit(seed, t, "holdout") < s.holdout_fraction for t in tokens])
            holdout = idx[after & hold_group]
            train = idx[before & ~hold_group]
            wrong_side = int((after & ~hold_group).sum() + (before & hold_group).sum())
            if wrong_side:
                excluded["group_time_conflict"] = wrong_side
        else:
            holdout, train = idx[after], idx[before]
        block = int(spec.horizon) if spec.task == "forecast" and spec.horizon else None
        folds = _rolling_folds(train, times, s.validation_folds, block)
    elif s.strategy == "group":
        if not group_cols:
            raise ValueError("a group split needs group_keys (or entity_keys)")
        tokens = _group_tokens(df, group_cols)
        u = np.array([_unit(seed, t, "holdout") for t in tokens])
        holdout, train = idx[u < s.holdout_fraction], idx[u >= s.holdout_fraction]
        fold_of = np.array([int(_unit(seed, t, "fold") * s.validation_folds) for t in tokens])
        folds = [(train[fold_of[train] != k], train[fold_of[train] == k]) for k in range(s.validation_folds)]
    else:  # random
        u = np.array([_unit(seed, k, "holdout") for k in keys])
        holdout, train = idx[u < s.holdout_fraction], idx[u >= s.holdout_fraction]
        fold_of = np.array([int(_unit(seed, k, "fold") * s.validation_folds) for k in keys])
        folds = [(train[fold_of[train] != k], train[fold_of[train] == k]) for k in range(s.validation_folds)]
    folds = [(a, b) for a, b in folds if len(a) and len(b)]
    k = np.asarray(keys)
    membership = {"train": sorted(k[train].tolist()), "holdout": sorted(k[holdout].tolist())}
    for i, (_, va) in enumerate(folds):
        membership[f"fold_{i}"] = sorted(k[va].tolist())
    groups = {}
    if group_cols and s.strategy in ("group", "group_chronological"):
        tokens = np.asarray(_group_tokens(df, group_cols))
        overlap = set(tokens[train]) & set(tokens[holdout])
        groups = {"columns": list(group_cols), "train_groups": int(len(set(tokens[train]))),
                  "holdout_groups": int(len(set(tokens[holdout]))), "overlap": len(overlap)}
    manifest = {
        "version": MANIFEST_VERSION, "dataset_version": dataset_version, "strategy": s.strategy, "seed": seed,
        "holdout_fraction": s.holdout_fraction if spec.task != "forecast" else None,
        "embargo_periods": s.embargo_periods, "independence_justification": s.independence_justification,
        "time_column": spec.time_column, "group_columns": list(group_cols) if s.strategy.startswith("group") else [],
        "rows": {"usable": n, "train": int(len(train)), "holdout": int(len(holdout)),
                 "folds": [{"train": int(len(a)), "validation": int(len(b))} for a, b in folds]},
        "membership": {name: membership_hash(v) for name, v in membership.items()},
        "boundaries": boundaries, "groups": groups, "excluded": excluded,
    }
    return Split(manifest=manifest, train=train, holdout=holdout, folds=folds, membership=membership)
