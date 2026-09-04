"""整周无交易日时，相邻周六不得生成重复周频业务键。"""

from __future__ import annotations

import unittest
from bisect import bisect_left

from shared.prediction_context import is_weekly_signal_date

# 2026 春节：02-14..02-23 无交易日；02-13(五) 与 02-24(二) 是交易日。
TRADING_DAYS = [
    "2026-02-09",
    "2026-02-10",
    "2026-02-11",
    "2026-02-12",
    "2026-02-13",
    "2026-02-24",
    "2026-02-25",
    "2026-02-26",
    "2026-02-27",
]


class _StubCalendar:
    def __init__(self, trading: list[str]) -> None:
        self._days = sorted(trading)

    def covers(self, value: str) -> bool:
        return str(value)[:10] in {
            "2026-02-14",
            "2026-02-21",
            "2026-02-28",
        }

    def previous_trading_day(self, value: str) -> str:
        position = bisect_left(self._days, str(value)[:10])
        if position == 0:
            raise ValueError(f"no previous trading day before {value}")
        return self._days[position - 1]


class WeeklySignalDateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calendar = _StubCalendar(TRADING_DAYS)

    def test_weekly_signal_date_matrix(self) -> None:
        cases = (
            ("normal_week", "2026-02-14", True),
            ("zero_trading_week", "2026-02-21", False),
            ("post_holiday", "2026-02-28", True),
            ("friday_before_holiday", "2026-02-13", False),
            ("holiday_monday", "2026-02-16", False),
            ("friday_after_holiday", "2026-02-27", False),
            ("no_previous_trading_day", "2026-01-03", False),
        )
        for name, day, expected in cases:
            with self.subTest(case=name):
                self.assertEqual(
                    is_weekly_signal_date(self.calendar, day),
                    expected,
                )
