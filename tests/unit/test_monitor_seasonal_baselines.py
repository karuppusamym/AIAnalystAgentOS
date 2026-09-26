"""Seasonal volume baselines in `services/monitors.py` (P6-05): Atlas's 17 pure tests, ported.

Ported from AIDataAnalyst@8b48fd9:tests/test_data_quality_seasonality.py (7, weekday baseline) and
tests/test_data_quality_month_end_seasonality.py (10, month-end baseline). The baselines are ported
as-is; Atlas's `evaluate_quality(profile, profile, ...)` becomes `volume_check(rows, rows, ...)`
(the rest of Atlas's quality evaluator is not ported, ADR-0018 §4), so only the call shape changes:
`anomaly_types` -> `anomaly`, `severities["VOLUME_CHANGE"]` -> `severity`, statuses in lower case.
The measured claims are unchanged: 8/8 weekend and 3/3 month-end false positives of the rolling
comparison go to 0, and real collapses stay critical.
"""

import calendar
from datetime import UTC, datetime, timedelta

from analystos.services.monitors import day_of_month_baseline, day_of_week_baseline, volume_check

# ======================================================== weekday (DQ-6)

_wd_WEEK_0_MONDAY = datetime(2026, 6, 1, 6, 0, tzinfo=UTC)
_wd_WEEKDAY_BASE = 1000
_wd_WEEKEND_BASE = 400


def _wd_row_count_for(day_offset: int) -> int:
    """A table's normal row count `day_offset` days after `_wd_WEEK_0_MONDAY`.

    Small, deterministic (not random) +/-1% jitter keyed off the day index, so
    every weekday value is close to 1000 and every weekend value is close to
    400, but no two same-weekday values are bit-for-bit identical -- closer to
    a real table's week-over-week variation than a perfectly flat constant.
    """
    weekday = (_wd_WEEK_0_MONDAY + timedelta(days=day_offset)).weekday()
    base = _wd_WEEKEND_BASE if weekday >= 5 else _wd_WEEKDAY_BASE
    jitter = ((day_offset * 7) % 11) - 5  # deterministic wobble in [-5, 5]
    return base + jitter


def _wd_build_history(num_days: int) -> list[tuple[datetime, int]]:
    return [
        (_wd_WEEK_0_MONDAY + timedelta(days=offset), _wd_row_count_for(offset))
        for offset in range(num_days)
    ]


def _wd_profile(row_count: int) -> int:
    return row_count


# --- 1 & 2: the measured false-positive comparison -----------------------------


def test_seasonality_eliminates_every_weekend_false_positive_naive_flags() -> None:
    """The concrete before/after this row's exit condition asks for.

    Eight weeks (56 days) of history establish the pattern. The next four
    weeks (28 more days, 8 weekend transitions: Fri->Sat and Sun->Mon x4) are
    each evaluated two ways: today's rolling-previous comparison, and the
    seasonality-aware comparison fed that same table's own persisted history.
    Every one of those 8 transitions is a *normal* week for this table -- not
    a single one should ever have opened an incident.
    """
    history = _wd_build_history(56)
    naive_false_positives = 0
    seasonal_false_positives = 0
    transitions_checked = 0

    for offset in range(56, 84):
        previous_day = _wd_WEEK_0_MONDAY + timedelta(days=offset - 1)
        current_day = _wd_WEEK_0_MONDAY + timedelta(days=offset)
        crosses_weekday_boundary = (previous_day.weekday(), current_day.weekday()) in {
            (4, 5),  # Friday -> Saturday
            (6, 0),  # Sunday -> Monday
        }
        if not crosses_weekday_boundary:
            continue  # only the actual weekday<->weekend transitions are the false-positive risk
        transitions_checked += 1

        current_row_count = _wd_row_count_for(offset)
        baseline_row_count = _wd_row_count_for(offset - 1)
        current_profile = _wd_profile(current_row_count)
        baseline_profile = _wd_profile(baseline_row_count)

        naive = volume_check(current_profile, baseline_profile)
        if naive.anomaly:
            naive_false_positives += 1

        seasonal = volume_check(
            current_profile,
            baseline_profile,
            history=history,
            observed_at=current_day,
            weekday_seasonality=True,
        )
        if seasonal.anomaly:
            seasonal_false_positives += 1

    # The measured numbers this row's exit condition ("Reduced false positives,
    # measured") asks for: today's rolling-previous baseline flags every single
    # normal weekend transition; the day-of-week baseline flags none of them.
    assert transitions_checked == 8
    assert naive_false_positives == 8
    assert seasonal_false_positives == 0


