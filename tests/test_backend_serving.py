from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event, text


def _create_schema(engine) -> None:
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
                    target_date TEXT,
                    predicted_direction INTEGER,
                    confidence REAL,
                    model_version TEXT,
                    extra TEXT,
                    created_at TEXT DEFAULT '2026-06-09 00:00:00',
                    updated_at TEXT DEFAULT '2026-06-09 00:00:00'
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


def _seed_predictions(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_predictions
                    (run_id, scheme_id, target_tenor, horizon,
                     predict_date, target_date, predicted_direction, confidence,
                     model_version, extra)
                VALUES
                    (1, 'demo_daily', '10Y', 1, '2026-06-05',
                     '2026-06-06', -1, 0.9, 'new', '{}')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_serving_pointer
                    (scheme_id, target_tenor, predict_date, serving_run_id, serving_status)
                VALUES ('demo_daily', '10Y', '2026-06-05', 1, 'approved')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_actuals (tenor, trade_date, direction_1d, direction_5d)
                VALUES ('10Y', '2026-06-06', -1, -1)
                """
            )
        )


class BackendServingPointerTests(unittest.TestCase):
    def test_scheme_metrics_returns_only_approved_serving_predictions(self) -> None:
        """仅有 serving_status=approved 的预测参与指标计算。"""
        from backend.services import scheme_metrics

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        _seed_predictions(engine)
        # 追加一个旧 run(非 approved)，不应被计入
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions
                        (run_id, scheme_id, target_tenor, horizon,
                         predict_date, target_date, predicted_direction, confidence,
                         model_version, extra)
                    VALUES (0, 'demo_daily', '10Y', 1, '2026-06-05',
                            '2026-06-06', 1, 0.4, 'old', '{}')
                    """
                )
            )
        try:
            result = scheme_metrics(engine, "demo_daily", "10Y")
        finally:
            engine.dispose()

        # 仅 approved run(1) 计入 — 样本数 1，准确率 100%
        self.assertEqual(result["summary"]["samples"], 1)
        self.assertEqual(result["summary"]["accuracy"], 100.0)
        self.assertEqual(len(result["daily_rows"]), 1)
        row = result["daily_rows"][0]
        self.assertEqual(row["predicted_direction"], -1)
        self.assertTrue(row["is_correct"])

    def test_scheme_metrics_buckets_daily_by_predict_date_and_matches_actuals_on_target_date(self) -> None:
        """日频月度归属按预测日；真实方向仍按被预测日 target_date 匹配。"""
        from backend.services import scheme_metrics

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions
                        (run_id, scheme_id, target_tenor, horizon,
                         predict_date, target_date, predicted_direction, confidence,
                         model_version, extra)
                    VALUES (7, 'demo_t5', '10Y', 5, '2026-05-29',
                            '2026-06-05', 1, 0.6, 'v1', '{}')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_serving_pointer
                        (scheme_id, target_tenor, predict_date, serving_run_id, serving_status)
                    VALUES ('demo_t5', '10Y', '2026-05-29', 7, 'approved')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_actuals (tenor, trade_date, direction_1d, direction_5d)
                    VALUES
                        ('10Y', '2026-05-29', -1, -1),
                        ('10Y', '2026-06-05', 1, 1)
                    """
                )
        )
        try:
            result = scheme_metrics(engine, "demo_t5", "10Y")
            may_result = scheme_metrics(engine, "demo_t5", "10Y", start_month="2026-05", end_month="2026-05")
            june_result = scheme_metrics(engine, "demo_t5", "10Y", start_month="2026-06", end_month="2026-06")
        finally:
            engine.dispose()

        self.assertEqual(result["summary"]["samples"], 1)
        self.assertEqual(result["summary"]["correct"], 1)
        self.assertEqual(result["monthly_metrics"][0]["month"], "2026-05")
        row = result["daily_rows"][0]
        self.assertEqual(row["actual_direction"], 1)
        self.assertTrue(row["is_correct"])
        self.assertEqual(may_result["summary"]["samples"], 1)
        self.assertEqual(may_result["monthly_metrics"][0]["month"], "2026-05")
        self.assertEqual(june_result["summary"]["samples"], 0)
        self.assertEqual(june_result["monthly_metrics"], [])

    def test_scheme_metrics_keeps_distinct_predict_dates_for_duplicate_target_date(self) -> None:
        """同一目标日可由多个预测日产生，日频明细和指标应按预测日分别计样本。"""
        from backend.services import scheme_metrics

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions
                        (run_id, scheme_id, target_tenor, horizon,
                         predict_date, target_date, predicted_direction, confidence,
                         model_version, extra)
                    VALUES
                        (7, 'demo_t5', '10Y', 5, '2026-06-04',
                         '2026-06-10', -1, 0.6, 'old', '{}'),
                        (8, 'demo_t5', '10Y', 5, '2026-06-05',
                         '2026-06-10', 1, 0.7, 'new', '{}')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_serving_pointer
                        (scheme_id, target_tenor, predict_date, serving_run_id, serving_status)
                    VALUES
                        ('demo_t5', '10Y', '2026-06-04', 7, 'approved'),
                        ('demo_t5', '10Y', '2026-06-05', 8, 'approved')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_actuals (tenor, trade_date, direction_1d, direction_5d)
                    VALUES ('10Y', '2026-06-10', 1, 1)
                    """
                )
            )
        try:
            with patch.dict("os.environ", {"BOND_FACTOR_LAB_TODAY": "2026-06-10"}):
                result = scheme_metrics(engine, "demo_t5", "10Y")
        finally:
            engine.dispose()

        self.assertEqual(result["summary"]["samples"], 2)
        self.assertEqual([row["predict_date"] for row in result["daily_rows"]], ["2026-06-04", "2026-06-05"])
        self.assertEqual([row["target_date"] for row in result["daily_rows"]], ["2026-06-10", "2026-06-10"])
        self.assertEqual([row["is_correct"] for row in result["daily_rows"]], [False, True])

    def test_scheme_metrics_keeps_distinct_predict_dates_with_same_feature_date(self) -> None:
        """即使 feature_date 相同，不同 predict_date 仍是不同实盘预测点。"""
        from backend.services import scheme_metrics

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions
                        (run_id, scheme_id, target_tenor, horizon,
                         predict_date, target_date, predicted_direction, confidence,
                         model_version, extra)
                    VALUES
                        (7, 'demo_t5', '10Y', 5, '2026-06-04',
                         '2026-06-10', -1, 0.6, 'first',
                         '{"feature_date":"2026-06-03"}'),
                        (8, 'demo_t5', '10Y', 5, '2026-06-05',
                         '2026-06-10', 1, 0.7, 'stale-rerun',
                         '{"feature_date":"2026-06-03"}')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_serving_pointer
                        (scheme_id, target_tenor, predict_date, serving_run_id, serving_status)
                    VALUES
                        ('demo_t5', '10Y', '2026-06-04', 7, 'approved'),
                        ('demo_t5', '10Y', '2026-06-05', 8, 'approved')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_actuals (tenor, trade_date, direction_1d, direction_5d)
                    VALUES ('10Y', '2026-06-10', -1, -1)
                    """
                )
            )
        try:
            with patch.dict("os.environ", {"BOND_FACTOR_LAB_TODAY": "2026-06-10"}):
                result = scheme_metrics(engine, "demo_t5", "10Y")
        finally:
            engine.dispose()

        self.assertEqual(result["summary"]["samples"], 2)
        self.assertEqual([row["predict_date"] for row in result["daily_rows"]], ["2026-06-04", "2026-06-05"])
        self.assertEqual([row["feature_date"] for row in result["daily_rows"]], ["2026-06-03", "2026-06-03"])
        self.assertEqual([row["target_date"] for row in result["daily_rows"]], ["2026-06-10", "2026-06-10"])
        self.assertEqual([row["is_correct"] for row in result["daily_rows"]], [True, False])

    def test_scheme_metrics_excludes_future_daily_predict_dates_not_future_targets(self) -> None:
        """日频明细按预测日展示；未来目标日可以先以待验证保留。"""
        from backend.services import scheme_metrics

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions
                        (run_id, scheme_id, target_tenor, horizon,
                         predict_date, target_date, predicted_direction, confidence,
                         model_version, extra)
                    VALUES
                        (7, 'demo_t5', '10Y', 5, '2026-06-03',
                         '2026-06-09', -1, 0.6, 'past', '{}'),
                        (8, 'demo_t5', '10Y', 5, '2026-06-10',
                         '2026-06-16', 1, 0.7, 'pending-target', '{}'),
                        (9, 'demo_t5', '10Y', 5, '2026-06-11',
                         '2026-06-17', -1, 0.8, 'future-predict', '{}')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_serving_pointer
                        (scheme_id, target_tenor, predict_date, serving_run_id, serving_status)
                    VALUES
                        ('demo_t5', '10Y', '2026-06-03', 7, 'approved'),
                        ('demo_t5', '10Y', '2026-06-10', 8, 'approved'),
                        ('demo_t5', '10Y', '2026-06-11', 9, 'approved')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_actuals (tenor, trade_date, direction_1d, direction_5d)
                    VALUES ('10Y', '2026-06-09', -1, -1)
                    """
                )
            )
        try:
            with patch.dict("os.environ", {"BOND_FACTOR_LAB_TODAY": "2026-06-10"}):
                result = scheme_metrics(engine, "demo_t5", "10Y")
        finally:
            engine.dispose()

        self.assertEqual([row["predict_date"] for row in result["daily_rows"]], ["2026-06-03", "2026-06-10"])
        self.assertEqual([row["target_date"] for row in result["daily_rows"]], ["2026-06-09", "2026-06-16"])
        self.assertIsNone(result["daily_rows"][1]["actual_direction"])
        self.assertIsNone(result["daily_rows"][1]["is_correct"])
        self.assertEqual(result["summary"]["samples"], 1)

    def test_list_predictions_is_read_only_and_returns_approved_only(self) -> None:
        from backend.services import list_predictions

        engine = create_engine("sqlite:///:memory:")
        statements: list[str] = []

        @event.listens_for(engine, "before_cursor_execute")
        def _capture_statement(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement.strip().lower())

        _create_schema(engine)
        _seed_predictions(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions
                        (run_id, scheme_id, target_tenor, horizon,
                         predict_date, target_date, predicted_direction, confidence,
                         model_version, extra)
                    VALUES (0, 'demo_daily', '10Y', 1, '2026-06-05',
                            '2026-06-06', 1, 0.4, 'old', '{}')
                    """
                )
            )
        statements.clear()
        try:
            result = list_predictions(engine, scheme_id="demo_daily", tenor="10Y")
        finally:
            engine.dispose()

        self.assertEqual(result["total"], 1)
        item = result["items"][0]
        self.assertEqual(item["scheme_id"], "demo_daily")
        self.assertEqual(item["predicted_direction"], -1)
        self.assertFalse(any(stmt.startswith(("insert", "update", "delete", "alter", "drop")) for stmt in statements))


if __name__ == "__main__":
    unittest.main()
