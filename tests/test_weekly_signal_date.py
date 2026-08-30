"""整周无交易日时，相邻周六不得生成重复周频业务键。"""

from __future__ import annotations

import os
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
    """非 signal 周六不得执行或覆写已有周频业务键。"""

    def _run(self, predict_date: str):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        from scheduler import launchd_prediction_runner as launchd_runner
        from scheduler import one_shot_prediction_runner as runner

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
            patch.object(
                runner,
                "resolve_database_lifecycle",
                return_value=(config,),
            ),
            patch.object(runner, "get_calendar", return_value=calendar),
            patch.object(runner, "execute_scheme") as execute_one,
        ):
            return (
                launchd_runner.run("weekly", predict_date=predict_date),
                execute_one,
            )

    def test_signal_saturday_executes(self) -> None:
        summary, execute_one = self._run("2026-02-14")
        self.assertNotEqual(summary.outcome, "not_applicable")
        execute_one.assert_called()

    def test_non_signal_saturday_is_not_applicable(self) -> None:
        summary, execute_one = self._run("2026-02-21")
        self.assertEqual(summary.outcome, "not_applicable")
        self.assertEqual(summary.exit_code, 0)
        execute_one.assert_not_called()