def test_single_saturday_before_after_evidence_is_concrete() -> None:
    """One transition in detail: the exact percentages/z-scores behind the tally above."""
    history = _wd_build_history(56)
    saturday = _wd_WEEK_0_MONDAY + timedelta(days=61)  # a week-9 Saturday, ~400 rows, fully normal
    friday_before_it = _wd_WEEK_0_MONDAY + timedelta(days=60)  # ~1000 rows

    current = _wd_profile(_wd_row_count_for(61))
    baseline = _wd_profile(_wd_row_count_for(60))

    naive = volume_check(current, baseline)
    assert naive.evidence["threshold_strategy"] == "ROLLING_PREVIOUS"
    # ~60% drop vs. Friday, comfortably past the 30% default threshold: a false positive.
    assert naive.evidence["volume_change_percent"] > 50.0
    assert naive.anomaly
    assert naive.status in {"warning", "critical"}

    seasonal = volume_check(
        current,
        baseline,
        history=history,
        observed_at=saturday,
        weekday_seasonality=True,
    )
    assert seasonal.evidence["threshold_strategy"] == "SEASONAL_DAY_OF_WEEK"
    assert saturday.weekday() == friday_before_it.weekday() + 1
    assert seasonal.evidence["seasonal_weekday"] == saturday.weekday()
    # Small z-score against this table's own other Saturdays: nowhere near anomalous.
    assert seasonal.evidence["seasonal_zscore"] < 1.5
    assert not seasonal.anomaly
    assert seasonal.status == "healthy"


# --- 3: a true positive is preserved, not lost, by switching baselines --------


def test_seasonal_baseline_still_catches_a_real_weekend_incident() -> None:
    """A Saturday that collapses to near-zero is still an incident under the
    seasonal baseline -- switching away from "compare to Friday" does not mean
    "stop detecting anomalies on weekends", only "stop misjudging a normal
    weekend as one".
    """
    history = _wd_build_history(56)
    saturday = _wd_WEEK_0_MONDAY + timedelta(days=61)

    real_incident_row_count = 20  # this table's Saturdays are normally ~400
    current = _wd_profile(real_incident_row_count)
    baseline = _wd_profile(_wd_row_count_for(60))

    seasonal = volume_check(
        current,
        baseline,
        history=history,
        observed_at=saturday,
        weekday_seasonality=True,
    )
    assert seasonal.evidence["threshold_strategy"] == "SEASONAL_DAY_OF_WEEK"
    assert seasonal.evidence["seasonal_zscore"] > 3.0
    assert seasonal.anomaly
    assert seasonal.severity == "critical"

    # And the naive comparison (Saturday vs. the Friday right before it) also
    # catches it -- nothing about the seasonal path is needed to see *this*
    # drop; the point is only that it no longer needs a real incident to fire.
    naive = volume_check(current, baseline)
    assert naive.anomaly


# --- 4: day_of_week_baseline as a pure function --------------------------------


def test_day_of_week_baseline_groups_by_weekday_only() -> None:
    history = _wd_build_history(26)  # offsets 0..25: 3 full Saturdays (5, 12, 19), none is day 26
    fourth_saturday = _wd_WEEK_0_MONDAY + timedelta(days=26)
    baseline = day_of_week_baseline(history, fourth_saturday, min_samples=3)

    assert baseline is not None
    assert baseline.weekday == 5
    assert baseline.sample_count == 3  # exactly the 3 prior Saturdays (offsets 5, 12, 19)
    assert 395 <= baseline.mean <= 405  # every Saturday in the fixture is ~400, never ~1000


def test_day_of_week_baseline_falls_back_to_none_with_thin_history() -> None:
    # Only 9 days of history: at most 2 same-weekday points for any weekday.
    history = _wd_build_history(9)
    tenth_day = _wd_WEEK_0_MONDAY + timedelta(days=9)
    assert day_of_week_baseline(history, tenth_day, min_samples=3) is None


def test_evaluate_quality_falls_back_when_seasonal_history_is_too_thin() -> None:
    """Even with `weekday_seasonality=True`, a table too new to have 3 same-weekday
    points yet gets the original rolling-previous verdict, not a seasonal guess
    built on too little data -- the safe default this row's flag was designed for.
    """
    thin_history = _wd_build_history(9)
    tenth_day = _wd_WEEK_0_MONDAY + timedelta(days=9)
    current = _wd_profile(_wd_row_count_for(9))
    baseline = _wd_profile(_wd_row_count_for(8))

    result = volume_check(
        current,
        baseline,
        history=thin_history,
        observed_at=tenth_day,
        weekday_seasonality=True,
    )
    assert result.evidence["threshold_strategy"] == "ROLLING_PREVIOUS"


