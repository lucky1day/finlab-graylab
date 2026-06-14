from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event, text


def _create_schema(engine) -> None:
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
                    frequency TEXT,
                    target_tenor TEXT,
                    schedule_cron TEXT,
                    schedule_timezone TEXT,
                    status TEXT,
                    deployed_at TEXT,
                    created_at TEXT DEFAULT '2026-06-09 00:00:00',
                    updated_at TEXT DEFAULT '2026-06-09 00:00:00'
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


def _register_scheme(
    engine,
    *,
    scheme_id: str,
    base_scheme_id: str,
    target_tenor: str,
    horizon: int,
    frequency: str = "daily",
    status: str = "active",
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_registry
                    (scheme_id, base_scheme_id, name, description, horizon, frequency, target_tenor,
                     schedule_cron, schedule_timezone, status, deployed_at)
                VALUES
                    (:scheme_id, :base_scheme_id, :scheme_id, '', :horizon, :frequency, :target_tenor,
                     '3 7 * * 1-5', 'Asia/Shanghai', :status, '2026-06-09')
                """
            ),
            {
                "scheme_id": scheme_id,
                "base_scheme_id": base_scheme_id,
                "horizon": horizon,
                "frequency": frequency,
                "target_tenor": target_tenor,
                "status": status,
            },
        )


def _seed_predictions(engine) -> None:
    _register_scheme(
        engine,
        scheme_id="demo_daily__h1__10Y",
        base_scheme_id="demo_daily",
        target_tenor="10Y",
        horizon=1,
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_predictions
                    (run_id, scheme_id, target_tenor, horizon,
                     predict_date, feature_date, target_date, prediction_phase, predicted_direction, confidence,
                     model_version, extra)
                VALUES
                    (1, 'demo_daily', '10Y', 1, '2026-06-05',
                     '2026-06-04', '2026-06-06', 'scheduled_live', -1, 0.9, 'new',
                     '{"feature_date":"2026-06-04","prediction_phase":"scheduled_live"}')
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


class BackendPredictionServingTests(unittest.TestCase):
    def test_list_schemes_returns_active_registry_rows_only(self) -> None:
        from backend.services import list_schemes

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        _register_scheme(
            engine,
            scheme_id="demo_active__h1__10Y",
            base_scheme_id="demo_active",
            target_tenor="10Y",
            horizon=1,
            status="active",
        )
        _register_scheme(
            engine,
            scheme_id="demo_paused__h1__10Y",
            base_scheme_id="demo_paused",
            target_tenor="10Y",
            horizon=1,
            status="paused",
        )
        _register_scheme(
            engine,
            scheme_id="demo_archived__h1__10Y",
            base_scheme_id="demo_archived",
            target_tenor="10Y",
            horizon=1,
            status="archived",
        )

        try:
            rows = list_schemes(engine)
        finally:
            engine.dispose()

        self.assertEqual([row["scheme_id"] for row in rows], ["demo_active__h1__10Y"])

    def test_scheme_metrics_returns_available_predictions(self) -> None:
        """所有预测记录（无 serving pointer 过滤）参与指标计算。"""
        from backend.services import scheme_metrics

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        _seed_predictions(engine)
        # UK 不可变 + UPSERT 模式下，每 (scheme,tenor,horizon,target_date) 只有一条记录，
        # 无需额外过滤层。追加一条不同 target_date 的记录验证查询返回所有行。
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions
                        (run_id, scheme_id, target_tenor, horizon,
                         predict_date, target_date, predicted_direction, confidence,
                         model_version, extra)
                    VALUES (2, 'demo_daily', '10Y', 1, '2026-06-06',
                            '2026-06-09', 1, 0.6, 'v2', '{}')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_actuals (tenor, trade_date, direction_1d, direction_5d)
                    VALUES ('10Y', '2026-06-06', -1, -1), ('10Y', '2026-06-09', 1, 1)
                    """
                )
            )
        try:
            result = scheme_metrics(engine, "demo_daily__h1__10Y")
        finally:
            engine.dispose()

        # 两条预测（target 06-06 和 06-09）均应被计入
        self.assertEqual(result["summary"]["samples"], 2)
        self.assertEqual(len(result["daily_rows"]), 2)
        self.assertEqual(result["scheme_id"], "demo_daily__h1__10Y")
        self.assertEqual(result["base_scheme_id"], "demo_daily")
        self.assertEqual(result["target_tenor"], "10Y")

    def test_scheme_metrics_rejects_base_scheme_id_entrypoint(self) -> None:
        from backend.services import scheme_metrics

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        _seed_predictions(engine)
        try:
            with self.assertRaisesRegex(LookupError, "registry scheme not found"):
                scheme_metrics(engine, "demo_daily")
        finally:
            engine.dispose()

    def test_scheme_metrics_rejects_non_active_registry_rows(self) -> None:
        from backend.services import scheme_metrics

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        _register_scheme(
            engine,
            scheme_id="demo_paused__h1__10Y",
            base_scheme_id="demo_paused",
            target_tenor="10Y",
            horizon=1,
            status="paused",
        )
        _register_scheme(
            engine,
            scheme_id="demo_archived__h1__10Y",
            base_scheme_id="demo_archived",
            target_tenor="10Y",
            horizon=1,
            status="archived",
        )
        try:
            with self.assertRaisesRegex(LookupError, "registry scheme not found"):
                scheme_metrics(engine, "demo_paused__h1__10Y")
            with self.assertRaisesRegex(LookupError, "registry scheme not found"):
                scheme_metrics(engine, "demo_archived__h1__10Y")
        finally:
            engine.dispose()

    def test_scheme_metrics_returns_feature_date_prediction_phase_and_phase_ranges(self) -> None:
        from backend.services import scheme_metrics

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        _register_scheme(
            engine,
            scheme_id="daily_5y_2_v28__h5__5Y",
            base_scheme_id="daily_5y_2_v28",
            target_tenor="5Y",
            horizon=5,
        )
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions
                        (run_id, scheme_id, target_tenor, horizon,
                         predict_date, feature_date, target_date, prediction_phase,
                         predicted_direction, confidence, model_version, extra)
                    VALUES
                        (39, 'daily_5y_2_v28', '5Y', 5, '2026-05-26',
                         '2026-05-25', '2026-06-01', 'gray_live',
                         1, 0.7, 'gray', '{"anchor_date":"2026-05-25"}'),
                        (52, 'daily_5y_2_v28', '5Y', 5, '2026-06-12',
                         '2026-06-11', '2026-06-18', 'scheduled_live',
                         -1, 0.8, 'scheduled', '{}')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_actuals (tenor, trade_date, direction_1d, direction_5d)
                    VALUES ('5Y', '2026-06-01', 1, 1)
                    """
                )
            )
        try:
            with patch.dict("os.environ", {"BOND_FACTOR_LAB_TODAY": "2026-06-20"}):
                result = scheme_metrics(engine, "daily_5y_2_v28__h5__5Y")
        finally:
            engine.dispose()

        self.assertEqual([row["feature_date"] for row in result["daily_rows"]], ["2026-05-25", "2026-06-11"])
        self.assertEqual([row["prediction_phase"] for row in result["daily_rows"]], ["gray_live", "scheduled_live"])
        self.assertEqual(
            result["phase_ranges"],
            [
                {
                    "prediction_phase": "gray_live",
                    "start_predict_date": "2026-05-26",
                    "end_predict_date": "2026-05-26",
                    "start_target_date": "2026-06-01",
                    "end_target_date": "2026-06-01",
                    "rows": 1,
                },
                {
                    "prediction_phase": "scheduled_live",
                    "start_predict_date": "2026-06-12",
                    "end_predict_date": "2026-06-12",
                    "start_target_date": "2026-06-18",
                    "end_target_date": "2026-06-18",
                    "rows": 1,
                },
            ],
        )

    def test_scheme_metrics_buckets_daily_by_target_date_and_matches_actuals_on_target_date(self) -> None:
        """日频月度归属按被预测日 target_date；真实方向仍按 target_date 匹配。"""
        from backend.services import scheme_metrics

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        _register_scheme(
            engine,
            scheme_id="demo_t5__h5__10Y",
            base_scheme_id="demo_t5",
            target_tenor="10Y",
            horizon=5,
        )
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
                    INSERT INTO t_scheme_actuals (tenor, trade_date, direction_1d, direction_5d)
                    VALUES
                        ('10Y', '2026-05-29', -1, -1),
                        ('10Y', '2026-06-05', 1, 1)
                    """
                )
        )
        try:
            result = scheme_metrics(engine, "demo_t5__h5__10Y")
            may_result = scheme_metrics(engine, "demo_t5__h5__10Y", start_month="2026-05", end_month="2026-05")
            june_result = scheme_metrics(engine, "demo_t5__h5__10Y", start_month="2026-06", end_month="2026-06")
        finally:
            engine.dispose()

        self.assertEqual(result["summary"]["samples"], 1)
        self.assertEqual(result["summary"]["correct"], 1)
        self.assertEqual(result["monthly_metrics"][0]["month"], "2026-06")
        row = result["daily_rows"][0]
        self.assertEqual(row["actual_direction"], 1)
        self.assertTrue(row["is_correct"])
        self.assertEqual(may_result["summary"]["samples"], 0)
        self.assertEqual(may_result["monthly_metrics"], [])
        self.assertEqual(june_result["summary"]["samples"], 1)
        self.assertEqual(june_result["monthly_metrics"][0]["month"], "2026-06")

    def test_scheme_metrics_keeps_distinct_target_dates_as_separate_rows(self) -> None:
        """不同 target_date 的预测分别展示，不被去重合并。"""
        from backend.services import scheme_metrics

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        _register_scheme(
            engine,
            scheme_id="demo_t5__h5__10Y",
            base_scheme_id="demo_t5",
            target_tenor="10Y",
            horizon=5,
        )
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
                         '2026-06-10', -1, 0.6, 'v1', '{}'),
                        (8, 'demo_t5', '10Y', 5, '2026-06-05',
                         '2026-06-11', 1, 0.7, 'v2', '{}')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_actuals (tenor, trade_date, direction_1d, direction_5d)
                    VALUES ('10Y', '2026-06-10', -1, -1), ('10Y', '2026-06-11', 1, 1)
                    """
                )
            )
        try:
            with patch.dict("os.environ", {"BOND_FACTOR_LAB_TODAY": "2026-06-11"}):
                result = scheme_metrics(engine, "demo_t5__h5__10Y")
        finally:
            engine.dispose()

        self.assertEqual(result["summary"]["samples"], 2)
        self.assertEqual([row["target_date"] for row in result["daily_rows"]], ["2026-06-10", "2026-06-11"])
        self.assertEqual([row["is_correct"] for row in result["daily_rows"]], [True, True])

    def test_scheme_metrics_keeps_distinct_predict_dates_with_same_feature_date(self) -> None:
        """即使 feature_date 相同，不同 predict_date 仍是不同实盘预测点。"""
        from backend.services import scheme_metrics

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        _register_scheme(
            engine,
            scheme_id="demo_t5__h5__10Y",
            base_scheme_id="demo_t5",
            target_tenor="10Y",
            horizon=5,
        )
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
                         '{"feature_date":"2026-06-04"}')
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
                result = scheme_metrics(engine, "demo_t5__h5__10Y")
        finally:
            engine.dispose()

        # 两条 predict_date 不同但 target_date 相同,UK 保证只保留最新一条
        self.assertEqual(result["summary"]["samples"], 1)
        self.assertEqual(result["daily_rows"][0]["predict_date"], "2026-06-05")
        self.assertEqual(result["daily_rows"][0]["target_date"], "2026-06-10")
        self.assertEqual(result["daily_rows"][0]["is_correct"], True)

    def test_scheme_metrics_excludes_future_daily_predict_dates_not_future_targets(self) -> None:
        """日频明细按预测日展示；未来目标日可以先以待验证保留。"""
        from backend.services import scheme_metrics

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        _register_scheme(
            engine,
            scheme_id="demo_t5__h5__10Y",
            base_scheme_id="demo_t5",
            target_tenor="10Y",
            horizon=5,
        )
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
                        (8, 'demo_t5', '10Y', 5, '2026-06-05',
                         '2026-06-16', 1, 0.7, 'pending-target', '{}'),
                        (9, 'demo_t5', '10Y', 5, '2026-06-11',
                         '2026-06-17', -1, 0.8, 'future-predict', '{}')
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
                result = scheme_metrics(engine, "demo_t5__h5__10Y")
        finally:
            engine.dispose()

        self.assertEqual([row["predict_date"] for row in result["daily_rows"]], ["2026-06-03", "2026-06-05"])
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
        statements.clear()
        try:
            result = list_predictions(engine, scheme_id="demo_daily", tenor="10Y")
        finally:
            engine.dispose()

        self.assertEqual(result["total"], 1)

        # 不再通过 serving pointer 过滤，无写语句
        self.assertFalse(any(stmt.startswith(("insert", "update", "delete", "alter", "drop")) for stmt in statements))


if __name__ == "__main__":
    unittest.main()
