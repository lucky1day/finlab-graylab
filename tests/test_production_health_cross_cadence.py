"""生产健康检查必须能对 weekly / monthly 做同等级的应出/实出对账。

健康脚本此前把 `frequency='daily'` 与 `task_type IN ('T+1','T+5')` 写死，weekly
与 monthly 的整批缺失没有任何自动对账入口——进程退出、日志存在或业务表后来被
补齐，都不能单独证明正式批次按时完整执行。

适用性判定同样不能沿用日频：weekly 在周六触发，`is_trading_day` 对它恒为假，
直接套用会让周频健康检查永远提前返回、什么都不检查。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_DDL = [
    "CREATE TABLE t_trade_calendar (rdate TEXT PRIMARY KEY, trade_flag TEXT)",
    """CREATE TABLE t_scheme_registry (
        scheme_id TEXT, base_scheme_id TEXT, status TEXT, frequency TEXT,
        task_type TEXT, target_tenor TEXT, horizon INTEGER)""",
    """CREATE TABLE t_scheme_runs (
        run_id INTEGER PRIMARY KEY, scheme_id TEXT, predict_date TEXT,
        status TEXT, prediction_phase TEXT, records_written INTEGER)""",
    """CREATE TABLE t_scheme_predictions (
        id INTEGER PRIMARY KEY, run_id INTEGER, scheme_id TEXT,
        target_tenor TEXT, horizon INTEGER, predict_date TEXT,
        feature_date TEXT, target_date TEXT, prediction_phase TEXT)""",
    "CREATE TABLE t_scheme_actuals (tenor TEXT, trade_date TEXT)",
    "CREATE TABLE api_wind_daily (indicators_code TEXT, indicators_value REAL, rdate TEXT)",
]

# 2026-06：1 日周一…6/7 周末，13/14 周末，15 周一。
_CALENDAR = [
    (f"2026-06-{day:02d}", "0" if day % 7 in (0, 6) else "1") for day in range(1, 31)
]

REGISTRY_ROWS = [
    ("demo_daily__h1__10Y", "demo_daily", "active", "daily", "T+1", "10Y", 1),
    ("demo_weekly__h1__10Y", "demo_weekly", "active", "weekly", "weekly_point", "10Y", 1),
    ("demo_monthly__h1__10Y", "demo_monthly", "active", "monthly", "monthly", "10Y", 1),
]


def _engine(runs=(), predictions=()):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as conn:
        for ddl in _DDL:
            conn.execute(text(ddl))
        conn.execute(
            text("INSERT INTO t_trade_calendar VALUES (:r, :f)"),
            [{"r": r, "f": f} for r, f in _CALENDAR],
        )
        conn.execute(
            text(
                "INSERT INTO t_scheme_registry VALUES "
                "(:sid, :base, :st, :fr, :tt, :tenor, :h)"
            ),
            [
                {
                    "sid": s,
                    "base": b,
                    "st": st,
                    "fr": fr,
                    "tt": tt,
                    "tenor": tenor,
                    "h": h,
                }
                for s, b, st, fr, tt, tenor, h in REGISTRY_ROWS
            ],
        )
        for run_id, scheme, pd_, status, phase in runs:
            conn.execute(
                text(
                    "INSERT INTO t_scheme_runs VALUES (:i,:s,:p,:st,:ph,1)"
                ),
                {"i": run_id, "s": scheme, "p": pd_, "st": status, "ph": phase},
            )
        for row_id, run_id, scheme, pd_, phase in predictions:
            conn.execute(
                text(
                    "INSERT INTO t_scheme_predictions VALUES "
                    "(:i,:r,:s,'10Y',1,:p,:p,:p,:ph)"
                ),
                {"i": row_id, "r": run_id, "s": scheme, "p": pd_, "ph": phase},
            )
    return engine


class CrossCadenceScopeTests(unittest.TestCase):
    def _snapshot(self, *, cadence, predict_date, runs=(), predictions=()):
        from scripts.check_production_daily_health import load_snapshot

        engine = _engine(runs, predictions)
        self.addCleanup(engine.dispose)
        return load_snapshot(
            engine,
            predict_date=predict_date,
            tenors=["10Y"],
            cadence=cadence,
        )

    def test_daily_scope_excludes_other_cadences(self) -> None:
        snapshot = self._snapshot(cadence="daily", predict_date="2026-06-05")
        self.assertEqual(snapshot.active_base_schemes, ("demo_daily",))

    def test_weekly_scope_selects_weekly_registry_rows(self) -> None:
        snapshot = self._snapshot(cadence="weekly", predict_date="2026-06-06")
        self.assertEqual(snapshot.active_base_schemes, ("demo_weekly",))

    def test_monthly_scope_selects_monthly_registry_rows(self) -> None:
        snapshot = self._snapshot(cadence="monthly", predict_date="2026-06-15")
        self.assertEqual(snapshot.active_base_schemes, ("demo_monthly",))

    def test_weekly_missing_batch_is_reported(self) -> None:
        """周频整批缺失必须被对账出来——这是本 issue 的核心缺口。"""
        from scripts.check_production_daily_health import evaluate_daily_health

        snapshot = self._snapshot(cadence="weekly", predict_date="2026-06-06")
        codes = {item.code for item in evaluate_daily_health(snapshot)}
        self.assertIn("daily_predictions_missing", codes)

    def test_weekly_saturday_is_due_even_though_not_a_trading_day(self) -> None:
        """周六不是交易日；沿用日频门会让周频永远跳过检查。"""
        snapshot = self._snapshot(cadence="weekly", predict_date="2026-06-06")
        self.assertFalse(snapshot.is_trading_day)
        self.assertTrue(snapshot.is_due)

    def test_weekly_non_signal_day_is_not_due(self) -> None:
        snapshot = self._snapshot(cadence="weekly", predict_date="2026-06-05")
        self.assertFalse(snapshot.is_due)
        self.assertEqual(evaluate_codes(snapshot), set())

    def test_monthly_non_fifteenth_is_not_due(self) -> None:
        snapshot = self._snapshot(cadence="monthly", predict_date="2026-06-16")
        self.assertFalse(snapshot.is_due)

    def test_daily_due_still_equals_trading_day(self) -> None:
        due = self._snapshot(cadence="daily", predict_date="2026-06-05")
        self.assertTrue(due.is_due)
        self.assertEqual(due.is_due, due.is_trading_day)
        off = self._snapshot(cadence="daily", predict_date="2026-06-06")
        self.assertFalse(off.is_due)
        self.assertEqual(off.is_due, off.is_trading_day)

    def test_weekly_scheduled_success_clears_findings(self) -> None:
        snapshot = self._snapshot(
            cadence="weekly",
            predict_date="2026-06-06",
            runs=[(1, "demo_weekly", "2026-06-06", "success", "scheduled_live")],
            predictions=[(1, 1, "demo_weekly", "2026-06-06", "scheduled_live")],
        )
        self.assertEqual(snapshot.successful_run_schemes, ("demo_weekly",))
        self.assertEqual(snapshot.predictions_count, 1)
        self.assertNotIn("daily_predictions_missing", evaluate_codes(snapshot))

    def test_gray_backfill_does_not_mask_weekly_failure(self) -> None:
        """#37 的 phase 分离必须同样适用于 weekly。"""
        snapshot = self._snapshot(
            cadence="weekly",
            predict_date="2026-06-06",
            runs=[
                (1, "demo_weekly", "2026-06-06", "failed", "scheduled_live"),
                (2, "demo_weekly", "2026-06-06", "success", "gray_live"),
            ],
            predictions=[(1, 2, "demo_weekly", "2026-06-06", "gray_live")],
        )
        self.assertEqual(snapshot.successful_run_schemes, ())
        self.assertEqual(snapshot.gray_live_run_schemes, ("demo_weekly",))
        self.assertIn("daily_predictions_missing", evaluate_codes(snapshot))

    def test_unknown_cadence_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            self._snapshot(cadence="hourly", predict_date="2026-06-05")


def evaluate_codes(snapshot) -> set[str]:
    from scripts.check_production_daily_health import evaluate_daily_health

    return {item.code for item in evaluate_daily_health(snapshot)}


if __name__ == "__main__":
    unittest.main()
