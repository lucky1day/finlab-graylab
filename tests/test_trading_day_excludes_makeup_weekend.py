"""交易日判定在所有日历实现中排除调休补班周末。"""

from __future__ import annotations

from datetime import date, timedelta
import random
import unittest

from sqlalchemy import create_engine, text

from shared.actual_facts import (
    build_month_calendar,
    build_week_calendar,
)
from shared.calendar_service import get_calendar, is_trading_day_row

MAKEUP_SATURDAY = "2024-09-14"
CALENDAR_ROWS = [
    ("2024-09-09", "202436", "1"),
    ("2024-09-10", "202436", "1"),
    ("2024-09-11", "202436", "1"),
    ("2024-09-12", "202436", "1"),
    ("2024-09-13", "202436", "1"),
    (MAKEUP_SATURDAY, "202436", "1"),
    ("2024-09-15", "202436", "0"),
    ("2024-09-16", "202437", "0"),
    ("2024-09-17", "202437", "0"),
    ("2024-09-18", "202437", "1"),
    ("2024-09-19", "202437", "1"),
    ("2024-09-20", "202437", "1"),
    ("2024-09-21", "202437", "0"),
    ("2024-09-22", "202437", "0"),
]


def _calendar_dicts() -> list[dict]:
    return [
        {"rdate": rdate, "week_id": week_id, "trade_flag": flag}
        for rdate, week_id, flag in CALENDAR_ROWS
    ]


class TradingDayRuleTests(unittest.TestCase):
    """唯一谓词：工作日历 trade_flag='1' 且非周末。"""

    def test_trading_day_predicate_matrix(self) -> None:
        cases = (
            ("weekday_workday", "2024-09-13", "1", True),
            ("makeup_weekend", MAKEUP_SATURDAY, "1", False),
            ("weekday_holiday", "2024-09-16", "0", False),
            ("plain_weekend", "2024-09-21", "0", False),
        )
        for name, day, trade_flag, expected in cases:
            with self.subTest(case=name):
                self.assertEqual(
                    is_trading_day_row(day, trade_flag),
                    expected,
                )


class CalendarServiceTradingDayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE t_trade_calendar "
                    "(rdate TEXT PRIMARY KEY, trade_flag TEXT)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO t_trade_calendar (rdate, trade_flag) "
                    "VALUES (:rdate, :trade_flag)"
                ),
                [
                    {"rdate": rdate, "trade_flag": flag}
                    for rdate, _week_id, flag in CALENDAR_ROWS
                ],
            )
            conn.execute(
                text("CREATE TABLE api_wind_date (rdate TEXT PRIMARY KEY, week_id TEXT)")
            )
            conn.execute(
                text("INSERT INTO api_wind_date (rdate, week_id) VALUES (:rdate, :week_id)"),
                [
                    {"rdate": rdate, "week_id": week_id}
                    for rdate, week_id, _flag in CALENDAR_ROWS
                ],
            )
        self.calendar = get_calendar(engine=self.engine)
        self.addCleanup(self.engine.dispose)

    def test_is_trading_day_excludes_makeup_weekend(self) -> None:
        self.assertTrue(self.calendar.is_trading_day("2024-09-13"))
        self.assertFalse(self.calendar.is_trading_day(MAKEUP_SATURDAY))

    def test_previous_trading_day_skips_makeup_weekend(self) -> None:
        self.assertEqual(self.calendar.previous_trading_day("2024-09-15"), "2024-09-13")
        self.assertEqual(self.calendar.previous_trading_day("2024-09-18"), "2024-09-13")

    def test_next_trading_days_skips_makeup_weekend(self) -> None:
        self.assertEqual(
            self.calendar.next_trading_days("2024-09-12", 2),
            ["2024-09-13", "2024-09-18"],
        )

    def test_nth_trading_day_after_skips_makeup_weekend(self) -> None:
        self.assertEqual(
            self.calendar.nth_trading_day_after("2024-09-12", 2), "2024-09-18"
        )

    def test_week_last_trading_day_excludes_makeup_weekend(self) -> None:
        self.assertEqual(self.calendar.week_id_to_last_trading_day(202436), "2024-09-13")
        self.assertEqual(self.calendar.week_id_to_last_trading_day(202437), "2024-09-20")

    def test_date_outside_calendar_is_not_trading_day(self) -> None:
        """既有契约：日历未收录的日期返回 False，本次改动不得改变。"""
        self.assertFalse(self.calendar.is_trading_day("2030-01-02"))


class ActualFactsCalendarTests(unittest.TestCase):
    """actual 构造器的日历必须与 CalendarService 同口径。"""

    def test_month_calendar_excludes_makeup_weekend(self) -> None:
        calendar = build_month_calendar(_calendar_dicts())
        self.assertNotIn(MAKEUP_SATURDAY, calendar.trading_days)
        self.assertIn("2024-09-13", calendar.trading_days)

    def test_month_anchor_falls_back_to_weekday(self) -> None:
        """月度 15 日锚点：15 日非交易日时取之前最近的交易日。"""
        from datetime import date

        calendar = build_month_calendar(_calendar_dicts())
        self.assertEqual(
            calendar.last_trading_day_on_or_before(date(2024, 9, 15)),
            "2024-09-13",
        )

    def test_week_calendar_last_trading_day_excludes_makeup_weekend(self) -> None:
        calendar = build_week_calendar(_calendar_dicts())
        self.assertEqual(calendar.week_id_to_last_trading_day(202436), "2024-09-13")
        self.assertEqual(calendar.week_id_to_last_trading_day(202437), "2024-09-20")

    def test_week_predict_date_follows_workday_calendar(self) -> None:
        """predict_date 跟随工作日历，不随交易日定义漂移。"""
        calendar = build_week_calendar(_calendar_dicts())
        self.assertEqual(calendar.week_predict_date[202436], "2024-09-15")
        self.assertEqual(calendar.week_predict_date[202437], "2024-09-21")

    def test_indexed_calendar_lookups_match_linear_reference(self) -> None:
        generator = random.Random(20260830)
        first = date(2024, 1, 1)
        calendar_rows = []
        for offset in range(180):
            current = first + timedelta(days=offset)
            calendar_rows.append(
                {
                    "rdate": current.isoformat(),
                    "week_id": f"2024{offset // 7 + 1:02d}",
                    "trade_flag": (
                        "1"
                        if current.weekday() < 5 and generator.random() > 0.2
                        else "0"
                    ),
                }
            )

        week_calendar = build_week_calendar(calendar_rows)
        month_calendar = build_month_calendar(calendar_rows)
        trading_days = list(month_calendar.trading_days)
        for offset in range(-2, 183):
            value = (first + timedelta(days=offset)).isoformat()
            for count in (-1, 0, 1, 5, 500):
                expected_next = [
                    day for day in week_calendar.trading_days if day > value
                ][:count]
                self.assertEqual(
                    week_calendar.next_trading_days(value, count),
                    expected_next,
                )
            self.assertEqual(
                month_calendar.is_trading_day(value),
                value in trading_days,
            )
            candidates = [day for day in trading_days if day < value]
            if candidates:
                self.assertEqual(
                    month_calendar.previous_trading_day(value),
                    candidates[-1],
                )
            else:
                with self.assertRaisesRegex(ValueError, "no previous"):
                    month_calendar.previous_trading_day(value)
