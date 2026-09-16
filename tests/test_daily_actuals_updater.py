from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from scheduler.daily_actuals_updater import update_actuals
from scheduler.repository import (
    plan_actuals_tail_repair,
    repair_actuals_after_source_watermark,
)
from shared.actual_facts import read_actual_source_snapshot
from shared.tenor_mapping import TENOR_TO_INDICATOR


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE api_wind_daily (
                    rdate TEXT NOT NULL,
                    indicators_code TEXT NOT NULL,
                    indicators_value REAL
                )
                """
            )
        )
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
    return engine


def _insert_source(engine, *rows: tuple[str, float]) -> None:
    indicator_code = TENOR_TO_INDICATOR["10Y"]
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO api_wind_daily
                    (rdate, indicators_code, indicators_value)
                VALUES (:rdate, :indicators_code, :indicators_value)
                """
            ),
            [
                {
                    "rdate": trade_date,
                    "indicators_code": indicator_code,
                    "indicators_value": value,
                }
                for trade_date, value in rows
            ],
        )


def test_normal_refresh_does_not_delete_actuals_after_a_source_retreat() -> None:
    engine = _engine()
    try:
        _insert_source(engine, ("2024-01-01", 2.0))
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO t_scheme_actuals
                        (tenor, trade_date, close_yield)
                    VALUES
                        ('10Y', '2024-01-02', 2.1),
                        ('10Y', '2024-01-03', 2.2)
                    """
                )
            )

        assert update_actuals(engine=engine, tenors=("10Y",)) == 1

        with engine.connect() as connection:
            dates = connection.execute(
                text(
                    "SELECT trade_date FROM t_scheme_actuals "
                    "WHERE tenor = '10Y' ORDER BY trade_date"
                )
            ).scalars().all()
        assert dates == ["2024-01-01", "2024-01-02", "2024-01-03"]
    finally:
        engine.dispose()


def test_source_snapshot_identity_does_not_change_after_the_read() -> None:
    engine = _engine()
    try:
        _insert_source(engine, ("2024-01-01", 2.0))
        with engine.connect() as connection:
            snapshot = read_actual_source_snapshot(connection, tenors=("10Y",))

        _insert_source(engine, ("2024-01-02", 2.1))

        assert snapshot.watermarks == {"10Y": "2024-01-01"}
        assert [row["trade_date"] for row in snapshot.rows] == ["2024-01-01"]
        with engine.connect() as connection:
            current = read_actual_source_snapshot(connection, tenors=("10Y",))
        assert current.watermarks == {"10Y": "2024-01-02"}
        assert current.source_digest != snapshot.source_digest
    finally:
        engine.dispose()


def test_tail_repair_rolls_back_upserts_when_delete_fails() -> None:
    engine = _engine()
    try:
        _insert_source(
            engine,
            ("2024-01-01", 2.0),
            ("2024-01-02", 2.1),
        )
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO t_scheme_actuals
                        (tenor, trade_date, close_yield)
                    VALUES
                        ('10Y', '2024-01-01', 9.9),
                        ('10Y', '2024-01-03', 2.2)
                    """
                )
            )
        plan = plan_actuals_tail_repair(
            engine,
            tenors=("10Y",),
        )
        assert plan.business_keys == (("10Y", "2024-01-03"),)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TRIGGER reject_actual_delete
                    BEFORE DELETE ON t_scheme_actuals
                    BEGIN
                        SELECT RAISE(ABORT, 'injected delete failure');
                    END
                    """
                )
            )

        try:
            repair_actuals_after_source_watermark(engine, plan)
        except IntegrityError:
            pass
        else:
            raise AssertionError("injected delete failure did not abort the repair")

        with engine.connect() as connection:
            stored = connection.execute(
                text(
                    "SELECT trade_date, close_yield FROM t_scheme_actuals "
                    "WHERE tenor = '10Y' ORDER BY trade_date"
                )
            ).all()
        assert stored == [("2024-01-01", 9.9), ("2024-01-03", 2.2)]
    finally:
        engine.dispose()


def test_tail_repair_rejects_a_plan_after_the_source_snapshot_changes() -> None:
    engine = _engine()
    try:
        _insert_source(engine, ("2024-01-01", 2.0))
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO t_scheme_actuals
                        (tenor, trade_date, close_yield)
                    VALUES ('10Y', '2024-01-02', 2.1)
                    """
                )
            )
        plan = plan_actuals_tail_repair(engine, tenors=("10Y",))
        assert plan.business_keys == (("10Y", "2024-01-02"),)

        _insert_source(engine, ("2024-01-02", 2.1))

        try:
            repair_actuals_after_source_watermark(engine, plan)
        except RuntimeError as exc:
            assert "source snapshot changed" in str(exc)
        else:
            raise AssertionError("stale source repair plan was not rejected")
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM t_scheme_actuals "
                    "WHERE tenor = '10Y' AND trade_date = '2024-01-02'"
                )
            ).scalar_one() == 1
    finally:
        engine.dispose()


def test_tail_repair_applies_the_previewed_business_keys() -> None:
    engine = _engine()
    try:
        _insert_source(engine, ("2024-01-01", 2.0))
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO t_scheme_actuals
                        (tenor, trade_date, close_yield)
                    VALUES ('10Y', '2024-01-02', 2.1)
                    """
                )
            )
        plan = plan_actuals_tail_repair(engine, tenors=("10Y",))

        assert repair_actuals_after_source_watermark(engine, plan) == (1, 1)

        with engine.connect() as connection:
            stored = connection.execute(
                text(
                    "SELECT trade_date, close_yield FROM t_scheme_actuals "
                    "WHERE tenor = '10Y' ORDER BY trade_date"
                )
            ).all()
        assert stored == [("2024-01-01", 2.0)]
    finally:
        engine.dispose()


def test_normal_refresh_is_idempotent_and_updates_the_existing_business_key() -> None:
    engine = _engine()
    try:
        _insert_source(
            engine,
            ("2024-01-01", 2.0),
            ("2024-01-02", 2.1),
        )
        assert update_actuals(engine=engine, tenors=("10Y",)) == 2
        assert update_actuals(engine=engine, tenors=("10Y",)) == 2
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE api_wind_daily
                    SET indicators_value = 1.9
                    WHERE rdate = '2024-01-02'
                    """
                )
            )
        assert update_actuals(engine=engine, tenors=("10Y",)) == 2

        with engine.connect() as connection:
            stored = connection.execute(
                text(
                    "SELECT trade_date, close_yield, direction_1d "
                    "FROM t_scheme_actuals WHERE tenor = '10Y' "
                    "ORDER BY trade_date"
                )
            ).all()
        assert stored == [
            ("2024-01-01", 2.0, None),
            ("2024-01-02", 1.9, -1),
        ]
    finally:
        engine.dispose()
