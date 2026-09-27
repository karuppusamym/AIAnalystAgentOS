"""Late rows (P6-02 reconciliation): rows that arrived behind the declared late window.

An incremental run reads `[committed watermark - late_window, high watermark]`. A row whose watermark is
older than that cutoff and that the committed output does not already hold (its key is absent, or held at
an older watermark) will never be read by a windowed run: it is *late*, and only a full reconcile picks it
up. The dry run computes the whole output, so it can count them exactly: candidate rows behind the cutoff
against the committed output's keys behind the cutoff (read through the gateway).

`late_rows` is a count only when it was measured; otherwise it is `None` with `late_rows_reason`
(no watermark declared, nothing committed yet, the output does not carry the key or watermark, the
committed output could not be read completely). A zero is never reported for something not measured.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from analystos.contracts.recipe import Incremental
from analystos.staging.incremental import as_text, shift, to_watermark

NO_WATERMARK = ("the pipeline declares no incremental watermark: every run reads its whole input, so late arrival "
                "is not defined")
Committed = Callable[[Any], tuple[list[Sequence[Any]], bool]]  # cutoff -> (rows of key..., watermark; complete)


class NotMeasured(Exception):  # noqa: N818 - a reason, not a failure
    """Raised by a `committed` reader that cannot read the committed output at all (the reason is reported)."""


def not_measured(reason: str, **detail: Any) -> dict[str, Any]:
    return {"late_rows": None, "late_rows_reason": reason, "late": detail}


def _wm(value: Any) -> Any:
    """A comparable watermark: timezone-aware timestamps in naive UTC, so the two sides always compare."""
    v = to_watermark(value)
    if isinstance(v, datetime) and v.tzinfo is not None:
        return v.astimezone(UTC).replace(tzinfo=None)
    return v


def measure(inc: Incremental | None, columns: Sequence[str], rows: Sequence[Sequence[Any]], *,
            stored_watermark: Any, committed: Committed) -> dict[str, Any]:
    """Count the candidate rows the next windowed run would never read (see the module docstring)."""
    if inc is None:
        return not_measured(NO_WATERMARK)
    detail: dict[str, Any] = {"watermark_column": inc.watermark, "key": list(inc.key),
                              "late_window_seconds": inc.late_window, "stored_watermark": as_text(stored_watermark)}
    missing = [c for c in (*inc.key, inc.watermark) if c not in columns]
    if missing:
        return not_measured(f"the output does not carry {', '.join(missing)}, so rows cannot be matched to the "
                            "committed output", **detail)
    stored = _wm(stored_watermark)
    if stored is None:
        return not_measured("no incremental run has committed a watermark yet: the first run reads every row",
                            **detail)
    cutoff = shift(stored, inc.late_window)
    detail["cutoff"] = as_text(cutoff)
    ki, wi = [list(columns).index(k) for k in inc.key], list(columns).index(inc.watermark)
    behind = {}
    for r in rows:
        w = _wm(r[wi])
        if w is not None and w < cutoff:
            key = tuple(r[i] for i in ki)
            behind[key] = max(w, behind.get(key, w))
    detail["candidate_rows_behind_window"] = len(behind)
    if not behind:
        return {"late_rows": 0, "late_rows_reason": None, "late": {**detail, "committed_rows_read": 0}}
    try:
        held, complete = committed(cutoff)
    except NotMeasured as exc:
        return not_measured(str(exc), **detail)
    if not complete:
        return not_measured("the committed output behind the cutoff is larger than one governed read returns",
                            **detail, committed_rows_read=len(held))
    have: dict[tuple[Any, ...], Any] = {}
    for r in held:
        key, w = tuple(r[:-1]), _wm(r[-1])
        if key not in have or (w is not None and (have[key] is None or w > have[key])):
            have[key] = w
    late = sum(1 for key, w in behind.items() if key not in have or have[key] is None or have[key] < w)
    return {"late_rows": late, "late_rows_reason": None, "late": {**detail, "committed_rows_read": len(held)}}
