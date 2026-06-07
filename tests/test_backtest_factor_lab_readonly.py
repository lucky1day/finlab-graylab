from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text


class BacktestFactorLabReadonlyTests(unittest.TestCase):
    def test_factor_lab_results_do_not_sync_scheme_registry(self) -> None:
        from backend.services import backtest_factor_lab_results

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE t_target_registry (
                        target_code TEXT,
                        display_name TEXT,
                        asset_class TEXT,
                        target_type TEXT,
                        sort_order INTEGER,
                        status TEXT,
                        extra TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_scheme_registry (
                        scheme_id TEXT,
                        name TEXT,
                        description TEXT,
                        horizon INTEGER,
                        tenors TEXT,
                        frequency TEXT,
                        schedule_cron TEXT,
                        schedule_timezone TEXT,
                        status TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_runs (
                        id INTEGER,
                        benchmark_id TEXT,
                        scheme_id TEXT,
                        data_source TEXT,
                        start_date TEXT,
                        end_date TEXT,
                        status TEXT,
                        summary TEXT,
                        report_path TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_monthly_metrics (
                        run_id INTEGER,
                        target_tenor TEXT,
                        horizon INTEGER,
                        month TEXT,
                        sample_count INTEGER,
                        correct_count INTEGER,
                        accuracy REAL,
                        up_precision REAL,
                        up_recall REAL,
                        down_precision REAL,
                        down_recall REAL,
                        actual_dist TEXT,
                        predicted_dist TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_backtest_predictions (
                        run_id INTEGER,
                        target_tenor TEXT,
                        horizon INTEGER,
                        predict_date TEXT,
                        feature_date TEXT,
                        target_date TEXT,
                        label INTEGER,
                        predicted_direction INTEGER,
                        confidence REAL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_target_registry
                        (target_code, display_name, asset_class, target_type, sort_order,
                         status, extra, created_at, updated_at)
                    VALUES
                        ('10Y', '10Y国债活跃', 'bond', 'active_treasury', 4,
                         'active', '{}', NULL, NULL)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, name, description, horizon, tenors, frequency,
                         schedule_cron, schedule_timezone, status)
                    VALUES
                        ('weekly_10y_d_overlay', '周度10Y', '只读回测方案', 6, '["10Y"]',
                         'weekly', '30 11 * * 6', 'Asia/Shanghai', 'paused')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_runs
                        (id, benchmark_id, scheme_id, data_source, start_date, end_date,
                         status, summary, report_path, created_at, updated_at)
                    VALUES
                        (15, 'model_muti_0529', 'weekly_10y_d_overlay',
                         'framework_db_aligned', '2025-07-12', '2026-05-09',
                         'success', '{}', NULL, NULL, '2026-06-05T21:32:13')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_monthly_metrics
                        (run_id, target_tenor, horizon, month, sample_count, correct_count,
                         accuracy, up_precision, up_recall, down_precision, down_recall,
                         actual_dist, predicted_dist)
                    VALUES
                        (15, '10Y', 6, '2026-05', 2, 2,
                         1.0, NULL, NULL, 1.0, 1.0,
                         :actual_dist, :predicted_dist)
                    """
                ),
                {
                    "actual_dist": '{"up":0,"down":2,"flat":0}',
                    "predicted_dist": '{"up":0,"down":2,"flat":0}',
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_backtest_predictions
                        (run_id, target_tenor, horizon, predict_date, feature_date,
                         target_date, label, predicted_direction, confidence)
                    VALUES
                        (15, '10Y', 6, '2026-05-09', '2026-05-08',
                         '2026-05-15', -1, -1, 0.32)
                    """
                )
            )

        with patch(
            "backend.services.sync_registry_from_configs",
            side_effect=AssertionError("factor-lab endpoint must not sync registry"),
        ):
            result = backtest_factor_lab_results(engine)

        self.assertEqual(result["schemes"][0]["scheme_id"], "weekly_10y_d_overlay")
        self.assertEqual(result["schemes"][0]["frequency"], "weekly")
        self.assertEqual(result["schemes"][0]["summary"]["accuracy"], 100.0)
        self.assertEqual(result["benchmark_label"], "0529历史基准")
        self.assertEqual(result["data_source_label"], "当前DB对齐回测")
        self.assertEqual(result["schemes"][0]["scheme_name"], "周度10Y")
        self.assertEqual(result["schemes"][0]["data_source_label"], "当前DB对齐回测")
        self.assertEqual(result["schemes"][0]["name"], "周度10Y｜10Y国债活跃｜当前DB对齐回测")


if __name__ == "__main__":
    unittest.main()
