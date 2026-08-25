"""日频健康检查以 scheduled_live 为权威，不被 gray_live 补缺改写。"""

from __future__ import annotations

import unittest

from sqlalchemy import create_engine, text

PREDICT_DATE = "2026-06-05"  # 周五，交易日
FEATURE_DATE = "2026-06-04"

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

_CALENDAR = [
    (f"2026-06-{day:02d}", "0" if day in (6, 7) else "1") for day in range(1, 13)
]


def _engine(runs: list[tuple], predictions: list[tuple]):
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
                "('demo_daily__h1__10Y','demo_daily','active','daily','T+1','10Y',1)"
            )
        )
        for run_id, status, phase, written in runs:
            conn.execute(
                text(
                    "INSERT INTO t_scheme_runs VALUES "
                    "(:i,'demo_daily',:pd,:s,:ph,:w)"
                ),
                {
                    "i": run_id,
                    "pd": PREDICT_DATE,
                    "s": status,
                    "ph": phase,
                    "w": written,
                },
            )
        for row_id, run_id, phase in predictions:
            conn.execute(
                text(
                    "INSERT INTO t_scheme_predictions VALUES "
                    "(:i,:r,'demo_daily','10Y',1,:pd,:fd,:pd,:ph)"
                ),
                {
                    "i": row_id,
                    "r": run_id,
                    "pd": PREDICT_DATE,
                    "fd": FEATURE_DATE,
                    "ph": phase,
                },
            )
    return engine


class PhaseIsolationTests(unittest.TestCase):
    def _snapshot(self, runs, predictions):
        from scripts.check_production_daily_health import load_snapshot

        engine = _engine(runs, predictions)
        self.addCleanup(engine.dispose)
        return load_snapshot(engine, predict_date=PREDICT_DATE, tenors=["10Y"])

    @staticmethod
    def _codes(snapshot) -> list[str]:
        from scripts.check_production_daily_health import evaluate_daily_health

        return [finding.code for finding in evaluate_daily_health(snapshot)]

    def test_gray_backfill_is_visible_without_masking_failed_scheduled_day(self) -> None:
        snapshot = self._snapshot(
            runs=[
                (1, "failed", "scheduled_live", None),
                (2, "success", "gray_live", 1),
            ],
            predictions=[(1, 2, "gray_live")],
        )
        self.assertEqual(snapshot.successful_daily_run_schemes, ())
        self.assertEqual(snapshot.predictions_count, 0)
        self.assertEqual(snapshot.run_prediction_counts, ())
        self.assertIn("daily_predictions_missing", self._codes(snapshot))
        self.assertEqual(snapshot.gray_live_run_schemes, ("demo_daily",))
        self.assertEqual(snapshot.gray_live_predictions_count, 1)

    def test_healthy_scheduled_day_is_unchanged(self) -> None:
        """正常 scheduled_live 成功日语义不变。"""
        snapshot = self._snapshot(
            runs=[(1, "success", "scheduled_live", 1)],
            predictions=[(1, 1, "scheduled_live")],
        )
        self.assertEqual(snapshot.successful_daily_run_schemes, ("demo_daily",))
        self.assertEqual(snapshot.predictions_count, 1)
        self.assertEqual(snapshot.gray_live_run_schemes, ())
        self.assertNotIn("daily_predictions_missing", self._codes(snapshot))

    def test_mixed_day_counts_only_scheduled_rows(self) -> None:
        """正式成功后又有 gray 补缺：核对口径只取 scheduled 行。"""
        snapshot = self._snapshot(
            runs=[
                (1, "success", "scheduled_live", 1),
                (2, "success", "gray_live", 1),
            ],
            predictions=[(1, 1, "scheduled_live"), (2, 2, "gray_live")],
        )
        self.assertEqual(snapshot.predictions_count, 1)
        self.assertEqual(len(snapshot.run_prediction_counts), 1)
        self.assertEqual(snapshot.run_prediction_counts[0].run_id, 1)
        self.assertEqual(snapshot.gray_live_predictions_count, 1)

    def test_null_phase_run_is_not_scheduled_evidence(self) -> None:
        """migration 010 之前的 NULL phase 行不得充当当日调度健康证据。"""
        snapshot = self._snapshot(
            runs=[(1, "success", None, 1)],
            predictions=[(1, 1, None)],
        )
        self.assertEqual(snapshot.successful_daily_run_schemes, ())
        self.assertEqual(snapshot.predictions_count, 0)
        self.assertEqual(snapshot.gray_live_run_schemes, ())
