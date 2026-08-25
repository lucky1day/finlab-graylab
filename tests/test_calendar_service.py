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

        calendar = get_calendar(engine=self.engine)

        self.assertTrue(calendar.is_trading_day("2026-06-05"))
        self.assertFalse(calendar.is_trading_day("2026-06-06"))
        self.assertFalse(calendar.is_trading_day("2026-06-15"))
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

    def test_week_id_to_last_trading_day_does_not_fallback_to_non_trading_day(self) -> None:
        from shared.calendar_service import get_calendar

        with self.engine.begin() as conn:
            conn.execute(
                text("INSERT INTO api_wind_date (rdate, week_id) VALUES (:rdate, :week_id)"),
                [
                    {"rdate": "2026-06-13", "week_id": "202623"},
                    {"rdate": "2026-06-14", "week_id": "202623"},
                ],
            )
            conn.execute(
                text("INSERT INTO t_trade_calendar (rdate, trade_flag) VALUES (:rdate, :trade_flag)"),
                [
                    {"rdate": "2026-06-13", "trade_flag": "0"},
                    {"rdate": "2026-06-14", "trade_flag": "0"},
                ],
            )

        calendar = get_calendar(engine=self.engine)

        with self.assertRaisesRegex(ValueError, "no trading day found"):
            calendar.week_id_to_last_trading_day(202623)

    def test_calendar_snapshot_export_reuses_caller_transaction(
        self,
    ) -> None:
        from shared.calendar_service import (
            read_calendar_snapshot_from_connection,
        )

        with self.engine.connect() as connection:
            frames = read_calendar_snapshot_from_connection(connection)

        self.assertEqual(
            list(frames),
            ["api_wind_date.csv", "t_trade_calendar.csv"],
        )
        self.assertEqual(
            list(frames["api_wind_date.csv"].columns),
            ["rdate", "week_id"],
        )
        self.assertEqual(
            list(frames["t_trade_calendar.csv"].columns),
            ["rdate", "trade_flag"],
        )
        self.assertEqual(
            frames["api_wind_date.csv"].iloc[0]["rdate"],
            "2026-05-25",
        )

    def test_isolated_week_id_jump_is_normalized(self) -> None:
        from shared.calendar_service import get_calendar

        with self.engine.begin() as conn:
            conn.execute(
                text("INSERT INTO t_trade_calendar (rdate, trade_flag) VALUES (:rdate, :trade_flag)"),
                [
                    {"rdate": "2026-06-29", "trade_flag": "1"},
                    {"rdate": "2026-06-30", "trade_flag": "1"},
                    {"rdate": "2026-07-01", "trade_flag": "1"},
                    {"rdate": "2026-07-02", "trade_flag": "1"},
                    {"rdate": "2026-07-03", "trade_flag": "1"},
                    {"rdate": "2026-07-04", "trade_flag": "0"},
                    {"rdate": "2026-07-05", "trade_flag": "0"},
                    {"rdate": "2026-07-06", "trade_flag": "1"},
                    {"rdate": "2026-07-07", "trade_flag": "1"},
                    {"rdate": "2026-07-08", "trade_flag": "1"},
                    {"rdate": "2026-07-09", "trade_flag": "1"},
                    {"rdate": "2026-07-10", "trade_flag": "1"},
                ],
            )
            conn.execute(
                text("INSERT INTO api_wind_date (rdate, week_id) VALUES (:rdate, :week_id)"),
                [
                    {"rdate": "2026-06-29", "week_id": "202625"},
                    {"rdate": "2026-06-30", "week_id": "202625"},
                    {"rdate": "2026-07-01", "week_id": "202625"},
                    {"rdate": "2026-07-02", "week_id": "202625"},
                    {"rdate": "2026-07-03", "week_id": "202626"},
                    {"rdate": "2026-07-04", "week_id": "202625"},
                    {"rdate": "2026-07-05", "week_id": "202625"},
                    {"rdate": "2026-07-06", "week_id": "202626"},
                    {"rdate": "2026-07-07", "week_id": "202626"},
                    {"rdate": "2026-07-08", "week_id": "202626"},
                    {"rdate": "2026-07-09", "week_id": "202626"},
                    {"rdate": "2026-07-10", "week_id": "202626"},
                ],
            )

        calendar = get_calendar(engine=self.engine)

        self.assertEqual(calendar.week_id_for_date("2026-07-03"), 202625)
        self.assertEqual(calendar.week_id_to_last_trading_day(202625), "2026-07-03")
        self.assertEqual(calendar.week_id_to_last_trading_day(202626), "2026-07-10")
