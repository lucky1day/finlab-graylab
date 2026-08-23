from __future__ import annotations

from datetime import date, timedelta

import pytest

from shared.period_average_buckets import (
    bucket_for_anchor,
    build_period_buckets,
    complete_bucket_average,
    detect_spring_boundary,
    target_pointer,
)


def _calendar(start: str, end: str, closures: set[str] | None = None) -> list[dict]:
    closed = closures or set()
    day = date.fromisoformat(start)
    final = date.fromisoformat(end)
    rows = []
    while day <= final:
        rows.append(
            {
                "rdate": day.isoformat(),
                "trade_flag": "0" if day.isoformat() in closed else "1",
            }
        )
        day += timedelta(days=1)
    return rows


def _spring_closure(start: str, end: str) -> set[str]:
    day = date.fromisoformat(start)
    final = date.fromisoformat(end)
    result = set()
    while day <= final:
        result.add(day.isoformat())
        day += timedelta(days=1)
    return result


def test_task_specs_include_three_bucket_horizon_combinations() -> None:
    from shared.task_specs import TASK_COMBINATIONS

    assert TASK_COMBINATIONS["monthly_average"] == (
        1,
        "target_month_average_yield_vs_feature_month_average_yield",
        "monthly",
    )
    assert TASK_COMBINATIONS["quarterly_average"] == (
        1,
        "target_quarter_average_yield_vs_feature_quarter_average_yield",
        "quarterly",
    )
    assert TASK_COMBINATIONS["annual_average"] == (
        1,
        "target_year_average_yield_vs_feature_year_average_yield",
        "annual",
    )


def test_mid_bucket_uses_previous_16_through_current_15() -> None:
    rows = _calendar("2024-05-01", "2024-07-31")

    bucket = bucket_for_anchor("monthly_average", "2024-06-14", rows)

    assert bucket.label == "MID-2024-06"
    assert bucket.start_date == "2024-05-16"
    assert bucket.end_date == "2024-06-15"
    assert bucket.anchor_date == "2024-06-14"
    assert bucket.trading_days[0] == "2024-05-16"
    assert bucket.trading_days[-1] == "2024-06-14"
    assert target_pointer(bucket.anchor_date) == "2024-06-15"


def test_calendar_quarter_anchor_falls_back_to_last_trading_day() -> None:
    rows = _calendar("2024-01-01", "2024-06-30")

    bucket = bucket_for_anchor("quarterly_average", "2024-03-29", rows)

    assert bucket.label == "CQ-2024-Q1"
    assert bucket.start_date == "2024-01-01"
    assert bucket.end_date == "2024-03-31"
    assert bucket.anchor_date == "2024-03-29"
    assert target_pointer(bucket.anchor_date) == "2024-03-30"


def test_spring_year_uses_unique_longest_trading_gap() -> None:
    closures = _spring_closure("2023-01-21", "2023-01-29")
    closures.update(_spring_closure("2024-02-09", "2024-02-18"))
    rows = _calendar("2023-01-01", "2024-03-15", closures)

    boundary = detect_spring_boundary(2024, rows)
    bucket = bucket_for_anchor("annual_average", "2024-02-08", rows)

    assert boundary.previous_trading_day == "2024-02-08"
    assert boundary.next_trading_day == "2024-02-19"
    assert boundary.gap_days == 11
    assert bucket.label == "SF-2023"
    assert bucket.start_date == "2023-01-30"
    assert bucket.end_date == "2024-02-08"
    assert bucket.anchor_date == "2024-02-08"
    assert target_pointer(bucket.anchor_date) == "2024-02-09"


def test_spring_detection_rejects_tied_longest_gap() -> None:
    closures = _spring_closure("2024-01-08", "2024-01-12")
    closures.update(_spring_closure("2024-02-05", "2024-02-09"))
    rows = _calendar("2024-01-01", "2024-03-15", closures)

    with pytest.raises(ValueError, match="not unique"):
        detect_spring_boundary(2024, rows)


def test_spring_detection_rejects_gap_shorter_than_six_days() -> None:
    rows = _calendar("2024-01-01", "2024-03-15")

    with pytest.raises(ValueError, match="shorter than 6"):
        detect_spring_boundary(2024, rows)


def test_calendar_must_be_contiguous_and_unique() -> None:
    missing = _calendar("2024-01-01", "2024-03-31")
    missing.pop(10)
    with pytest.raises(ValueError, match="not contiguous"):
        build_period_buckets("quarterly_average", missing)

    duplicate = _calendar("2024-01-01", "2024-03-31")
    duplicate.append(dict(duplicate[-1]))
    with pytest.raises(ValueError, match="duplicate trade calendar date"):
        build_period_buckets("quarterly_average", duplicate)


def test_bucket_average_requires_complete_unique_finite_observations() -> None:
    rows = _calendar("2024-05-01", "2024-06-30")
    bucket = bucket_for_anchor("monthly_average", "2024-06-14", rows)
    observations = [
        {"trade_date": day, "close_yield": float(index + 1)}
        for index, day in enumerate(bucket.trading_days)
    ]

    expected = sum(row["close_yield"] for row in observations) / len(observations)
    assert complete_bucket_average(bucket, observations) == expected

    with pytest.raises(ValueError, match="missing trading dates"):
        complete_bucket_average(bucket, observations[:-1])
    with pytest.raises(ValueError, match="duplicate period-average observation"):
        complete_bucket_average(bucket, observations + [dict(observations[-1])])
    invalid = [dict(row) for row in observations]
    invalid[-1]["close_yield"] = float("nan")
    with pytest.raises(ValueError, match="invalid period-average observation"):
        complete_bucket_average(bucket, invalid)


def test_unknown_task_type_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported period-average task_type"):
        build_period_buckets("monthly", _calendar("2024-01-01", "2024-03-31"))
