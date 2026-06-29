from __future__ import annotations

import unittest

from sqlalchemy import create_engine, text


def _create_weekly_schema(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_registry (
                    scheme_id TEXT PRIMARY KEY,
                    base_scheme_id TEXT,
                    name TEXT,
                    description TEXT,
                    horizon INTEGER,
                    task_type TEXT,
                    frequency TEXT,
                    target_tenor TEXT,
                    schedule_cron TEXT,
                    schedule_timezone TEXT,
                    status TEXT,
                    deployed_at TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
        )
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
                    direction_weekly INTEGER,
                    target_rule TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_monthly_actuals (
                    tenor TEXT,
                    feature_month_id TEXT,
                    target_month_id TEXT,
                    predict_date TEXT,
                    feature_date TEXT,
                    target_date TEXT,
                    feature_yield REAL,
                    target_yield REAL,
                    direction_monthly INTEGER,
                    price_signal TEXT,
                    target_rule TEXT,
                    extra TEXT
                )
                """
            )
        )


def _register_weekly_scheme(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_registry
                    (scheme_id, base_scheme_id, name, description, horizon, task_type, frequency, target_tenor,
                     schedule_cron, schedule_timezone, status, deployed_at, created_at, updated_at)
                VALUES
                    ('demo_weekly_scheme__h6__10Y', 'demo_weekly_scheme', 'Demo Weekly', '',
                     6, 'weekly_point', 'weekly', '10Y', '30 11 * * 6', 'Asia/Shanghai', 'active',
                     '2026-06-09', '2026-06-09', '2026-06-09')
                """
            )
        )


