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

import os
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




class LaunchdWeeklySkipTests(unittest.TestCase):
    """非 signal 周六必须不执行，否则 UPSERT 会覆写首个周六的 predict_date。

    `t_scheme_predictions` 的 UPSERT 是
    ``ON DUPLICATE KEY UPDATE ... predict_date = VALUES(predict_date)``，唯一键
    是 ``(scheme_id, target_tenor, horizon, target_date)``。整周无交易日时两个
    周六解析出同一业务键，若后一个也执行，保留行的 predict_date 会被改成
    2026-02-21，而缺口报告仍按 2026-02-14 匹配，幽灵缺口依旧存在。
    """

    def _run(self, predict_date: str):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        from scheduler import launchd_prediction_runner as runner

        config = SimpleNamespace(
            scheme_id="weekly_demo",
            scheme_version="v1",
            frequency="weekly",
            status="active",
            runtime_type="blackbox_v2",
            input_source="data_bridge_current",
            version_status="active",
            task_type="weekly_point",
            horizon=1,
            tenors=["10Y"],
            legacy_mode="formal",
            capabilities=frozenset({"launchd_one_shot"}),
        )
        calendar = _StubCalendar(TRADING_DAYS)
        with (
            patch.dict(
                os.environ,
                {"BFL_DEPLOYMENT_TARGET": "mac3-production"},
                clear=False,
            ),
            patch.object(
                runner.DataBridgeRefreshConfig, "from_env", return_value=object()
            ),
            patch.object(runner, "_runner_lock", return_value=nullcontext()),
            patch.object(runner, "discover_schemes", return_value=[config]),
            patch.object(runner, "create_engine_from_env", return_value=Mock()),
            patch.object(runner, "get_calendar", return_value=calendar),
            patch.object(runner, "execute_scheme") as execute_one,
        ):
            return runner.run("weekly", predict_date=predict_date), execute_one

    def test_signal_saturday_executes(self) -> None:
        summary, execute_one = self._run("2026-02-14")
        self.assertNotEqual(summary.outcome, "not_applicable")
        execute_one.assert_called()

    def test_non_signal_saturday_is_not_applicable(self) -> None:
        summary, execute_one = self._run("2026-02-21")
        self.assertEqual(summary.outcome, "not_applicable")
        self.assertEqual(summary.exit_code, 0)
        execute_one.assert_not_called()
