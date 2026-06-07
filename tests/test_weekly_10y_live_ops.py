from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, text

from shared.models import PredictionRecord


class Weekly10YReadinessTests(unittest.TestCase):
    def _engine_with_weekly_rows(self):
        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE api_wind_weekly (
                        id INTEGER,
                        rdate TEXT,
                        week_id INTEGER,
                        indicators_code TEXT,
                        indicators_value REAL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE api_wind_derivative_weekly (
                        week_id INTEGER,
                        indicators_code TEXT,
                        indicators_value REAL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE api_wind_daily (
                        rdate TEXT,
                        week_id INTEGER,
                        indicators_code TEXT,
                        indicators_value REAL
                    )
                    """
                )
            )
        return engine

    def test_readiness_reports_missing_required_codes_for_feature_week(self) -> None:
        from scripts.check_weekly_10y_readiness import assess_weekly_10y_readiness

        engine = self._engine_with_weekly_rows()
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO api_wind_derivative_weekly
                        (week_id, indicators_code, indicators_value)
                    VALUES
                        (202620, 'TB0YWI3C', 1.70),
                        (202620, 'TB1YWI3C', 1.10),
                        (202620, 'TB5YWI3C', 1.40),
                        (202621, 'TB0YWI3C', 1.72)
                    """
                )
            )

        result = assess_weekly_10y_readiness(engine, "2026-05-30")

        self.assertFalse(result.ready)
        self.assertEqual(result.feature_week_id, 202621)
        self.assertEqual(result.feature_date, "2026-05-29")
        self.assertEqual(result.target_week_id, 202622)
        self.assertEqual(result.target_date, "2026-06-05")
        self.assertEqual(result.latest_complete_required_week_id, 202620)
        self.assertEqual(
            result.missing_required_values,
            [
                {"week_id": 202621, "indicators_code": "TB1YWI3C"},
                {"week_id": 202621, "indicators_code": "TB5YWI3C"},
            ],
        )

    def test_readiness_is_ready_when_feature_week_has_all_required_codes(self) -> None:
        from scripts.check_weekly_10y_readiness import assess_weekly_10y_readiness

        engine = self._engine_with_weekly_rows()
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO api_wind_derivative_weekly
                        (week_id, indicators_code, indicators_value)
                    VALUES
                        (202621, 'TB0YWI3C', 1.72),
                        (202621, 'TB1YWI3C', 1.11),
                        (202621, 'TB5YWI3C', 1.41)
                    """
                )
            )

        result = assess_weekly_10y_readiness(engine, "2026-05-30")

        self.assertTrue(result.ready)
        self.assertEqual(result.latest_complete_required_week_id, 202621)
        self.assertEqual(result.missing_required_values, [])

    def test_readiness_latest_supported_predict_date_uses_source_rdate(self) -> None:
        from scripts.check_weekly_10y_readiness import assess_weekly_10y_readiness

        engine = self._engine_with_weekly_rows()
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO api_wind_weekly
                        (id, rdate, week_id, indicators_code, indicators_value)
                    VALUES
                        (1, '2026-06-05', 202621, 'S0114089', 2726.48)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO api_wind_derivative_weekly
                        (week_id, indicators_code, indicators_value)
                    VALUES
                        (202621, 'TB0YWI3C', 1.7175),
                        (202621, 'TB1YWI3C', 1.1550),
                        (202621, 'TB5YWI3C', 1.4150)
                    """
                )
            )

        result = assess_weekly_10y_readiness(engine, "2026-06-06")

        self.assertTrue(result.ready)
        self.assertEqual(result.feature_week_id, 202621)
        self.assertEqual(result.latest_supported_predict_date, "2026-06-06")

    def test_readiness_requires_weekly_values_even_when_daily_close_exists(self) -> None:
        from scripts.check_weekly_10y_readiness import assess_weekly_10y_readiness

        engine = self._engine_with_weekly_rows()
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO api_wind_derivative_weekly
                        (week_id, indicators_code, indicators_value)
                    VALUES
                        (202620, 'TB0YWI3C', 1.70),
                        (202620, 'TB1YWI3C', 1.10),
                        (202620, 'TB5YWI3C', 1.40)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO api_wind_daily
                        (rdate, week_id, indicators_code, indicators_value)
                    VALUES
                        ('2026-05-25', 202621, 'TB0YWI0C', 1.71),
                        ('2026-05-29', 202621, 'TB0YWI0C', 1.72),
                        ('2026-05-29', 202621, 'TB1YWI0C', 1.11),
                        ('2026-05-29', 202621, 'TB5YWI0C', 1.41),
                        ('2026-06-01', 202622, 'TB0YWI0C', 9.99)
                    """
                )
            )

        result = assess_weekly_10y_readiness(engine, "2026-05-30")

        self.assertFalse(result.ready)
        self.assertEqual(result.latest_complete_required_week_id, 202620)
        self.assertEqual(
            result.missing_required_values,
            [
                {"week_id": 202621, "indicators_code": "TB0YWI3C"},
                {"week_id": 202621, "indicators_code": "TB1YWI3C"},
                {"week_id": 202621, "indicators_code": "TB5YWI3C"},
            ],
        )


class Weekly10YLiveWriterTests(unittest.TestCase):
    def test_live_writer_rejects_non_weekly_10y_scheme(self) -> None:
        from scripts.write_weekly_10y_live_prediction import validate_scheme_id

        with self.assertRaisesRegex(ValueError, "only supports weekly_10y_d_overlay"):
            validate_scheme_id("t1_daily")

    def test_live_writer_does_not_upsert_when_readiness_fails(self) -> None:
        from scripts.check_weekly_10y_readiness import Weekly10YReadinessResult
        from scripts.write_weekly_10y_live_prediction import write_weekly_10y_live_prediction

        readiness = Weekly10YReadinessResult(
            predict_date="2026-06-06",
            feature_week_id=202622,
            feature_date="2026-06-05",
            target_week_id=202623,
            target_date="2026-06-12",
            latest_complete_required_week_id=202620,
            latest_supported_predict_date="2026-05-23",
            ready=False,
            missing_required_values=[{"week_id": 202622, "indicators_code": "TB0YWI3C"}],
        )
        engine = object()
        upsert = Mock()
        run_log = Mock()
        dry_run = Mock(
            return_value=[
                PredictionRecord(
                    scheme_id="weekly_10y_d_overlay",
                    target_tenor="10Y",
                    horizon=6,
                    predict_date="2026-06-06",
                    target_date="2026-06-12",
                    predicted_direction=-1,
                    confidence=0.32,
                    model_version="test",
                )
            ]
        )

        with patch(
            "scripts.write_weekly_10y_live_prediction.assess_weekly_10y_readiness",
            return_value=readiness,
        ):
            with self.assertRaisesRegex(RuntimeError, "weekly source data is not ready"):
                write_weekly_10y_live_prediction(
                    predict_date="2026-06-06",
                    engine=engine,
                    dry_run_func=dry_run,
                    upsert_func=upsert,
                    run_log_func=run_log,
                )

        dry_run.assert_not_called()
        upsert.assert_not_called()
        run_log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