class WeeklyMetricsTests(unittest.TestCase):
    def test_scheme_metrics_uses_weekly_actuals_for_horizon_6(self) -> None:
        from backend.services import scheme_metrics
        from shared.prediction_context import WEEKLY_TARGET_RULE

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        _create_weekly_schema(engine)
        _register_weekly_scheme(engine)
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
                        (tenor, predict_date, target_date, direction_weekly, target_rule)
                    VALUES ('10Y', '2026-05-23', '2026-05-29', -1, :target_rule)
                    """
                ),
                {"target_rule": WEEKLY_TARGET_RULE},
            )

        result = scheme_metrics(engine, "demo_weekly_scheme__h6__10Y")

        self.assertEqual(result["summary"]["samples"], 1)
        self.assertEqual(result["summary"]["correct"], 1)
        self.assertEqual(result["daily_rows"][0]["feature_date"], "2026-05-22")
        self.assertEqual(result["daily_rows"][0]["actual_direction"], -1)
        self.assertTrue(result["daily_rows"][0]["is_correct"])

    def test_scheme_metrics_buckets_weekly_rows_by_target_month(self) -> None:
        from backend.services import scheme_metrics
        from shared.prediction_context import WEEKLY_TARGET_RULE

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        _create_weekly_schema(engine)
        _register_weekly_scheme(engine)
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
                    INSERT INTO t_scheme_weekly_actuals
                        (tenor, predict_date, target_date, direction_weekly, target_rule)
                    VALUES
                        ('10Y', '2025-10-25', '2025-11-07', -1, :target_rule),
                        ('10Y', '2025-11-01', '2025-11-14', 1, :target_rule)
                    """
                ),
                {"target_rule": WEEKLY_TARGET_RULE},
            )

        result = scheme_metrics(engine, "demo_weekly_scheme__h6__10Y")
        metrics = {row["month"]: row for row in result["monthly_metrics"]}

        # 按 target_date 分组,两行 target 均在 11月
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics["2025-11"]["samples"], 2)
        self.assertEqual([row["feature_date"] for row in result["daily_rows"]], ["2025-10-24", "2025-10-31"])

    def test_scheme_metrics_uses_weekly_average_actuals_for_weekly_average_task(self) -> None:
        from backend.services import scheme_metrics
        from shared.prediction_context import WEEKLY_AVERAGE_TARGET_RULE, WEEKLY_TARGET_RULE

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        _create_weekly_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, name, description, horizon, task_type, frequency, target_tenor,
                         schedule_cron, schedule_timezone, status, deployed_at, created_at, updated_at)
                    VALUES
                        ('demo_weekly_average__h6__10Y', 'demo_weekly_average', 'Demo Weekly Average', '',
                         6, 'weekly_average', 'weekly', '10Y', '30 11 * * 6', 'Asia/Shanghai', 'active',
                         '2026-06-09', '2026-06-09', '2026-06-09')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions
                        (run_id, scheme_id, target_tenor, horizon, predict_date, target_date,
                         predicted_direction, confidence, model_version, extra)
                    VALUES
                        (1, 'demo_weekly_average', '10Y', 6, '2026-05-23', '2026-05-29',
                         1, 0.32, 'test', '{"frequency":"weekly","feature_date":"2026-05-22"}')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_weekly_actuals
                        (tenor, predict_date, target_date, direction_weekly, target_rule)
                    VALUES
                        ('10Y', '2026-05-23', '2026-05-29', -1, :point_rule),
                        ('10Y', '2026-05-23', '2026-05-29', 1, :average_rule)
                    """
                ),
                {
                    "point_rule": WEEKLY_TARGET_RULE,
                    "average_rule": WEEKLY_AVERAGE_TARGET_RULE,
                },
            )

        result = scheme_metrics(engine, "demo_weekly_average__h6__10Y")

        self.assertEqual(result["task_type"], "weekly_average")
        self.assertEqual(result["summary"]["samples"], 1)
        self.assertEqual(result["summary"]["correct"], 1)
        self.assertEqual(result["daily_rows"][0]["actual_direction"], 1)
        self.assertTrue(result["daily_rows"][0]["is_correct"])

    def test_scheme_metrics_uses_monthly_actuals_for_monthly_task(self) -> None:
        from backend.services import scheme_metrics
        from shared.prediction_context import MONTHLY_TARGET_RULE

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        _create_weekly_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, name, description, horizon, task_type, frequency, target_tenor,
                         schedule_cron, schedule_timezone, status, deployed_at, created_at, updated_at)
                    VALUES
                        ('demo_monthly__h30__10Y', 'demo_monthly', 'Demo Monthly', '',
                         30, 'monthly', 'monthly', '10Y', '0 18 15 * *', 'Asia/Shanghai', 'active',
                         '2026-06-29', '2026-06-29', '2026-06-29')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions
                        (run_id, scheme_id, target_tenor, horizon, predict_date, feature_date, target_date,
                         predicted_direction, confidence, model_version, extra)
                    VALUES
                        (1, 'demo_monthly', '10Y', 30, '2026-04-15', '2026-04-15', '2026-05-15',
                         1, 0.68, 'test',
                         '{"frequency":"monthly","feature_month_id":"2026-04","target_month_id":"2026-05",
                           "feature_date":"2026-04-15","target_date":"2026-05-15",
                           "target_rule":"next_month_observation_yield_vs_feature_month_observation_yield"}')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_actuals
                        (tenor, trade_date, direction_1d, direction_5d)
                    VALUES ('10Y', '2026-05-15', -1, -1)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_monthly_actuals
                        (tenor, feature_month_id, target_month_id, predict_date, feature_date, target_date,
                         feature_yield, target_yield, direction_monthly, price_signal, target_rule, extra)
                    VALUES
                        ('10Y', '2026-04', '2026-05', '2026-04-15', '2026-04-15', '2026-05-15',
                         1.77, 1.82, 1, '空', :target_rule, '{}')
                    """
                ),
                {"target_rule": MONTHLY_TARGET_RULE},
            )

        result = scheme_metrics(engine, "demo_monthly__h30__10Y")

        self.assertEqual(result["task_type"], "monthly")
        self.assertEqual(result["summary"]["samples"], 1)
        self.assertEqual(result["summary"]["correct"], 1)
        self.assertEqual(result["daily_rows"][0]["actual_direction"], 1)
        self.assertTrue(result["daily_rows"][0]["is_correct"])


if __name__ == "__main__":
    unittest.main()
