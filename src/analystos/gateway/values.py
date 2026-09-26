"""Driver values -> JSON-safe values, and secret-free one-line error text (shared by gateway and engines)."""
from __future__ import annotations

import math
import uuid
from datetime import date, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal
from typing import Any


def json_safe(value: Any) -> Any:
    """Convert a driver value into something JSON (and JSONB) can store losslessly enough."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        return float(value)
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    return str(value)


def first_line(exc: BaseException | None) -> str:
    text_ = str(exc or "").strip()
    return text_.splitlines()[0][:500] if text_ else (exc.__class__.__name__ if exc else "error")