def test_seasonality_disabled_by_default_is_byte_identical_to_before() -> None:
    """Without opting in, passing history/timestamp changes nothing: the flag
    fully gates the new code path, matching this repo's established rollout
    convention for a new quality strategy.
    """
    history = _wd_build_history(56)
    saturday = _wd_WEEK_0_MONDAY + timedelta(days=61)
    current = _wd_profile(_wd_row_count_for(61))
    baseline = _wd_profile(_wd_row_count_for(60))

    without_history = volume_check(current, baseline)
    with_history_but_disabled = volume_check(
        current,
        baseline,
        history=history,
        observed_at=saturday,
        weekday_seasonality=False,
    )
    assert without_history == with_history_but_disabled


# ======================================================== month end (DQ-6 follow-up)

_me_START = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
_me_NORMAL_ROW_COUNT = 1000
_me_CLOSE_ROW_COUNT = 3000
_me_CLOSE_WINDOW_DAYS = 2  # the month's last 2 calendar days count as "month-end"


def _me_is_close_window(day: datetime) -> bool:
    last_day = calendar.monthrange(day.year, day.month)[1]
    return (last_day - day.day) < _me_CLOSE_WINDOW_DAYS


def _me_row_count_for(day_offset: int) -> int:
    """A table's normal row count `day_offset` days after `_me_START`.

    Every day in the month's last `_me_CLOSE_WINDOW_DAYS` is a flat, exact
    month-end close spike (so the zero-stdev percent-change verdict path is
    exercised deterministically); every other day is close to 1000 with small
    deterministic (not random) jitter, closer to a real table's day-to-day
    variation than a perfectly flat constant.
    """
    day = _me_START + timedelta(days=day_offset)
    if _me_is_close_window(day):
        return _me_CLOSE_ROW_COUNT
    jitter = ((day_offset * 7) % 11) - 5  # deterministic wobble in [-5, 5]
    return _me_NORMAL_ROW_COUNT + jitter


def _me_build_history(num_days: int) -> list[tuple[datetime, int]]:
    return [(_me_START + timedelta(days=offset), _me_row_count_for(offset)) for offset in range(num_days)]


def _me_profile(row_count: int) -> int:
    return row_count


# --- 1 & 2: the measured false-positive comparison -----------------------------


def test_month_end_seasonality_eliminates_every_close_window_false_positive() -> None:
    """The concrete before/after this row's exit condition asks for.

    Five months (Jan-May 2026, 151 days) of history establish the close-window
    pattern -- each month's own month-end position seen once. The next three
    months (Jun/Jul/Aug 2026) are each evaluated two ways at the moment they
    enter the close window. Every one of those 3 entries is a *normal* month-end
    for this table -- not one of them should ever open an incident.
    """
    history = _me_build_history(151)  # Jan 1 .. May 31, 2026: five real month-end cycles
    naive_false_positives = 0
    month_end_false_positives = 0
    transitions_checked = 0

    for offset in range(151, 151 + 92):  # Jun, Jul, Aug 2026
        current_day = _me_START + timedelta(days=offset)
        previous_day = _me_START + timedelta(days=offset - 1)
        entering_close_window = _me_is_close_window(current_day) and not _me_is_close_window(previous_day)
        if not entering_close_window:
            continue  # only the actual normal->close-window transition is the false-positive risk
        transitions_checked += 1

        current_profile = _me_profile(_me_row_count_for(offset))
        baseline_profile = _me_profile(_me_row_count_for(offset - 1))

        naive = volume_check(current_profile, baseline_profile)
        if naive.anomaly:
            naive_false_positives += 1

        month_end = volume_check(
            current_profile,
            baseline_profile,
            history=history,
            observed_at=current_day,
            month_end_seasonality=True,
        )
        if month_end.anomaly:
            month_end_false_positives += 1

    # The measured numbers this row's exit condition ("Reduced false positives,
    # measured") asks for: today's rolling-previous baseline flags every single
    # normal close-window entry; the month-end baseline flags none of them.
    assert transitions_checked == 3
    assert naive_false_positives == 3
    assert month_end_false_positives == 0


