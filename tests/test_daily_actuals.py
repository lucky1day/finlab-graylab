from __future__ import annotations

import unittest

from sqlalchemy import create_engine, text


def _create_actuals_schema(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE api_wind_daily (
                    rdate TEXT,
                    indicators_code TEXT,
                    indicators_value REAL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_actuals (
                    tenor TEXT,
                    trade_date TEXT,
                    close_yield REAL,
                    direction_1d INTEGER,
                    direction_5d INTEGER
                )
                """
            )
        )


class DailyActualsWatermarkTests(unittest.TestCase):
    def test_read_source_watermarks_maps_indicator_codes_to_tenors(self) -> None:
        from scheduler.daily_actuals_updater import read_source_watermarks

        engine = create_engine("sqlite:///:memory:")
        _create_actuals_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO api_wind_daily (rdate, indicators_code, indicators_value)
                    VALUES
                        ('2026-07-01', 'TB1YWI0C', 1.10),
                        ('2026-07-03', 'TB1YWI0C', 1.12),
                        ('2026-07-02', 'TB5YWI0C', 1.45)
                    """
                )
            )
        try:
            watermarks = read_source_watermarks(engine, tenors=["1Y", "5Y"], end_date="2026-07-02")
        finally:
            engine.dispose()

        self.assertEqual(watermarks, {"1Y": "2026-07-01", "5Y": "2026-07-02"})

    def test_delete_actuals_after_source_watermark_prunes_only_tail_rows(self) -> None:
        from scheduler.repository import delete_actuals_after_source_watermark

        engine = create_engine("sqlite:///:memory:")
        _create_actuals_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_actuals
                        (tenor, trade_date, close_yield, direction_1d, direction_5d)
                    VALUES
                        ('1Y', '2026-07-01', 1.10, 1, 1),
                        ('1Y', '2026-07-02', 1.11, 1, 1),
                        ('5Y', '2026-07-02', 1.45, -1, -1)
                    """
                )
            )

        try:
            deleted = delete_actuals_after_source_watermark(
                engine,
                {"1Y": "2026-07-01", "5Y": "2026-07-02"},
                end_date="2026-07-02",
            )
            with engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT tenor, trade_date FROM t_scheme_actuals ORDER BY tenor, trade_date")
                ).all()
        finally:
            engine.dispose()

        self.assertEqual(deleted, 1)
        self.assertEqual(rows, [("1Y", "2026-07-01"), ("5Y", "2026-07-02")])


if __name__ == "__main__":
    unittest.main()
