from __future__ import annotations

from datetime import date, timedelta

import pytest

from shared.blackbox_v2.contracts import BlackboxMetadata
from shared.blackbox_v2.history import _period_average_candidates
from shared.blackbox_v2.requests import build_live_request
from shared.blackbox_v2.snapshot import CutoffKeys


def _calendar_rows(start: str, end: str, closures: set[str] | None = None) -> list[dict]:
    closed = closures or set()
    current = date.fromisoformat(start)
    final = date.fromisoformat(end)
    rows = []
    while current <= final:
        rows.append(
            {
                "rdate": current.isoformat(),
                "trade_flag": "0" if current.isoformat() in closed else "1",
            }
        )
        current += timedelta(days=1)
    return rows


class _Calendar:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def period_calendar_rows(self):
        return tuple(self._rows)


def _metadata(task_type: str, target_rule: str, frequency: str) -> BlackboxMetadata:
    return BlackboxMetadata(
        schema_version="1.0",
        scheme_id=f"demo_{task_type}",
        name="Demo",
        algorithm_version="1.0.0",
        target_tenor="10Y",
        task_type=task_type,
        horizon=1,
        target_rule=target_rule,
        frequency=frequency,
    )


def _constant_yield_rows(
    calendar_rows: list[dict],
    *,
    missing_date: str,
) -> list[dict]:
    return [
        {
            "trade_date": row["rdate"],
            "tenor": "10Y",
            "close_yield": 2.0,
        }
        for row in calendar_rows
        if date.fromisoformat(row["rdate"]).weekday() < 5
        and row["rdate"] != missing_date
    ]


def test_monthly_average_live_request_uses_anchor_and_next_day_pointer() -> None:
    metadata = _metadata(
        "monthly_average",
        "target_month_average_yield_vs_feature_month_average_yield",
        "monthly",
    )
    cutoffs = CutoffKeys("2024-06-14", "202424", "202406")

    request = build_live_request(
        metadata,
        predict_date="2024-06-14",
        calendar=_Calendar(_calendar_rows("2024-05-01", "2024-07-31")),
        cutoffs=cutoffs,
    )

    assert request.predict_date == "2024-06-14"
    assert request.feature_date == "2024-06-14"
    assert request.target_date == "2024-06-15"
    assert request.daily_cutoff_key == "2024-06-14"


def test_period_average_request_rejects_non_anchor_or_mismatched_cutoff() -> None:
    metadata = _metadata(
        "quarterly_average",
        "target_quarter_average_yield_vs_feature_quarter_average_yield",
        "quarterly",
    )
    calendar = _Calendar(_calendar_rows("2024-01-01", "2024-06-30"))
    with pytest.raises(ValueError, match="expected one quarterly_average bucket"):
        build_live_request(
            metadata,
            predict_date="2024-03-28",
            calendar=calendar,
            cutoffs=CutoffKeys("2024-03-28", "202413", "202403"),
        )
    with pytest.raises(ValueError, match="daily_cutoff_key must equal feature_date"):
        build_live_request(
            metadata,
            predict_date="2024-03-29",
            calendar=calendar,
            cutoffs=CutoffKeys("2024-03-28", "202413", "202403"),
        )


def test_period_average_history_uses_next_bucket_mean_and_pointer_date() -> None:
    calendar_rows = _calendar_rows("2024-01-01", "2024-03-31")
    metadata = _metadata(
        "monthly_average",
        "target_month_average_yield_vs_feature_month_average_yield",
        "monthly",
    )
    trading_dates = [
        row["rdate"]
        for row in calendar_rows
        if date.fromisoformat(row["rdate"]).weekday() < 5
    ]
    yield_rows = [
        {
            "trade_date": trading_date,
            "tenor": "10Y",
            "close_yield": 2.0 if trading_date <= "2024-02-15" else 1.0,
        }
        for trading_date in trading_dates
    ]

    candidates = _period_average_candidates(metadata, yield_rows, calendar_rows)
    selected = next(
        item for item in candidates if item.predict_date == "2024-02-15"
    )

    assert selected.feature_date == "2024-02-15"
    assert selected.target_date == "2024-02-16"
    assert selected.label == -1
    assert selected.actual_extra["feature_bucket"]["label"] == "MID-2024-02"
    assert selected.actual_extra["target_bucket"]["label"] == "MID-2024-03"
    assert selected.actual_extra["actual_target_anchor"] == "2024-03-15"


def test_period_average_history_rejects_missing_bucket_trading_day() -> None:
    calendar_rows = _calendar_rows("2024-01-01", "2024-06-30")
    metadata = _metadata(
        "quarterly_average",
        "target_quarter_average_yield_vs_feature_quarter_average_yield",
        "quarterly",
    )
    yield_rows = _constant_yield_rows(
        calendar_rows,
        missing_date="2024-02-01",
    )

    with pytest.raises(ValueError, match="missing trading dates"):
        _period_average_candidates(metadata, yield_rows, calendar_rows)


def test_period_average_history_ignores_missing_bucket_before_requested_scope() -> None:
    calendar_rows = _calendar_rows("2024-01-01", "2025-06-30")
    metadata = _metadata(
        "quarterly_average",
        "target_quarter_average_yield_vs_feature_quarter_average_yield",
        "quarterly",
    )
    yield_rows = _constant_yield_rows(
        calendar_rows,
        missing_date="2024-02-01",
    )

    candidates = _period_average_candidates(
        metadata,
        yield_rows,
        calendar_rows,
        predict_date_from="2025-01-01",
        target_date_before="2025-07-01",
    )

    assert [item.predict_date for item in candidates] == ["2025-03-31"]


def test_period_average_history_still_rejects_missing_bucket_in_requested_scope() -> None:
    calendar_rows = _calendar_rows("2024-01-01", "2025-06-30")
    metadata = _metadata(
        "quarterly_average",
        "target_quarter_average_yield_vs_feature_quarter_average_yield",
        "quarterly",
    )
    yield_rows = _constant_yield_rows(
        calendar_rows,
        missing_date="2025-02-03",
    )

    with pytest.raises(ValueError, match="missing trading dates"):
        _period_average_candidates(
            metadata,
            yield_rows,
            calendar_rows,
            predict_date_from="2025-01-01",
            target_date_before="2025-07-01",
        )
