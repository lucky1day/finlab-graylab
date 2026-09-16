from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine, text

from scheduler.actuals_errors import ActualSourceIncompleteError
from scheduler.actuals_runner import ActualsJobError, run_actuals_job
from scheduler.repository import (
    ActualWriteStats,
    upsert_actuals,
    upsert_actuals_detailed,
    upsert_period_average_actuals_detailed,
)
from shared.models import ActualRecord, PeriodAverageActualRecord


def test_daily_actual_detailed_write_is_observable_noop_then_change() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE t_scheme_actuals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tenor TEXT NOT NULL,
                        trade_date TEXT NOT NULL,
                        close_yield REAL NOT NULL,
                        direction_1d INTEGER,
                        direction_5d INTEGER,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE (tenor, trade_date)
                    )
                    """
                )
            )
        original = ActualRecord("10Y", "2026-09-15", 1.85, -1, 1)

        assert upsert_actuals_detailed(engine, [original]) == ActualWriteStats(
            attempted=1,
            inserted=1,
        )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE t_scheme_actuals SET updated_at = '2000-01-01 00:00:00'"
                )
            )

        assert upsert_actuals_detailed(engine, [original]) == ActualWriteStats(
            attempted=1,
            unchanged=1,
        )
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT updated_at FROM t_scheme_actuals")
            ).scalar_one() == "2000-01-01 00:00:00"

        changed = replace(original, close_yield=1.9, direction_1d=1)
        assert upsert_actuals_detailed(engine, [changed]) == ActualWriteStats(
            attempted=1,
            changed=1,
        )
        with engine.connect() as connection:
            stored = connection.execute(
                text(
                    "SELECT close_yield, direction_1d, updated_at "
                    "FROM t_scheme_actuals"
                )
            ).one()
        assert stored[0:2] == (1.9, 1)
        assert stored[2] != "2000-01-01 00:00:00"

        # 旧入口继续返回本批处理条数，不把该数值解释为新增数。
        assert upsert_actuals(engine, [changed]) == 1
    finally:
        engine.dispose()


def test_period_average_full_window_refresh_does_not_rewrite_unchanged_fact() -> None:
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
        record = PeriodAverageActualRecord(
            tenor="10Y",
            predict_date="2025-01-15",
            feature_date="2025-01-15",
            target_date="2025-02-15",
            feature_yield=2.0,
            target_yield=1.9,
            actual_direction=-1,
            price_signal="多",
            target_rule="target_month_average_yield_vs_feature_month_average_yield",
            extra={"source": "test"},
        )
        assert upsert_period_average_actuals_detailed(
            engine, [record]
        ).inserted == 1
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE t_scheme_period_average_actuals "
                    "SET updated_at = '2000-01-01 00:00:00'"
                )
            )

        stats = upsert_period_average_actuals_detailed(engine, [record])
        assert stats == ActualWriteStats(attempted=1, unchanged=1)
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT updated_at FROM t_scheme_period_average_actuals"
                )
            ).scalar_one() == "2000-01-01 00:00:00"
    finally:
        engine.dispose()


def test_actuals_runner_preserves_completed_stage_and_stable_failure_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = Mock()
    calendar = Mock()
    calendar.covers.return_value = True
    calendar.is_trading_day.return_value = True
    daily_stats = ActualWriteStats(attempted=5, inserted=5)
    monthly = Mock(return_value=ActualWriteStats())
    period_average = Mock(return_value=ActualWriteStats())

    with (
        patch("scheduler.actuals_runner.create_engine_from_env", return_value=engine),
        patch("scheduler.actuals_runner.get_calendar", return_value=calendar),
        patch(
            "scheduler.actuals_runner.update_actuals_detailed",
            return_value=daily_stats,
        ),
        patch(
            "scheduler.weekly_actuals_updater.resolve_actual_tenors",
            return_value=["10Y"],
        ),
        patch(
            "scheduler.weekly_actuals_updater.read_yield_rows",
            return_value=[],
        ),
        patch(
            "scheduler.actuals_runner.update_monthly_actuals_detailed",
            monthly,
        ),
        patch(
            "scheduler.actuals_runner.update_period_average_actuals_detailed",
            period_average,
        ),
    ):
        with pytest.raises(ActualsJobError) as raised:
            run_actuals_job("2026-09-15")

    summary = raised.value.summary
    assert summary.overall == "partial"
    assert [(item.stage, item.status) for item in summary.stages] == [
        ("daily", "success"),
        ("weekly", "failed"),
        ("monthly", "not_run"),
        ("period_average", "not_run"),
    ]
    assert summary.stages[0].stats == daily_stats
    assert summary.stages[1].code == "source_incomplete"
    assert summary.stages[1].error_type == "ActualSourceIncompleteError"
    assert isinstance(raised.value.cause, ActualSourceIncompleteError)
    exception_record = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("actuals_stage_exception")
    )
    assert exception_record.exc_info is not None
    terminal_record = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("actuals_stage_terminal")
    )
    assert "missing active tenors" not in terminal_record.getMessage()
    monthly.assert_not_called()
    period_average.assert_not_called()
    engine.dispose.assert_called_once_with()
