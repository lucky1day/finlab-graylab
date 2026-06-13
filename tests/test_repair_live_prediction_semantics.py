from __future__ import annotations

import json
import unittest

from sqlalchemy import create_engine, text


def _create_schema(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE t_trade_calendar (
                    rdate TEXT PRIMARY KEY,
                    trade_flag TEXT
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
                    prediction_phase TEXT
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
                    predict_date TEXT,
                    feature_date TEXT,
                    target_date TEXT,
                    prediction_phase TEXT,
                    extra TEXT
                )
                """
            )
        )
        conn.execute(
            text("INSERT INTO t_trade_calendar (rdate, trade_flag) VALUES (:rdate, '1')"),
            [{"rdate": "2026-05-29"}, {"rdate": "2026-06-01"}, {"rdate": "2026-06-12"}],
        )


def _insert_prediction(
    engine,
    *,
    run_id: int,
    scheme_id: str,
    predict_date: str,
    feature_date: str | None,
    prediction_phase: str | None,
    extra: dict,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_runs (run_id, scheme_id, prediction_phase)
                VALUES (:run_id, :scheme_id, :prediction_phase)
                """
            ),
            {"run_id": run_id, "scheme_id": scheme_id, "prediction_phase": prediction_phase},
        )
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_predictions
                    (run_id, scheme_id, predict_date, feature_date, target_date, prediction_phase, extra)
                VALUES
                    (:run_id, :scheme_id, :predict_date, :feature_date, :target_date, :prediction_phase, :extra)
                """
            ),
            {
                "run_id": run_id,
                "scheme_id": scheme_id,
                "predict_date": predict_date,
                "feature_date": feature_date,
                "target_date": "2026-06-01",
                "prediction_phase": prediction_phase,
                "extra": json.dumps(extra),
            },
        )


class RepairLivePredictionSemanticsTests(unittest.TestCase):
    def test_t1_gray_live_feature_date_is_repaired_from_trade_calendar(self) -> None:
        from scripts.repair_live_prediction_semantics import collect_repairs

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        _insert_prediction(
            engine,
            run_id=22,
            scheme_id="t1_daily",
            predict_date="2026-06-01",
            feature_date="2026-06-01",
            prediction_phase="gray_live",
            extra={"feature_date": "2026-06-01", "anchor_date": "2026-06-01"},
        )

        repairs, errors = collect_repairs(engine)

        self.assertEqual(errors, [])
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0].feature_date, "2026-05-29")
        self.assertEqual(repairs[0].prediction_phase, "gray_live")
        self.assertEqual(repairs[0].extra["feature_date"], "2026-05-29")
        self.assertEqual(repairs[0].extra["anchor_date"], "2026-05-29")

    def test_audited_bad_weekly_rows_are_not_repaired(self) -> None:
        from scripts.repair_live_prediction_semantics import collect_repairs

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        _insert_prediction(
            engine,
            run_id=57,
            scheme_id="weekly_5y_direct_0529",
            predict_date="2026-06-13",
            feature_date=None,
            prediction_phase=None,
            extra={"feature_date": "2026-06-12"},
        )

        repairs, errors = collect_repairs(engine)
        self.assertEqual(repairs, [])
        self.assertTrue(any("delete_bad_live_predictions.py" in error for error in errors), errors)

    def test_unhandled_bad_live_row_is_error(self) -> None:
        from scripts.repair_live_prediction_semantics import collect_repairs

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        _insert_prediction(
            engine,
            run_id=999,
            scheme_id="t1_daily",
            predict_date="2026-06-01",
            feature_date="2026-06-01",
            prediction_phase="gray_live",
            extra={"feature_date": "2026-06-01"},
        )

        _, errors = collect_repairs(engine)

        self.assertTrue(any("unhandled bad live row" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
