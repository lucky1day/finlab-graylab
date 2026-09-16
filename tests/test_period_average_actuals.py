from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from scheduler.repository import upsert_monthly_actuals, upsert_period_average_actuals
from shared.actual_facts import build_period_average_actual_records_from_rows
from shared.models import MonthlyActualRecord, PeriodAverageActualRecord


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
    invalid = [dict(item) for item in complete]
    invalid[-1]["close_yield"] = float("nan")
    with pytest.raises(ValueError, match="invalid period-average observation"):
        build_period_average_actual_records_from_rows(
            invalid,
            calendar,
            task_types=("quarterly_average",),
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
        selected = next(item for item in records if item.predict_date == "2024-02-15")
        assert upsert_period_average_actuals(engine, [selected]) == 1
        assert upsert_period_average_actuals(engine, [selected]) == 1
        with engine.connect() as connection:
            original_id = connection.execute(
                text(
                    "SELECT id FROM t_scheme_period_average_actuals "
                    "WHERE tenor = '10Y' AND predict_date = '2024-02-15' "
                    "AND target_rule = "
                    "'target_month_average_yield_vs_feature_month_average_yield'"
                )
            ).scalar_one()
        updated = replace(
            selected,
            target_yield=3.0,
            actual_direction=1,
            price_signal="空",
            extra={"revision": "same-business-key"},
        )
        assert upsert_period_average_actuals(engine, [updated]) == 1
        other_rule = replace(updated, target_rule="other_target_rule")
        assert upsert_period_average_actuals(engine, [other_rule]) == 1
        with engine.connect() as connection:
            stored = connection.execute(
                text(
                    "SELECT actual_direction, target_rule, extra "
                    "FROM t_scheme_period_average_actuals "
                    "WHERE predict_date = '2024-02-15' "
                    "ORDER BY target_rule"
                )
            ).all()
        assert len(stored) == 2
        original_rule = next(
            row
            for row in stored
            if row[1] == "target_month_average_yield_vs_feature_month_average_yield"
        )
        assert original_rule[0] == 1
        assert "same-business-key" in original_rule[2]
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT id FROM t_scheme_period_average_actuals "
                    "WHERE tenor = '10Y' AND predict_date = '2024-02-15' "
                    "AND target_rule = "
                    "'target_month_average_yield_vs_feature_month_average_yield'"
                )
            ).scalar_one() == original_id
    finally:
        engine.dispose()


def test_period_average_repository_rolls_back_batch_on_invalid_second_row() -> None:
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
        valid = _period_average_record("2024-02-15")
        invalid = replace(
            _period_average_record("2024-03-15"),
            target_yield=None,  # type: ignore[arg-type]
        )

        with pytest.raises(IntegrityError):
            upsert_period_average_actuals(engine, [valid, invalid])

        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM t_scheme_period_average_actuals")
            ).scalar_one() == 0
    finally:
        engine.dispose()


def _period_average_record(predict_date: str) -> PeriodAverageActualRecord:
    return PeriodAverageActualRecord(
        tenor="10Y",
        predict_date=predict_date,
        feature_date=predict_date,
        target_date=predict_date,
        feature_yield=2.0,
        target_yield=1.0,
        actual_direction=-1,
        price_signal="多",
        target_rule="target_month_average_yield_vs_feature_month_average_yield",
        extra={"source": "test"},
    )


def test_monthly_actual_repository_sqlite_upsert_matches_mysql_contract() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE t_scheme_monthly_actuals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tenor TEXT NOT NULL,
                        feature_month_id TEXT NOT NULL,
                        target_month_id TEXT NOT NULL,
                        predict_date TEXT NOT NULL,
                        feature_date TEXT NOT NULL,
                        target_date TEXT NOT NULL,
                        feature_yield REAL NOT NULL,
                        target_yield REAL NOT NULL,
                        direction_monthly INTEGER NOT NULL,
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
        original = _monthly_record("2024-02-15")
        assert upsert_monthly_actuals(engine, [original]) == 1
        assert upsert_monthly_actuals(engine, [original]) == 1
        with engine.connect() as connection:
            original_id = connection.execute(
                text(
                    "SELECT id FROM t_scheme_monthly_actuals "
                    "WHERE tenor = '10Y' AND predict_date = '2024-02-15' "
                    "AND target_rule = 'monthly_rule'"
                )
            ).scalar_one()
        updated = replace(
            original,
            target_yield=3.0,
            direction_monthly=1,
            price_signal="空",
            extra={"revision": "same-business-key"},
        )
        assert upsert_monthly_actuals(engine, [updated]) == 1
        assert upsert_monthly_actuals(
            engine,
            [replace(updated, target_rule="other_target_rule")],
        ) == 1

        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT direction_monthly, target_rule, extra "
                    "FROM t_scheme_monthly_actuals ORDER BY target_rule"
                )
            ).all()
        assert len(rows) == 2
        original_rule = next(row for row in rows if row[1] == "monthly_rule")
        assert original_rule[0] == 1
        assert "same-business-key" in original_rule[2]
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT id FROM t_scheme_monthly_actuals "
                    "WHERE tenor = '10Y' AND predict_date = '2024-02-15' "
                    "AND target_rule = 'monthly_rule'"
                )
            ).scalar_one() == original_id
    finally:
        engine.dispose()


def test_monthly_actual_repository_rolls_back_batch_on_invalid_second_row() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE t_scheme_monthly_actuals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tenor TEXT NOT NULL,
                        feature_month_id TEXT NOT NULL,
                        target_month_id TEXT NOT NULL,
                        predict_date TEXT NOT NULL,
                        feature_date TEXT NOT NULL,
                        target_date TEXT NOT NULL,
                        feature_yield REAL NOT NULL,
                        target_yield REAL NOT NULL,
                        direction_monthly INTEGER NOT NULL,
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
        valid = _monthly_record("2024-02-15")
        invalid = replace(
            _monthly_record("2024-03-15"),
            target_yield=None,  # type: ignore[arg-type]
        )

        with pytest.raises(IntegrityError):
            upsert_monthly_actuals(engine, [valid, invalid])

        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM t_scheme_monthly_actuals")
            ).scalar_one() == 0
    finally:
        engine.dispose()


def _monthly_record(predict_date: str) -> MonthlyActualRecord:
    return MonthlyActualRecord(
        tenor="10Y",
        feature_month_id="2024-02",
        target_month_id="2024-03",
        predict_date=predict_date,
        feature_date=predict_date,
        target_date=predict_date,
        feature_yield=2.0,
        target_yield=1.0,
        direction_monthly=-1,
        price_signal="多",
        target_rule="monthly_rule",
        extra={"source": "test"},
    )