def test_single_close_window_entry_before_after_evidence_is_concrete() -> None:
    """One transition in detail: June 29, 2026 -- June has 30 days, so day 29 is
    the first day of its 2-day close window (`days_before_month_end == 1`)."""
    history = _me_build_history(151)
    june_29 = _me_START + timedelta(days=179)
    june_28 = _me_START + timedelta(days=178)
    assert june_29.month == 6 and june_29.day == 29
    assert calendar.monthrange(2026, 6)[1] == 30

    current = _me_profile(_me_row_count_for(179))
    baseline = _me_profile(_me_row_count_for(178))

    naive = volume_check(current, baseline)
    assert naive.evidence["threshold_strategy"] == "ROLLING_PREVIOUS"
    # ~1000 -> 3000, comfortably past the 30% default threshold: a false positive.
    assert naive.evidence["volume_change_percent"] > 100.0
    assert naive.anomaly

    month_end = volume_check(
        current,
        baseline,
        history=history,
        observed_at=june_29,
        month_end_seasonality=True,
    )
    assert month_end.evidence["threshold_strategy"] == "SEASONAL_MONTH_END"
    assert month_end.evidence["seasonal_days_before_month_end"] == 1
    # Exactly Jan/Feb/Mar/Apr/May's own "1 day before month end" points.
    assert month_end.evidence["seasonal_sample_count"] == 5
    assert month_end.evidence["seasonal_mean_row_count"] == 3000.0
    assert "seasonal_change_percent" in month_end.evidence  # flat close values -> zero stdev path
    assert not month_end.anomaly
    assert month_end.status == "healthy"
    assert june_28  # the naive baseline day, referenced above for clarity only


# --- 3: a true positive is preserved, not lost, by switching baselines --------


def test_month_end_baseline_still_catches_a_real_close_failure() -> None:
    """A close window that collapses to near-zero (e.g. the reconciliation batch
    failed to run) is still an incident under the month-end baseline -- switching
    away from "compare to the day before" does not mean "stop detecting anomalies
    at month-end", only "stop misjudging a normal close as one"."""
    history = _me_build_history(151)
    june_29 = _me_START + timedelta(days=179)
    baseline = _me_profile(_me_row_count_for(178))

    real_incident_row_count = 50  # this table's close windows are normally exactly 3000
    current = _me_profile(real_incident_row_count)

    month_end = volume_check(
        current,
        baseline,
        history=history,
        observed_at=june_29,
        month_end_seasonality=True,
    )
    assert month_end.evidence["threshold_strategy"] == "SEASONAL_MONTH_END"
    assert month_end.evidence["seasonal_change_percent"] > 90.0
    assert month_end.anomaly
    assert month_end.severity == "critical"

    # And the naive comparison also catches it -- the point is only that the
    # month-end path no longer needs a real incident to fire.
    naive = volume_check(current, baseline)
    assert naive.anomaly


def test_day_of_month_baseline_uses_zscore_when_history_has_real_spread() -> None:
    """A hand-picked, small-numbers case exercising the nonzero-stdev z-score
    path (the flat synthetic close values above always take the zero-stdev
    percent-change path; this proves the other branch independently)."""
    # "1 day before month end" points from three 31-day months, with a real spread.
    history = [
        (datetime(2026, 1, 30, tzinfo=UTC), 100),
        (datetime(2026, 3, 30, tzinfo=UTC), 104),
        (datetime(2026, 5, 30, tzinfo=UTC), 96),
    ]
    current_day = datetime(2026, 7, 30, tzinfo=UTC)  # July: also 31 days, same position

    baseline = day_of_month_baseline(history, current_day, min_samples=3)
    assert baseline is not None
    assert baseline.days_before_month_end == 1
    assert baseline.sample_count == 3
    assert 99.0 < baseline.mean < 101.0
    assert baseline.stdev > 0

    result = volume_check(
        _me_profile(300),  # wildly outside a mean-100 baseline
        _me_profile(100),
        history=history,
        observed_at=current_day,
        month_end_seasonality=True,
    )
    assert result.evidence["threshold_strategy"] == "SEASONAL_MONTH_END"
    assert "seasonal_zscore" in result.evidence
    assert result.evidence["seasonal_zscore"] > 3.0
    assert result.anomaly
    assert result.severity == "critical"


# --- 4: day_of_month_baseline as a pure function, and its fallback ------------


