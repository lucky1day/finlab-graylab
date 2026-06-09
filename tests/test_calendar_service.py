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
                    {"rdate": "2026-05-25", "trade_flag": "1"},
                    {"rdate": "2026-05-26", "trade_flag": "1"},
                    {"rdate": "2026-05-27", "trade_flag": "1"},
                    {"rdate": "2026-05-28", "trade_flag": "1"},
                    {"rdate": "2026-05-29", "trade_flag": "1"},
                    {"rdate": "2026-05-30", "trade_flag": "0"},
                    {"rdate": "2026-05-31", "trade_flag": "0"},
                    {"rdate": "2026-06-01", "trade_flag": "1"},
                    {"rdate": "2026-06-02", "trade_flag": "1"},
                    {"rdate": "2026-06-03", "trade_flag": "1"},
                    {"rdate": "2026-06-04", "trade_flag": "1"},
                    {"rdate": "2026-06-05", "trade_flag": "1"},
                    {"rdate": "2026-06-06", "trade_flag": "0"},
                    {"rdate": "2026-06-07", "trade_flag": "0"},
                    {"rdate": "2026-06-08", "trade_flag": "1"},
                    {"rdate": "2026-06-09", "trade_flag": "1"},
                    {"rdate": "2026-06-10", "trade_flag": "1"},
                    {"rdate": "2026-06-11", "trade_flag": "1"},
                    {"rdate": "2026-06-12", "trade_flag": "1"},
                ],
            )
            conn.execute(text("CREATE TABLE api_wind_date (rdate TEXT PRIMARY KEY, week_id TEXT)"))
            conn.execute(
                text("INSERT INTO api_wind_date (rdate, week_id) VALUES (:rdate, :week_id)"),
                [
                    {"rdate": "2026-05-25", "week_id": "202620"},
                    {"rdate": "2026-05-26", "week_id": "202620"},
                    {"rdate": "2026-05-27", "week_id": "202620"},
                    {"rdate": "2026-05-28", "week_id": "202620"},
                    {"rdate": "2026-05-29", "week_id": "202620"},
                    {"rdate": "2026-05-30", "week_id": "202620"},
                    {"rdate": "2026-05-31", "week_id": "202620"},
                    {"rdate": "2026-06-01", "week_id": "202621"},
                    {"rdate": "2026-06-02", "week_id": "202621"},
                    {"rdate": "2026-06-03", "week_id": "202621"},
                    {"rdate": "2026-06-04", "week_id": "202621"},
                    {"rdate": "2026-06-05", "week_id": "202621"},
                    {"rdate": "2026-06-06", "week_id": "202621"},
                    {"rdate": "2026-06-07", "week_id": "202621"},
                    {"rdate": "2026-06-08", "week_id": "202622"},
                    {"rdate": "2026-06-09", "week_id": "202622"},
                    {"rdate": "2026-06-10", "week_id": "202622"},
                    {"rdate": "2026-06-11", "week_id": "202622"},
                    {"rdate": "2026-06-12", "week_id": "202622"},
                ],
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_nth_trading_day_after_uses_trade_calendar(self) -> None:
        from shared.calendar_service import get_calendar
        from scheduler.calendar import is_trading_day as scheduler_is_trading_day

        calendar = get_calendar(engine=self.engine)

        self.assertTrue(calendar.is_trading_day("2026-06-05"))
        self.assertFalse(calendar.is_trading_day("2026-06-06"))
        self.assertFalse(calendar.is_trading_day("2026-06-15"))
        self.assertFalse(scheduler_is_trading_day(self.engine, "2026-06-15"))
        self.assertEqual(calendar.nth_trading_day_after("2026-06-04", 5), "2026-06-11")
        self.assertEqual(
            calendar.next_trading_days("2026-06-04", 5),
            ["2026-06-05", "2026-06-08", "2026-06-09", "2026-06-10", "2026-06-11"],
        )

    def test_week_id_for_date_uses_api_wind_date_only(self) -> None:
        from shared.calendar_service import get_calendar

        calendar = get_calendar(engine=self.engine)

        self.assertEqual(calendar.week_id_for_date("2026-06-05"), 202621)
        self.assertEqual(calendar.week_id_for_date("2026-06-08"), 202622)
        self.assertIsNone(calendar.week_id_for_date("2026-06-15"))

    def test_week_id_to_last_trading_day_joins_api_wind_date_to_trade_calendar(self) -> None:
        from shared.calendar_service import get_calendar

        calendar = get_calendar(engine=self.engine)

        self.assertEqual(calendar.week_id_to_last_trading_day(202621), "2026-06-05")
        self.assertEqual(calendar.week_id_to_last_trading_day(202622), "2026-06-12")


if __name__ == "__main__":
    unittest.main()
