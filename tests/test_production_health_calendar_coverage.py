"""生产健康检查在日历覆盖耗尽时必须 fail-closed。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from scripts.check_production_daily_health import (
    DailyHealthSnapshot,
    evaluate_daily_health,
)


CALENDAR_ROWS = [
    ("2026-06-01", "1"),
    ("2026-06-07", "0"),
]
UNCOVERED = "2026-07-01"


@pytest.fixture
def calendar_engine() -> Iterator[Engine]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE t_trade_calendar "
                "(rdate TEXT PRIMARY KEY, trade_flag TEXT)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO t_trade_calendar (rdate, trade_flag) "
                "VALUES (:rdate, :trade_flag)"
            ),
            [
                {"rdate": rdate, "trade_flag": trade_flag}
                for rdate, trade_flag in CALENDAR_ROWS
            ],
        )
    yield engine
    engine.dispose()


def test_uncovered_predict_date_fails_before_business_queries(
    calendar_engine: Engine,
) -> None:
    from scripts import check_production_daily_health as health

    with pytest.raises(ValueError, match=UNCOVERED):
        health.load_snapshot(calendar_engine, predict_date=UNCOVERED)


@pytest.mark.parametrize(
    ("is_trading_day", "expected_missing"),
    [(True, True), (False, False)],
    ids=("trading-day", "holiday"),
)
def test_prediction_missing_check_respects_trading_day(
    is_trading_day: bool,
    expected_missing: bool,
) -> None:
    snapshot = DailyHealthSnapshot(
        predict_date="2026-06-04",
        expected_feature_date="2026-06-02",
        is_trading_day=is_trading_day,
        active_daily_base_schemes=("demo",),
        successful_daily_run_schemes=(),
        predictions_count=0,
        run_prediction_counts=(),
        prediction_date_checks=(),
        actual_watermarks=(),
    )
    codes = {finding.code for finding in evaluate_daily_health(snapshot)}

    assert ("daily_predictions_missing" in codes) is expected_missing