def test_day_of_month_baseline_groups_by_month_end_position_across_month_lengths() -> None:
    history = [
        (datetime(2026, 1, 30, tzinfo=UTC), 3000),  # Jan (31 days): 1 day before month end
        (datetime(2026, 2, 27, tzinfo=UTC), 3000),  # Feb (28 days): 1 day before month end
        (datetime(2026, 3, 30, tzinfo=UTC), 3000),  # Mar (31 days): 1 day before month end
        (datetime(2026, 1, 15, tzinfo=UTC), 1000),  # a normal mid-month day: must not be grouped in
    ]
    fourth_month_end = datetime(2026, 4, 29, tzinfo=UTC)  # Apr (30 days): same position
    baseline = day_of_month_baseline(history, fourth_month_end, min_samples=3)

    assert baseline is not None
    assert baseline.days_before_month_end == 1
    assert baseline.sample_count == 3  # exactly the 3 prior month-ends, not the mid-month point
    assert baseline.mean == 3000.0


def test_day_of_month_baseline_falls_back_to_none_with_thin_history() -> None:
    history = [
        (datetime(2026, 1, 30, tzinfo=UTC), 3000),
        (datetime(2026, 2, 27, tzinfo=UTC), 3000),
    ]
    third_month_end = datetime(2026, 3, 30, tzinfo=UTC)
    assert day_of_month_baseline(history, third_month_end, min_samples=3) is None


def test_evaluate_quality_falls_back_when_month_end_history_is_too_thin() -> None:
    """Even with `month_end_seasonality=True`, a table too new to have 3
    same-position points yet gets the original rolling-previous verdict."""
    thin_history = _me_build_history(60)  # Jan + Feb 2026: only 2 month-end cycles so far
    march_30 = _me_START + timedelta(days=88)  # Mar 30, 2026: 1 day before March's month end
    assert march_30.month == 3 and march_30.day == 30

    current = _me_profile(_me_row_count_for(88))
    baseline = _me_profile(_me_row_count_for(87))

    result = volume_check(
        current,
        baseline,
        history=thin_history,
        observed_at=march_30,
        month_end_seasonality=True,
    )
    assert result.evidence["threshold_strategy"] == "ROLLING_PREVIOUS"


def test_month_end_strategy_not_applied_outside_its_window() -> None:
    """A comfortably mid-month day does not use the month-end baseline even
    when the flag is on -- only readings inside `month_end_window_days` do."""
    history = _me_build_history(151)
    mid_june = _me_START + timedelta(days=165)  # June 15, 2026: far from any month end
    assert mid_june.day == 15

    current = _me_profile(_me_row_count_for(165))
    baseline = _me_profile(_me_row_count_for(164))

    result = volume_check(
        current,
        baseline,
        history=history,
        observed_at=mid_june,
        month_end_seasonality=True,
    )
    assert result.evidence["threshold_strategy"] == "ROLLING_PREVIOUS"


# --- 5: additive, not exclusive -- month-end and day-of-week coexist -----------


def test_both_seasonal_strategies_enabled_month_end_wins_inside_its_window() -> None:
    """With both flags on: a month-end-window reading prefers the month-end
    baseline (the more specific signal for that day); an ordinary day in the
    same run still gets the day-of-week baseline DQ-6 already shipped -- proving
    this row's grouping is additive alongside DQ-6's, not a replacement for it."""
    history = _me_build_history(151)

    june_29 = _me_START + timedelta(days=179)
    in_window = volume_check(
        _me_profile(_me_row_count_for(179)),
        _me_profile(_me_row_count_for(178)),
        history=history,
        observed_at=june_29,
        weekday_seasonality=True,
        month_end_seasonality=True,
    )
    assert in_window.evidence["threshold_strategy"] == "SEASONAL_MONTH_END"

    mid_june = _me_START + timedelta(days=165)
    outside_window = volume_check(
        _me_profile(_me_row_count_for(165)),
        _me_profile(_me_row_count_for(164)),
        history=history,
        observed_at=mid_june,
        weekday_seasonality=True,
        month_end_seasonality=True,
    )
    assert outside_window.evidence["threshold_strategy"] == "SEASONAL_DAY_OF_WEEK"


def test_month_end_seasonality_disabled_by_default_is_byte_identical_to_before() -> None:
    """Without opting in, passing history/timestamp changes nothing: the flag
    fully gates the new code path, matching DQ-6's own rollout convention."""
    history = _me_build_history(151)
    june_29 = _me_START + timedelta(days=179)
    current = _me_profile(_me_row_count_for(179))
    baseline = _me_profile(_me_row_count_for(178))

    without_history = volume_check(current, baseline)
    with_history_but_disabled = volume_check(
        current,
        baseline,
        history=history,
        observed_at=june_29,
        month_end_seasonality=False,
    )
    assert without_history == with_history_but_disabled
