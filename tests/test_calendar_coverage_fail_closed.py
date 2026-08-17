"""日历覆盖耗尽必须 fail-closed，不得伪装成节假日。

`CalendarService.is_trading_day` 对日历未收录的日期返回 False，这是既定契约。
但 launchd 预测入口据此把「日历没续期」与「今天是节假日」判成同一件事，
返回 ``outcome='not_applicable'`` 且退出码 0——整批生产静默不产出，且与真实
节假日无法区分。

`t_trade_calendar` 需要人工逐年延长，因此覆盖耗尽是可预期的运维事件，必须
以配置错误暴露，而不是静默成功。
"""

from __future__ import annotations

import os
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.calendar_service import get_calendar  # noqa: E402

# 覆盖 2026-06-01..2026-06-05（工作日）与随后的周末；之后无任何日历行。
CALENDAR_ROWS = [
    ("2026-06-01", "1"),
    ("2026-06-02", "1"),
    ("2026-06-03", "0"),  # 假期中的工作日
    ("2026-06-04", "1"),
    ("2026-06-05", "1"),
    ("2026-06-06", "0"),
    ("2026-06-07", "0"),
]
COVERED_TRADING = "2026-06-04"
COVERED_HOLIDAY = "2026-06-03"
UNCOVERED = "2026-07-01"


class CalendarCoverageTests(unittest.TestCase):
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
                [{"rdate": r, "trade_flag": f} for r, f in CALENDAR_ROWS],
            )
        self.calendar = get_calendar(engine=self.engine)
        self.addCleanup(self.engine.dispose)

    def test_covers_reports_calendar_membership(self) -> None:
        self.assertTrue(self.calendar.covers(COVERED_TRADING))
        self.assertTrue(self.calendar.covers(COVERED_HOLIDAY))
        self.assertFalse(self.calendar.covers(UNCOVERED))

    def test_covers_is_independent_of_trade_flag(self) -> None:
        """假期与周末仍在日历覆盖内；covers 只回答『日历知不知道这天』。"""
        self.assertFalse(self.calendar.is_trading_day(COVERED_HOLIDAY))
        self.assertTrue(self.calendar.covers(COVERED_HOLIDAY))

    def test_is_trading_day_contract_unchanged(self) -> None:
        """既有契约不变：未收录日期仍返回 False。"""
        self.assertFalse(self.calendar.is_trading_day(UNCOVERED))


def _config(scheme_id: str, frequency: str) -> SimpleNamespace:
    return SimpleNamespace(
        scheme_id=scheme_id,
        scheme_version=f"{scheme_id}-v",
        frequency=frequency,
        status="active",
        runtime_type="blackbox_v2",
        input_source="data_bridge_current",
        version_status="active",
        task_type="T+1" if frequency == "daily" else "weekly_point",
        horizon=1,
        tenors=["10Y"],
        legacy_mode="formal",
        capabilities=frozenset({"launchd_one_shot"}),
    )


class _StubCalendar:
    """覆盖区间受控的日历替身。"""

    def __init__(self, covered: set[str], trading: set[str]) -> None:
        self._covered = covered
        self._trading = trading

    def covers(self, value) -> bool:
        return str(value)[:10] in self._covered

    def is_trading_day(self, value) -> bool:
        return str(value)[:10] in self._trading


class LaunchdRunnerCoverageTests(unittest.TestCase):
    def _run(self, cadence: str, predict_date: str, calendar: _StubCalendar):
        from scheduler import launchd_prediction_runner as runner

        config = _config("demo", "daily" if cadence == "daily" else cadence)
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
            return runner.run(cadence, predict_date=predict_date), execute_one

    def test_uncovered_date_fails_closed_for_daily(self) -> None:
        from scheduler.launchd_prediction_runner import (
            LaunchdPredictionConfigurationError,
        )

        calendar = _StubCalendar(covered={COVERED_TRADING}, trading={COVERED_TRADING})
        with self.assertRaises(LaunchdPredictionConfigurationError):
            self._run("daily", UNCOVERED, calendar)

    def test_uncovered_date_fails_closed_for_weekly(self) -> None:
        """周频原本连交易日判定都没有，覆盖耗尽时会静默用陈旧日期。"""
        from scheduler.launchd_prediction_runner import (
            LaunchdPredictionConfigurationError,
        )

        calendar = _StubCalendar(covered={COVERED_TRADING}, trading={COVERED_TRADING})
        with self.assertRaises(LaunchdPredictionConfigurationError):
            self._run("weekly", UNCOVERED, calendar)

    def test_covered_holiday_still_reports_not_applicable(self) -> None:
        """真实节假日行为不变：not_applicable + 退出码 0。"""
        calendar = _StubCalendar(
            covered={COVERED_TRADING, COVERED_HOLIDAY}, trading={COVERED_TRADING}
        )
        summary, execute_one = self._run("daily", COVERED_HOLIDAY, calendar)
        self.assertEqual(summary.outcome, "not_applicable")
        self.assertEqual(summary.exit_code, 0)
        execute_one.assert_not_called()

    def test_covered_trading_day_is_not_short_circuited(self) -> None:
        """覆盖内的交易日不得被覆盖检查或节假日检查提前截断。"""
        calendar = _StubCalendar(
            covered={COVERED_TRADING, COVERED_HOLIDAY}, trading={COVERED_TRADING}
        )
        summary, _execute_one = self._run("daily", COVERED_TRADING, calendar)
        self.assertNotEqual(summary.outcome, "not_applicable")
        self.assertNotEqual(summary.outcome, "configuration_error")


if __name__ == "__main__":
    unittest.main()


