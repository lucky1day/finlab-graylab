from __future__ import annotations

import json
import unittest

from sqlalchemy import create_engine, text


def _create_schema(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    scheme_id TEXT,
                    feature_date TEXT,
                    prediction_phase TEXT,
                    extra TEXT
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


def _insert_prediction(engine, *, run_id: int, scheme_id: str, extra: dict) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_runs (run_id, scheme_id, prediction_phase)
                VALUES (:run_id, :scheme_id, NULL)
                """
            ),
            {"run_id": run_id, "scheme_id": scheme_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_predictions
                    (run_id, scheme_id, feature_date, prediction_phase, extra)
                VALUES
                    (:run_id, :scheme_id, NULL, NULL, :extra)
                """
            ),
            {"run_id": run_id, "scheme_id": scheme_id, "extra": json.dumps(extra)},
        )


class PredictionSemanticsBackfillTests(unittest.TestCase):
    def test_collects_explicit_run_id_mapping_without_applying(self) -> None:
        from scripts.backfill_prediction_semantics import collect_updates

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        _insert_prediction(
            engine,
            run_id=39,
            scheme_id="daily_5y_2_v28",
            extra={"feature_date": "2026-05-25", "anchor_date": "2026-05-25"},
        )

        updates, errors = collect_updates(engine)

        self.assertEqual(errors, [])
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].feature_date, "2026-05-25")
        self.assertEqual(updates[0].prediction_phase, "gray_live")

        with engine.connect() as conn:
            row = conn.execute(text("SELECT feature_date, prediction_phase FROM t_scheme_predictions")).one()
        self.assertEqual(tuple(row), (None, None))

    def test_apply_updates_prediction_rows_and_run_rows(self) -> None:
        from scripts.backfill_prediction_semantics import apply_updates, collect_updates

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        _insert_prediction(
            engine,
            run_id=52,
            scheme_id="daily_5y_2_v28",
            extra={"feature_date": "2026-06-11", "anchor_date": "2026-06-11"},
        )

        updates, errors = collect_updates(engine)
        self.assertEqual(errors, [])
        apply_updates(engine, updates)

        with engine.connect() as conn:
            pred = conn.execute(text("SELECT feature_date, prediction_phase FROM t_scheme_predictions")).one()
            run = conn.execute(text("SELECT prediction_phase FROM t_scheme_runs WHERE run_id = 52")).scalar_one()
        self.assertEqual(tuple(pred), ("2026-06-11", "scheduled_live"))
        self.assertEqual(run, "scheduled_live")

    def test_missing_feature_date_is_error(self) -> None:
        from scripts.backfill_prediction_semantics import collect_updates

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        _insert_prediction(engine, run_id=39, scheme_id="daily_5y_2_v28", extra={})

        _, errors = collect_updates(engine)

        self.assertTrue(any("missing feature_date" in error for error in errors), errors)

    def test_anchor_date_must_match_feature_date(self) -> None:
        from scripts.backfill_prediction_semantics import collect_updates

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        _insert_prediction(
            engine,
            run_id=39,
            scheme_id="daily_5y_2_v28",
            extra={"feature_date": "2026-05-25", "anchor_date": "2026-05-26"},
        )

        _, errors = collect_updates(engine)

        self.assertTrue(any("anchor_date" in error for error in errors), errors)

    def test_unknown_run_id_is_error_when_phase_is_missing(self) -> None:
        from scripts.backfill_prediction_semantics import collect_updates

        engine = create_engine("sqlite:///:memory:")
        _create_schema(engine)
        _insert_prediction(
            engine,
            run_id=999,
            scheme_id="daily_5y_2_v28",
            extra={"feature_date": "2026-06-11"},
        )

        _, errors = collect_updates(engine)

        self.assertTrue(any("no explicit prediction_phase mapping" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
