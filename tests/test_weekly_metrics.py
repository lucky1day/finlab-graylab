from __future__ import annotations

import unittest

from sqlalchemy import create_engine, text


def _create_weekly_schema(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    scheme_id TEXT,
                    target_tenor TEXT,
                    horizon INTEGER,
                    predict_date TEXT,
                    feature_date TEXT,
                    target_date TEXT,
                    prediction_phase TEXT,
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
                CREATE TABLE t_scheme_serving_pointer (
                    scheme_id TEXT,
                    target_tenor TEXT,
                    predict_date TEXT,
                    serving_run_id INTEGER,
                    serving_status TEXT
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


class WeeklyMetricsTests(unittest.TestCase):
    def test_scheme_metrics_uses_weekly_actuals_for_horizon_6(self) -> None:
        from backend.services import scheme_metrics

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        _create_weekly_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions
                        (run_id, scheme_id, target_tenor, horizon, predict_date, target_date,
                         predicted_direction, confidence, model_version, extra)
                    VALUES
                        (1, 'demo_weekly_scheme', '10Y', 6, '2026-05-23', '2026-05-29',
                         -1, 0.32, 'test', '{"frequency":"weekly","feature_date":"2026-05-22"}')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_serving_pointer
                        (scheme_id, target_tenor, predict_date, serving_run_id, serving_status)
                    VALUES ('demo_weekly_scheme', '10Y', '2026-05-23', 1, 'approved')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_actuals
                        (tenor, trade_date, direction_1d, direction_5d)
                    VALUES ('10Y', '2026-05-29', 1, 1)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_weekly_actuals
                        (tenor, predict_date, target_date, direction_weekly)
                    VALUES ('10Y', '2026-05-23', '2026-05-29', -1)
                    """
                )
            )

        result = scheme_metrics(engine, "demo_weekly_scheme", "10Y")

        self.assertEqual(result["summary"]["samples"], 1)
        self.assertEqual(result["summary"]["correct"], 1)
        self.assertEqual(result["daily_rows"][0]["feature_date"], "2026-05-22")
        self.assertEqual(result["daily_rows"][0]["actual_direction"], -1)
        self.assertTrue(result["daily_rows"][0]["is_correct"])

    def test_scheme_metrics_buckets_weekly_rows_by_target_month(self) -> None:
        from backend.services import scheme_metrics

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        _create_weekly_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions
                        (run_id, scheme_id, target_tenor, horizon, predict_date, target_date,
                         predicted_direction, confidence, model_version, extra)
                    VALUES
                        (1, 'demo_weekly_scheme', '10Y', 6, '2025-10-25', '2025-11-07',
                         1, 0.32, 'test', '{"frequency":"weekly","feature_date":"2025-10-24"}'),
                        (2, 'demo_weekly_scheme', '10Y', 6, '2025-11-01', '2025-11-14',
                         -1, 0.32, 'test', '{"frequency":"weekly","feature_date":"2025-10-31"}')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_serving_pointer
                        (scheme_id, target_tenor, predict_date, serving_run_id, serving_status)
                    VALUES
                        ('demo_weekly_scheme', '10Y', '2025-10-25', 1, 'approved'),
                        ('demo_weekly_scheme', '10Y', '2025-11-01', 2, 'approved')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_weekly_actuals
                        (tenor, predict_date, target_date, direction_weekly)
                    VALUES
                        ('10Y', '2025-10-25', '2025-11-07', -1),
                        ('10Y', '2025-11-01', '2025-11-14', 1)
                    """
                )
            )

        result = scheme_metrics(engine, "demo_weekly_scheme", "10Y")
        metrics = {row["month"]: row for row in result["monthly_metrics"]}

        # 按 target_date 分组,两行 target 均在 11月
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics["2025-11"]["samples"], 2)
        self.assertEqual([row["feature_date"] for row in result["daily_rows"]], ["2025-10-24", "2025-10-31"])


if __name__ == "__main__":
    unittest.main()
