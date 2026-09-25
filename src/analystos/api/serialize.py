from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.inspection import inspect


def row(obj: Any, exclude: set[str] | None = None) -> dict[str, Any]:
    if obj is None:
        return None  # type: ignore[return-value]
    out = {}
    for attr in inspect(obj).mapper.column_attrs:
        key = attr.key
        if exclude and key in exclude:
            continue
        value = getattr(obj, key)
        if key == "capabilities" and isinstance(value, dict) and "manifests" in value:
            value = {k: v for k, v in value.items() if k != "manifests"}  # a run's bound manifests: refs are enough here
        out[key] = value.isoformat() if isinstance(value, datetime) else value
    return out


def rows(objs, exclude: set[str] | None = None) -> list[dict[str, Any]]:
    return [row(o, exclude) for o in objs]