class MonthlyAnchorCoverageTests(unittest.TestCase):
    """月频目标锚点必须落在声明的目标月内。

    日历在当前月 15 日仍覆盖、但下月锚点未覆盖时，`previous_trading_day`
    会静默回退到日历末端，产出 `target_month_id` 与 `target_date` 不属同一月
    的组合（例：target_month=2027-01 而 target_date=2026-12-31）。
    """

    class _EndOfCalendar:
        """只覆盖到 2026-12-31 的日历替身。"""

        TRADING = ("2026-12-14", "2026-12-15", "2026-12-30", "2026-12-31")
        COVERED = TRADING

        def covers(self, value) -> bool:
            return str(value)[:10] in self.COVERED

        def is_trading_day(self, value) -> bool:
            return str(value)[:10] in self.TRADING

        def previous_trading_day(self, value) -> str:
            earlier = [d for d in self.TRADING if d < str(value)[:10]]
            if not earlier:
                raise ValueError(f"no previous trading day before {value}")
            return earlier[-1]

    def test_target_anchor_beyond_calendar_fails_closed(self) -> None:
        from shared.prediction_context import build_monthly_live_context

        with self.assertRaises(ValueError) as caught:
            build_monthly_live_context(self._EndOfCalendar(), "2026-12-15")
        self.assertIn("2027-01", str(caught.exception))

    def test_covered_month_still_resolves(self) -> None:
        """目标月被覆盖时行为不变。"""
        from shared.prediction_context import build_monthly_live_context

        class _Covered(self._EndOfCalendar):
            TRADING = ("2026-11-13", "2026-12-14", "2026-12-15")
            COVERED = (*TRADING, "2026-11-15")

        ctx = build_monthly_live_context(_Covered(), "2026-11-15")
        self.assertEqual(ctx.feature_date, "2026-11-13")
        self.assertEqual(ctx.target_month_id, "2026-12")
        self.assertEqual(ctx.target_date, "2026-12-15")

    def test_partially_covered_target_month_fails_closed(self) -> None:
        """次月已有交易日但未覆盖自然 15 日时，不能猜测 14 日是最终锚点。"""
        from shared.prediction_context import build_monthly_live_context

        class _CoveredOnlyThroughTargetFourteenth(self._EndOfCalendar):
            TRADING = ("2026-12-14", "2026-12-15", "2027-01-14")
            COVERED = TRADING

        with self.assertRaisesRegex(ValueError, "target anchor 2027-01-15"):
            build_monthly_live_context(
                _CoveredOnlyThroughTargetFourteenth(),
                "2026-12-15",
            )


class ActualsRunnerCoverageTests(unittest.TestCase):
    """launchd actuals 入口同样不得把日历耗尽当成非交易日。

    `run_actuals_job` 在非交易日会把 daily/weekly 的 end_date 回退到上一个交易
    日。日历未覆盖时 `is_trading_day` 也返回 False，于是覆盖耗尽会伪装成节假
    日：每天都用日历最后一个交易日重刷同一批 actuals，日志是 INFO、退出码 0，
    运维看不出 actuals 早已停止推进。
    """

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
                [{"rdate": r, "trade_flag": f} for r, f in CALENDAR_ROWS],
            )
        self.calendar = get_calendar(engine=self.engine)
        self.addCleanup(self.engine.dispose)

    def _patches(self):
        from unittest.mock import patch as _patch

        return (
            _patch(
                "scheduler.actuals_runner.create_engine_from_env",
                return_value=Mock(),
            ),
            _patch(
                "scheduler.actuals_runner.get_calendar",
                return_value=self.calendar,
            ),
            _patch("scheduler.actuals_runner.update_actuals", return_value=0),
            _patch(
                "scheduler.actuals_runner.update_weekly_actuals", return_value=0
            ),
            _patch(
                "scheduler.actuals_runner.update_monthly_actuals", return_value=0
            ),
        )

    def test_uncovered_run_date_fails_closed_without_writing(self) -> None:
        from scheduler.actuals_runner import run_actuals_job

        engine_p, calendar_p, daily_p, weekly_p, monthly_p = self._patches()
        with engine_p, calendar_p, daily_p as daily, weekly_p as weekly, (
            monthly_p
        ) as monthly:
            with self.assertRaises(ValueError):
                run_actuals_job(run_date=UNCOVERED)
            daily.assert_not_called()
            weekly.assert_not_called()
            monthly.assert_not_called()

    def test_covered_holiday_still_rolls_back_to_previous_trading_day(self) -> None:
        """真实节假日的既有语义不变——修复不得把节假日也一起挡掉。"""
        from scheduler.actuals_runner import run_actuals_job

        engine_p, calendar_p, daily_p, weekly_p, monthly_p = self._patches()
        with engine_p, calendar_p, daily_p as daily, weekly_p as weekly, (
            monthly_p
        ) as monthly:
            run_actuals_job(run_date=COVERED_HOLIDAY)
            self.assertEqual(daily.call_args.kwargs["end_date"], "2026-06-02")
            self.assertEqual(weekly.call_args.kwargs["end_date"], "2026-06-02")
            self.assertEqual(
                monthly.call_args.kwargs["end_date"], COVERED_HOLIDAY
            )

    def test_covered_trading_day_uses_run_date(self) -> None:
        from scheduler.actuals_runner import run_actuals_job

        engine_p, calendar_p, daily_p, weekly_p, monthly_p = self._patches()
        with engine_p, calendar_p, daily_p as daily, weekly_p as weekly, (
            monthly_p
        ) as monthly:
            run_actuals_job(run_date=COVERED_TRADING)
            self.assertEqual(
                daily.call_args.kwargs["end_date"], COVERED_TRADING
            )
            self.assertEqual(
                monthly.call_args.kwargs["end_date"], COVERED_TRADING
            )
