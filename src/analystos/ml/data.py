"""From a snapshot (`{columns, rows}`, rows canonically sorted) to typed frames.

A snapshot holds JSON-safe values (numbers, strings, booleans, ISO dates). Column *families* (numeric,
categorical, boolean, datetime) come from the catalog type recorded with the dataset, else from the
declared feature type, else from the values. Row identity is the hash of the row's content plus its
occurrence number, so the same data always yields the same keys (the split manifest's membership).
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np
import pandas as pd

FAMILIES = ("numeric", "categorical", "boolean", "datetime")
_CATALOG_FAMILY = {"numeric": "numeric", "boolean": "boolean", "date": "datetime", "timestamp": "datetime",
                   "text": "categorical"}


def catalog_family(data_type: str | None) -> str | None:
    """The family of a catalog column type (`integer`, `numeric(10,2)`, `timestamp with time zone`...)."""
    if not data_type:
        return None
    t = str(data_type).lower()
    if any(k in t for k in ("timestamp", "datetime")) or t == "date":
        return "datetime"
    if t.startswith(("bool",)):
        return "boolean"
    if any(k in t for k in ("int", "numeric", "decimal", "double", "float", "real", "number", "money")):
        return "numeric"
    if any(k in t for k in ("char", "text", "string", "varchar", "uuid", "enum")):
        return "categorical"
    return None


def _value_family(values: Iterable[Any]) -> str:
    seen = [v for v in values if v is not None]
    if not seen:
        return "categorical"
    if all(isinstance(v, bool) for v in seen):
        return "boolean"
    if all(isinstance(v, int | float) and not isinstance(v, bool) for v in seen):
        return "numeric"
    return "categorical"


def families(columns: list[str], rows: list[list[Any]], catalog_types: Mapping[str, str | None],
             declared: Mapping[str, str | None] | None = None) -> dict[str, str]:
    """Family per column: declared (feature spec) > catalog type > observed values."""
    declared = declared or {}
    out: dict[str, str] = {}
    for i, c in enumerate(columns):
        fam = declared.get(c) or catalog_family(catalog_types.get(c))
        out[c] = fam or _value_family(r[i] for r in rows[:5000])
    return out


def frame(columns: list[str], rows: list[list[Any]]) -> pd.DataFrame:
    return pd.DataFrame.from_records([list(r) for r in rows], columns=list(columns)) if rows else \
        pd.DataFrame({c: pd.Series(dtype=object) for c in columns})


def row_keys(columns: list[str], rows: list[list[Any]]) -> list[str]:
    """Content hash of each row (+ its occurrence number among identical rows): stable across re-reads."""
    seen: dict[str, int] = {}
    out = []
    for r in rows:
        h = hashlib.sha256(json.dumps(list(r), sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()[:24]
        n = seen.get(h, 0)
        seen[h] = n + 1
        out.append(f"{h}#{n}")
    return out


def to_datetime(s: pd.Series) -> pd.Series:
    """UTC timestamps; naive values are read as UTC; unparseable values become NaT."""
    return pd.to_datetime(s, errors="coerce", utc=True, format="mixed")


def to_number(s: pd.Series) -> tuple[pd.Series, pd.Series]:
    """(float values, mask of non-null inputs that are not numbers)."""
    def conv(v: Any) -> float:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return np.nan
        if isinstance(v, bool):
            return float(v)
        if isinstance(v, int | float):
            return float(v)
        try:
            return float(str(v))
        except ValueError:
            return math.inf  # marker: present but not a number
    out = s.map(conv).astype(float)
    bad = np.isinf(out)
    return out.where(~bad, np.nan), bad


def to_bool(s: pd.Series) -> tuple[pd.Series, pd.Series]:
    truthy, falsy = {"true", "t", "1", "yes", "y"}, {"false", "f", "0", "no", "n"}

    def conv(v: Any) -> float:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return np.nan
        if isinstance(v, bool | int | float):
            return float(bool(v))
        text = str(v).strip().lower()
        if text in truthy:
            return 1.0
        if text in falsy:
            return 0.0
        return math.inf
    out = s.map(conv).astype(float)
    bad = np.isinf(out)
    return out.where(~bad, np.nan), bad


def model_matrix(df: pd.DataFrame, schema: list[dict[str, Any]]) -> tuple[pd.DataFrame, pd.Series]:
    """Feature columns in the package's order and families -> (model input frame, rejected-row reasons).
    Numeric, boolean and datetime (days since epoch) become floats; categoricals strings (None stays
    missing: imputed exactly as in training). A present value of the wrong family rejects the row."""
    out: dict[str, pd.Series] = {}
    reasons = pd.Series([""] * len(df), index=df.index, dtype=object)
    for f in schema:
        name, fam = f["name"], f["family"]
        col = df[name] if name in df.columns else pd.Series([None] * len(df), index=df.index, dtype=object)
        if fam == "numeric":
            values, bad = to_number(col)
        elif fam == "boolean":
            values, bad = to_bool(col)
        elif fam == "datetime":
            ts = to_datetime(col)
            bad = ts.isna() & col.notna()
            values = (ts - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds() / 86400.0
        else:
            values = col.map(lambda v: None if v is None or (isinstance(v, float) and math.isnan(v)) else str(v)).astype(object)
            bad = pd.Series(False, index=df.index)
        if bool(bad.any()):
            reasons = reasons.where(~bad, reasons + f"{name}: not a {fam} value; ")
        out[name] = values
    return pd.DataFrame(out, index=df.index), reasons.str.strip()
