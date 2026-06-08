from __future__ import annotations

import unittest

from sqlalchemy import create_engine, text


class CalendarServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE t_trade_calendar (rdate TEXT PRIMARY KEY, trade_flag TEXT)"))
            conn.execute(
                text("INSERT INTO t_trade_calendar (rdate, trade_flag) VALUES (:rdate, :trade_flag)"),
                [
                    {"rdate": "2026-06-04", "trade_flag": "1"},
                    {"rdate": "2026-06-05", "trade_flag": "1"},
                    {"rdate": "2026-06-06", "trade_flag": "0"},
                    {"rdate": "2026-06-07", "trade_flag": "0"},
                    {"rdate": "2026-06-08", "trade_flag": "1"},
                    {"rdate": "2026-06-09", "trade_flag": "1"},
                    {"rdate": "2026-06-10", "trade_flag": "1"},
                    {"rdate": "2026-06-11", "trade_flag": "1"},
                    {"rdate": "2026-06-12", "trade_flag": "1"},
                    {"rdate": "2026-05-25", "trade_flag": "1"},
                    {"rdate": "2026-05-26", "trade_flag": "1"},
                    {"rdate": "2026-05-27", "trade_flag": "1"},
                    {"rdate": "2026-05-28", "trade_flag": "1"},
                    {"rdate": "2026-05-29", "trade_flag": "0"},
                ],
            )
            for table_name in ("api_wind_weekly", "api_wind_derivative_weekly", "api_wind_daily"):
                conn.execute(text(f"CREATE TABLE {table_name} (id INTEGER PRIMARY KEY, rdate TEXT, week_id INTEGER)"))

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_nth_trading_day_after_uses_trade_calendar(self) -> None:
        from shared.calendar_service import get_calendar

        calendar = get_calendar(engine=self.engine)

        self.assertEqual(calendar.nth_trading_day_after("2026-06-04", 5), "2026-06-11")
        self.assertEqual(
            calendar.next_trading_days("2026-06-04", 5),
            ["2026-06-05", "2026-06-08", "2026-06-09", "2026-06-10", "2026-06-11"],
        )

    def test_week_id_for_date_falls_back_through_source_tables(self) -> None:
        from shared.calendar_service import get_calendar

        with self.engine.begin() as conn:
            conn.execute(
                text("INSERT INTO api_wind_derivative_weekly (id, rdate, week_id) VALUES (1, '2026-06-05', 202620)")
            )
            conn.execute(
                text("INSERT INTO api_wind_derivative_weekly (id, rdate, week_id) VALUES (2, '2026-06-05', 202621)")
            )
            conn.execute(text("INSERT INTO api_wind_daily (id, rdate, week_id) VALUES (1, '2026-06-04', 202620)"))
        calendar = get_calendar(engine=self.engine)

        self.assertEqual(calendar.week_id_for_date("2026-06-05"), 202621)
        self.assertEqual(calendar.week_id_for_date("2026-06-04"), 202620)
        self.assertIsNone(calendar.week_id_for_date("2026-06-03"))

    def test_week_id_to_last_trading_day_uses_calendar_before_pure_friday(self) -> None:
        from shared.calendar_service import get_calendar

        calendar = get_calendar(engine=self.engine)

        self.assertEqual(calendar.week_id_to_last_trading_day(202621), "2026-05-28")


if __name__ == "__main__":
    unittest.main()
