from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, text

from migrations.runner import (
    MigrationPreflightError,
    _expected_period_average_actuals_schema,
    _validate_period_average_actuals_schema,
)
from scheduler.repository import upsert_period_average_actuals
from shared.actual_facts import build_period_average_actual_records_from_rows


def _calendar_rows(start: str, end: str, closures: set[str] | None = None) -> list[dict]:
    current = date.fromisoformat(start)
    final = date.fromisoformat(end)
    closed = closures or set()
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


def _yield_rows(calendar_rows: list[dict], value_for_date) -> list[dict]:
    return [
        {
            "trade_date": row["rdate"],
            "tenor": "10Y",
            "close_yield": value_for_date(row["rdate"]),
        }
        for row in calendar_rows
        if row["trade_flag"] == "1"
        and date.fromisoformat(row["rdate"]).weekday() < 5
    ]


def test_period_average_actual_uses_complete_next_mid_bucket() -> None:
    calendar = _calendar_rows("2024-01-01", "2024-03-31")
    rows = _yield_rows(
        calendar,
        lambda day: 2.0 if day <= "2024-02-15" else 1.0,
    )

    records = build_period_average_actual_records_from_rows(
        rows,
        calendar,
        task_types=("monthly_average",),
    )
    selected = next(item for item in records if item.predict_date == "2024-02-15")

    assert selected.feature_date == "2024-02-15"
    assert selected.target_date == "2024-02-16"
    assert selected.feature_yield == 2.0
    assert selected.target_yield == 1.0
    assert selected.actual_direction == -1
    assert selected.price_signal == "多"
    assert selected.extra["feature_bucket"]["label"] == "MID-2024-02"
    assert selected.extra["target_bucket"]["label"] == "MID-2024-03"


def test_period_average_actual_skips_unfinished_target_bucket() -> None:
    calendar = _calendar_rows("2024-01-01", "2024-06-30")
    rows = _yield_rows(calendar, lambda _day: 2.0)
    rows = [item for item in rows if item["trade_date"] <= "2024-05-31"]

    records = build_period_average_actual_records_from_rows(
        rows,
        calendar,
        task_types=("quarterly_average",),
    )

    assert records == []


def test_period_average_actual_fails_on_missing_or_duplicate_bucket_fact() -> None:
    calendar = _calendar_rows("2024-01-01", "2024-06-30")
    complete = _yield_rows(calendar, lambda _day: 2.0)
    missing = [item for item in complete if item["trade_date"] != "2024-02-01"]

    with pytest.raises(ValueError, match="missing trading dates"):
        build_period_average_actual_records_from_rows(
            missing,
            calendar,
            task_types=("quarterly_average",),
        )
    with pytest.raises(ValueError, match="duplicate source actual fact"):
        build_period_average_actual_records_from_rows(
            [*complete, dict(complete[0])],
            calendar,
            task_types=("quarterly_average",),
        )


def test_period_average_actual_ignores_missing_bucket_before_start_date() -> None:
    calendar = _calendar_rows("2024-01-01", "2025-06-30")
    complete = _yield_rows(calendar, lambda _day: 2.0)
    rows = [item for item in complete if item["trade_date"] != "2024-02-01"]

    records = build_period_average_actual_records_from_rows(
        rows,
        calendar,
        task_types=("quarterly_average",),
        start_date="2025-01-01",
    )

    assert [item.target_date for item in records] == [
        "2025-01-01",
        "2025-04-01",
    ]


def test_period_average_actual_rejects_missing_bucket_after_start_date() -> None:
    calendar = _calendar_rows("2024-01-01", "2025-06-30")
    complete = _yield_rows(calendar, lambda _day: 2.0)
    rows = [item for item in complete if item["trade_date"] != "2025-02-03"]

    with pytest.raises(ValueError, match="missing trading dates"):
        build_period_average_actual_records_from_rows(
            rows,
            calendar,
            task_types=("quarterly_average",),
            start_date="2025-01-01",
        )


def test_period_average_repository_writes_one_generic_table() -> None:
    calendar = _calendar_rows("2024-01-01", "2024-03-31")
    records = build_period_average_actual_records_from_rows(
        _yield_rows(calendar, lambda day: 2.0 if day <= "2024-02-15" else 1.0),
        calendar,
        task_types=("monthly_average",),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE t_scheme_period_average_actuals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tenor TEXT NOT NULL,
                        predict_date TEXT NOT NULL,
                        feature_date TEXT NOT NULL,
                        target_date TEXT NOT NULL,
                        feature_yield REAL NOT NULL,
                        target_yield REAL NOT NULL,
                        actual_direction INTEGER NOT NULL,
                        price_signal TEXT NOT NULL,
                        target_rule TEXT NOT NULL,
                        extra TEXT,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE (tenor, predict_date, target_rule)
                    )
                    """
                )
            )
        assert upsert_period_average_actuals(engine, records) == len(records)
        with engine.connect() as connection:
            stored = connection.execute(
                text(
                    "SELECT actual_direction, target_rule, extra "
                    "FROM t_scheme_period_average_actuals "
                    "WHERE predict_date = '2024-02-15'"
                )
            ).one()
        assert stored[0] == -1
        assert stored[1] == "target_month_average_yield_vs_feature_month_average_yield"
        assert "MID-2024-03" in stored[2]
    finally:
        engine.dispose()


def test_period_average_migration_schema_is_closed_world() -> None:
    expected = {"exists": True, **_expected_period_average_actuals_schema()}
    _validate_period_average_actuals_schema({"exists": False}, allow_missing=True)
    _validate_period_average_actuals_schema(expected, allow_missing=False)

    drifted = dict(expected)
    drifted["indexes"] = {}
    with pytest.raises(MigrationPreflightError, match="unexpected"):
        _validate_period_average_actuals_schema(drifted, allow_missing=False)
    with pytest.raises(MigrationPreflightError, match="missing"):
        _validate_period_average_actuals_schema(
            {"exists": False},
            allow_missing=False,
        )
