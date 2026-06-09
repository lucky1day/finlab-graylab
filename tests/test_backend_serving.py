from __future__ import annotations

import unittest

from sqlalchemy import create_engine, event, text


def _create_schema(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                    (scheme_id, target_tenor, horizon,
                     predict_date, target_date, predicted_direction, confidence,
                     model_version, extra)
                VALUES
                    ('demo_daily', '10Y', 1, '2026-06-05',
                     '2026-06-06', -1, 0.9, 'new', '{}')
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
    def test_scheme_metrics_returns_correct_accuracy_and_daily_rows(self) -> None:
        from backend.services import scheme_metrics

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        _seed_predictions(engine)
        try:
            result = scheme_metrics(engine, "demo_daily", "10Y")
        finally:
            engine.dispose()

        self.assertEqual(result["summary"]["accuracy"], 100.0)
        self.assertEqual(len(result["daily_rows"]), 1)
        row = result["daily_rows"][0]
        self.assertEqual(row["predicted_direction"], -1)
        self.assertTrue(row["is_correct"])

    def test_list_predictions_is_read_only_and_returns_rows(self) -> None:
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
        item = result["items"][0]
        self.assertEqual(item["scheme_id"], "demo_daily")
        self.assertEqual(item["predicted_direction"], -1)
        self.assertFalse(any(stmt.startswith(("insert", "update", "delete", "alter", "drop")) for stmt in statements))


if __name__ == "__main__":
    unittest.main()
