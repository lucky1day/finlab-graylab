"""整周无交易日时，后一个周六不是周频信号日。

周频由自然周六触发。但长假整周无交易日时，相邻两个周六的
``previous_trading_day`` 相同，会推导出同一 ``feature_date`` /
``target_date``——而 ``t_scheme_predictions`` 的唯一键正是该业务键，一条预测
只能存在一次。

若把两个周六都算成到期：缺口报告里先到的那个永远匹配不到行，registry 在前端
永久显示 missing 且任何补数都消不掉。

真实日历实测：2026-02-14..2026-02-23 整段无交易日，两个周六 2026-02-14 与
2026-02-21 都解析出 feature_date=2026-02-13 / target_date=2026-02-27。
"""

from __future__ import annotations

import sys
import unittest
from bisect import bisect_left
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.prediction_context import is_weekly_signal_date  # noqa: E402

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

    def previous_trading_day(self, value: str) -> str:
        position = bisect_left(self._days, str(value)[:10])
        if position == 0:
            raise ValueError(f"no previous trading day before {value}")
        return self._days[position - 1]


class WeeklySignalDateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calendar = _StubCalendar(TRADING_DAYS)

    def test_saturday_after_normal_week_is_signal_date(self) -> None:
        """刚结束的一周有交易日：是信号日。"""
        self.assertTrue(is_weekly_signal_date(self.calendar, "2026-02-14"))

    def test_saturday_after_zero_trading_week_is_not_signal_date(self) -> None:
        """整周无交易日：该周六不关闭新的 feature 周，不是信号日。"""
        self.assertFalse(is_weekly_signal_date(self.calendar, "2026-02-21"))

    def test_next_saturday_resumes_after_holiday(self) -> None:
        """假期结束后的周六恢复为信号日。"""
        self.assertTrue(is_weekly_signal_date(self.calendar, "2026-02-28"))

    def test_non_saturday_is_never_signal_date(self) -> None:
        for day in ("2026-02-13", "2026-02-16", "2026-02-27"):
            with self.subTest(day=day):
                self.assertFalse(is_weekly_signal_date(self.calendar, day))

    def test_no_previous_trading_day_is_not_signal_date(self) -> None:
        """日历起点之前没有交易日时 fail-safe 为非信号日，不抛异常。"""
        self.assertFalse(is_weekly_signal_date(self.calendar, "2026-01-03"))

    def test_两个周六不再解析出同一业务键(self) -> None:
        """回归点：两个周六的 previous_trading_day 相同，只能有一个到期。"""
        first = self.calendar.previous_trading_day("2026-02-14")
        second = self.calendar.previous_trading_day("2026-02-21")
        self.assertEqual(first, second)
        due = [
            day
            for day in ("2026-02-14", "2026-02-21")
            if is_weekly_signal_date(self.calendar, day)
        ]
        self.assertEqual(due, ["2026-02-14"])


if __name__ == "__main__":
    unittest.main()
