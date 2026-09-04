from __future__ import annotations

from datetime import date, timedelta

from shared.period_average_buckets import (
    bucket_for_anchor,
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
