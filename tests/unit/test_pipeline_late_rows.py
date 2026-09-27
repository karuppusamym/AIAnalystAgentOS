"""P6-02 `late_rows` of a pipeline reconciliation: candidate rows behind `committed watermark - late_window`
that the committed output does not hold. Measured -> a count; not measured -> None with a reason, never 0."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from analystos.contracts.recipe import Incremental
from analystos.pipelines import late

INC = Incremental(watermark="updated_at", key=["id"], late_window="10m")
COLS = ["id", "amount", "updated_at"]
ROWS = [["a", 1, "2031-01-01T00:00:00"],  # behind the window, committed as-is
        ["b", 2, "2031-01-01T00:05:00"],  # behind the window, never committed: late
        ["c", 3, "2031-01-01T00:40:00"],  # behind the window, committed at an older watermark: a late update
        ["d", 4, "2031-01-01T00:55:00"]]  # inside the window: the next run reads it
STORED = "2031-01-01T01:00:00"  # cutoff = 00:50


def _committed(rows, complete=True):
    seen = []

    def read(cutoff):
        seen.append(cutoff)
        return rows, complete
    return read, seen


def test_without_a_watermark_late_rows_is_not_measured():
    out = late.measure(None, COLS, ROWS, stored_watermark=STORED, committed=_committed([])[0])
    assert out["late_rows"] is None and "no incremental watermark" in out["late_rows_reason"]


def test_late_rows_counts_rows_behind_the_window_the_output_does_not_hold():
    read, seen = _committed([["a", datetime(2031, 1, 1, 0, 0, tzinfo=UTC)], ["c", "2031-01-01T00:20:00"]])
    out = late.measure(INC, COLS, ROWS, stored_watermark=STORED, committed=read)
    assert out["late_rows"] == 2 and out["late_rows_reason"] is None
    assert seen == [datetime(2031, 1, 1, 0, 50)]
    assert out["late"]["cutoff"] == "2031-01-01T00:50:00" and out["late"]["candidate_rows_behind_window"] == 3
    assert out["late"]["committed_rows_read"] == 2 and out["late"]["late_window_seconds"] == 600


def test_nothing_behind_the_window_is_a_measured_zero_without_a_read():
    read, seen = _committed([])
    out = late.measure(INC, COLS, [ROWS[3]], stored_watermark=STORED, committed=read)
    assert out["late_rows"] == 0 and seen == []


@pytest.mark.parametrize(("stored", "cols", "read", "reason"), [
    (None, COLS, _committed([])[0], "no incremental run has committed"),
    (STORED, ["id", "amount"], _committed([])[0], "does not carry updated_at"),
    (STORED, COLS, _committed([["a", "2031-01-01T00:00:00"]], complete=False)[0], "larger than one governed read"),
])
def test_what_was_not_measured_is_none_with_why(stored, cols, read, reason):
    rows = [r[:len(cols)] for r in ROWS]
    out = late.measure(INC, cols, rows, stored_watermark=stored, committed=read)
    assert out["late_rows"] is None and reason in out["late_rows_reason"]


def test_an_unreadable_committed_output_is_not_measured():
    def refuse(_cutoff):
        raise late.NotMeasured("the committed output x.y is not in your data scope")

    out = late.measure(INC, COLS, ROWS, stored_watermark=STORED, committed=refuse)
    assert out["late_rows"] is None and "not in your data scope" in out["late_rows_reason"]


def test_a_numeric_watermark_shifts_by_units():
    inc = Incremental(watermark="seq", key=["id"], late_window=5)
    read, seen = _committed([])
    out = late.measure(inc, ["id", "seq"], [["x", 90], ["y", 97]], stored_watermark="100", committed=read)
    assert seen == [95] and out["late_rows"] == 1
