from __future__ import annotations

import unittest

from sqlalchemy import create_engine, text


class DeleteBacktestRunsTests(unittest.TestCase):
    def test_dry_run_counts_target_rows_without_deleting(self) -> None:
        from scripts import delete_backtest_runs

        engine = _engine_with_backtest_run()
        summary = delete_backtest_runs.delete_backtest_run(
            engine,
            scheme_id="weekly_10y_d_overlay_0529",
            run_id=106,
            benchmark_id="model_muti_0529",
            data_source="framework_db_aligned",
            apply=False,
        )

        self.assertFalse(summary["applied"])
        self.assertEqual(summary["run"]["scheme_id"], "weekly_10y_d_overlay_0529")
        self.assertEqual(summary["counts"]["t_backtest_monthly_metrics"], 1)
        self.assertEqual(summary["counts"]["t_backtest_predictions"], 2)
        self.assertEqual(summary["counts"]["t_backtest_runs"], 1)
        self.assertEqual(_count(engine, "t_backtest_runs"), 2)
        self.assertEqual(_count(engine, "t_backtest_predictions"), 3)

    def test_apply_deletes_only_requested_backtest_run(self) -> None:
        from scripts import delete_backtest_runs

        engine = _engine_with_backtest_run()
        summary = delete_backtest_runs.delete_backtest_run(
            engine,
            scheme_id="weekly_10y_d_overlay_0529",
            run_id=106,
            benchmark_id="model_muti_0529",
            data_source="framework_db_aligned",
            apply=True,
        )

        self.assertTrue(summary["applied"])
        self.assertEqual(_count(engine, "t_backtest_runs"), 1)
        self.assertEqual(_count(engine, "t_backtest_predictions"), 1)
        self.assertEqual(_count(engine, "t_backtest_monthly_metrics"), 1)
        with engine.connect() as conn:
            remaining = conn.execute(text("SELECT id FROM t_backtest_runs")).scalar_one()
        self.assertEqual(remaining, 107)

    def test_rejects_scheme_mismatch(self) -> None:
        from scripts import delete_backtest_runs

        engine = _engine_with_backtest_run()
        with self.assertRaisesRegex(SystemExit, "scheme_id mismatch"):
            delete_backtest_runs.delete_backtest_run(
                engine,
                scheme_id="weekly_5y_direct_0529",
                run_id=106,
                apply=False,
            )


class DeleteBadLivePredictionsTests(unittest.TestCase):
    def test_apply_deletes_only_audited_bad_prediction_rows(self) -> None:
        from scripts import delete_bad_live_predictions

        engine = _engine_with_live_predictions()
        summary = delete_bad_live_predictions.delete_bad_live_predictions(engine, apply=True)

        self.assertTrue(summary["applied"])
        self.assertEqual(summary["deleted_prediction_rows"], 4)
        self.assertEqual(_count(engine, "t_scheme_predictions"), 1)
        self.assertEqual(_count(engine, "t_scheme_runs"), 5)
        self.assertEqual(_count(engine, "t_scheme_run_log"), 5)
        with engine.connect() as conn:
            remaining = conn.execute(text("SELECT run_id FROM t_scheme_predictions")).scalar_one()
        self.assertEqual(remaining, 58)


def _engine_with_backtest_run():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE t_backtest_runs (
                    id INTEGER PRIMARY KEY,
                    backtest_run_id INTEGER,
                    benchmark_id TEXT,
                    scheme_id TEXT,
                    data_source TEXT,
                    start_date TEXT,
                    end_date TEXT,
                    status TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_backtest_predictions (
                    run_id INTEGER,
                    scheme_id TEXT,
                    target_tenor TEXT,
                    predict_date TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_backtest_monthly_metrics (
                    run_id INTEGER,
                    scheme_id TEXT,
                    target_tenor TEXT,
                    month TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_backtest_reproduction_checks (
                    id INTEGER PRIMARY KEY,
                    benchmark_id TEXT,
                    check_name TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO t_backtest_runs
                    (id, backtest_run_id, benchmark_id, scheme_id, data_source, start_date, end_date, status)
                VALUES
                    (106, 106, 'model_muti_0529', 'weekly_10y_d_overlay_0529', 'framework_db_aligned',
                     '2025-07-11', '2026-05-22', 'success'),
                    (107, 107, 'v28_daily_5y_2', 'daily_5y_2_v28', 'framework_db_aligned',
                     '2025-01-02', '2026-05-22', 'success')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO t_backtest_predictions (run_id, scheme_id, target_tenor, predict_date)
                VALUES
                    (106, 'weekly_10y_d_overlay_0529', '10Y', '2025-07-11'),
                    (106, 'weekly_10y_d_overlay_0529', '10Y', '2025-07-18'),
                    (107, 'daily_5y_2_v28', '5Y', '2026-05-22')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO t_backtest_monthly_metrics (run_id, scheme_id, target_tenor, month)
                VALUES
                    (106, 'weekly_10y_d_overlay_0529', '10Y', '2025-07'),
                    (107, 'daily_5y_2_v28', '5Y', '2026-05')
                """
            )
        )
    return engine


def _engine_with_live_predictions():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_predictions (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER,
                    scheme_id TEXT,
                    predict_date TEXT,
                    feature_date TEXT,
                    target_date TEXT,
                    prediction_phase TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_runs (
                    run_id INTEGER PRIMARY KEY,
                    scheme_id TEXT,
                    status TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_run_log (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER,
                    scheme_id TEXT,
                    status TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_predictions
                    (id, run_id, scheme_id, predict_date, feature_date, target_date, prediction_phase)
                VALUES
                    (5, 31, 'weekly_5y_direct_0529', '2026-06-06', '2026-05-29', '2026-06-05', 'gray_live'),
                    (1, 57, 'weekly_5y_direct_0529', '2026-06-13', '2026-05-15', '2026-05-22', 'scheduled_live'),
                    (2, 56, 'weekly_7y_cross_d_overlay_0529', '2026-06-13', '2026-05-15', '2026-05-22', 'scheduled_live'),
                    (3, 58, 'weekly_5y_direct_0529', '2026-06-20', '2026-06-12', '2026-06-19', 'scheduled_live'),
                    (4, 42, 'daily_5y_2_v28', '2026-05-29', '2026-05-28', '2026-06-04', 'gray_live')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_runs (run_id, scheme_id, status)
                VALUES
                    (31, 'weekly_5y_direct_0529', 'success'),
                    (57, 'weekly_5y_direct_0529', 'success'),
                    (56, 'weekly_7y_cross_d_overlay_0529', 'success'),
                    (58, 'weekly_5y_direct_0529', 'success'),
                    (42, 'daily_5y_2_v28', 'success')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_run_log (id, run_id, scheme_id, status)
                VALUES
                    (5, 31, 'weekly_5y_direct_0529', 'success'),
                    (1, 57, 'weekly_5y_direct_0529', 'success'),
                    (2, 56, 'weekly_7y_cross_d_overlay_0529', 'success'),
                    (3, 58, 'weekly_5y_direct_0529', 'success'),
                    (4, 42, 'daily_5y_2_v28', 'success')
                """
            )
        )
    return engine


def _count(engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
