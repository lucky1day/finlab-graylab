from __future__ import annotations

import unittest

from sqlalchemy import create_engine, text


class WeeklyMetricsTests(unittest.TestCase):
    def test_scheme_metrics_uses_weekly_actuals_for_horizon_6(self) -> None:
        from backend.services import scheme_metrics

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE t_scheme_predictions (
                        scheme_id TEXT,
                        target_tenor TEXT,
                        horizon INTEGER,
                        predict_date TEXT,
                        target_date TEXT,
                        predicted_direction INTEGER,
                        confidence REAL,
                        model_version TEXT,
                        extra TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_scheme_actuals (
                        tenor TEXT,
                        trade_date TEXT,
                        direction_1d INTEGER,
                        direction_5d INTEGER
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_scheme_weekly_actuals (
                        tenor TEXT,
                        predict_date TEXT,
                        target_date TEXT,
                        direction_weekly INTEGER
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions
                        (scheme_id, target_tenor, horizon, predict_date, target_date,
                         predicted_direction, confidence, model_version, extra)
                    VALUES
                        ('weekly_10y_d_overlay', '10Y', 6, '2026-05-23', '2026-05-29',
                         -1, 0.32, 'test', '{"frequency":"weekly","feature_date":"2026-05-22"}')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_actuals
                        (tenor, trade_date, direction_1d, direction_5d)
                    VALUES
                        ('10Y', '2026-05-29', 1, 1)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_weekly_actuals
                        (tenor, predict_date, target_date, direction_weekly)
                    VALUES
                        ('10Y', '2026-05-23', '2026-05-29', -1)
                    """
                )
            )

        result = scheme_metrics(engine, "weekly_10y_d_overlay", "10Y")

        self.assertEqual(result["summary"]["samples"], 1)
        self.assertEqual(result["summary"]["correct"], 1)
        self.assertEqual(result["daily_rows"][0]["actual_direction"], -1)
        self.assertTrue(result["daily_rows"][0]["is_correct"])

    def test_scheme_metrics_buckets_weekly_rows_by_feature_month(self) -> None:
        from backend.services import scheme_metrics

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE t_scheme_predictions (
                        scheme_id TEXT,
                        target_tenor TEXT,
                        horizon INTEGER,
                        predict_date TEXT,
                        target_date TEXT,
                        predicted_direction INTEGER,
                        confidence REAL,
                        model_version TEXT,
                        extra TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_scheme_actuals (
                        tenor TEXT,
                        trade_date TEXT,
                        direction_1d INTEGER,
                        direction_5d INTEGER
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE t_scheme_weekly_actuals (
                        tenor TEXT,
                        predict_date TEXT,
                        target_date TEXT,
                        direction_weekly INTEGER
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions
                        (scheme_id, target_tenor, horizon, predict_date, target_date,
                         predicted_direction, confidence, model_version, extra)
                    VALUES
                        ('weekly_10y_d_overlay', '10Y', 6, '2025-10-25', '2025-10-31',
                         1, 0.32, 'test', '{"frequency":"weekly","feature_date":"2025-10-24"}'),
                        ('weekly_10y_d_overlay', '10Y', 6, '2025-11-01', '2025-11-07',
                         -1, 0.32, 'test', '{"frequency":"weekly","feature_date":"2025-10-31"}')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_weekly_actuals
                        (tenor, predict_date, target_date, direction_weekly)
                    VALUES
                        ('10Y', '2025-10-25', '2025-10-31', -1),
                        ('10Y', '2025-11-01', '2025-11-07', 1)
                    """
                )
            )

        result = scheme_metrics(engine, "weekly_10y_d_overlay", "10Y")
        metrics = {row["month"]: row for row in result["monthly_metrics"]}

        self.assertEqual(metrics["2025-10"]["samples"], 2)
        self.assertNotIn("2025-11", metrics)


if __name__ == "__main__":
    unittest.main()
