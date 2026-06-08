from __future__ import annotations

import importlib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Weekly7YIntegrationTests(unittest.TestCase):
    def test_scheme_files_exist_and_config_declares_weekly_7y(self) -> None:
        scheme_dir = PROJECT_ROOT / "schemes" / "weekly_7y_cross_d_overlay"
        config_path = scheme_dir / "config.yaml"
        predict_path = scheme_dir / "predict.py"

        self.assertTrue(config_path.exists(), "weekly_7y_cross_d_overlay config.yaml must exist")
        self.assertTrue(predict_path.exists(), "weekly_7y_cross_d_overlay predict.py must exist")

        text = config_path.read_text(encoding="utf-8")
        self.assertIn("scheme_id: weekly_7y_cross_d_overlay", text)
        self.assertIn("frequency: weekly", text)
        self.assertIn('tenors: ["7Y"]', text)
        self.assertIn('cron: "30 11 * * 6"', text)
        self.assertIn("status: paused", text)

    def test_weekly_7y_predictor_uses_cross_d_overlay_columns(self) -> None:
        module = importlib.import_module("schemes.weekly_7y_cross_d_overlay.core.predictors")
        weekly_df = _weekly_fixture()

        result = module.predict_w7y(weekly_df, rdate="2026-02-21", target_week_id=202608)

        self.assertEqual(result.tenor, "7Y")
        self.assertEqual(result.week_id, 202608)
        self.assertIn(result.pred_label, (-1, 1))
        self.assertTrue(0.0 <= result.prob_up <= 1.0)
        self.assertEqual(result.prediction_column, "cross_d_pred_label")
        self.assertEqual(result.probability_column, "cross_d_prob_up")
        self.assertIn("7y_cross_d_overlay_0529", result.source)
        self.assertIn("seven_year_1y_3y_spread_reversal_3w", result.source_spec)

    def test_weekly_7y_adapter_uses_common_weekly_input_artifact(self) -> None:
        module = importlib.import_module("schemes.weekly_7y_cross_d_overlay.predict")
        artifact = SimpleNamespace(
            dataframe=_weekly_fixture(),
            path=Path("/tmp/weekly_output.csv"),
            source="test_weekly_input_artifact",
        )
        fake_engine = SimpleNamespace(dispose=lambda: None)

        with patch.object(module, "create_sqlalchemy_engine", return_value=fake_engine):
            with patch.object(module, "read_source_week_id_for_date", return_value=202608):
                with patch.object(module, "build_weekly_input_artifact", return_value=artifact) as build:
                    records = module.run("2026-02-21")

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.scheme_id, "weekly_7y_cross_d_overlay")
        self.assertEqual(record.target_tenor, "7Y")
        self.assertEqual(record.horizon, 6)
        self.assertEqual(record.predict_date, "2026-02-21")
        self.assertEqual(record.target_date, "2026-02-27")
        self.assertIn(record.predicted_direction, (-1, 1))
        self.assertEqual(record.extra["feature_week_id"], 202608)
        self.assertEqual(record.extra["target_week_id"], 202609)
        self.assertEqual(record.extra["input_artifact_source"], "test_weekly_input_artifact")
        self.assertEqual(build.call_args.kwargs["scheme_id"], "weekly_7y_cross_d_overlay")
        self.assertEqual(build.call_args.kwargs["end_week"], 202608)

    def test_weekly_7y_backtest_rows_follow_saturday_prediction_rule(self) -> None:
        module = importlib.import_module("backtests.weekly_7y_cross_d_overlay_reproduction")
        predictions = pd.DataFrame(
            [
                {
                    "week_id": 202608,
                    "week_date": pd.Timestamp("2026-02-20"),
                    "actual_label": -1,
                    "future_return": -0.001,
                    "cross_d_pred_label": -1,
                    "cross_d_prob_up": 0.45,
                    "main_pred_label": 1,
                    "main_prob_up": 0.525,
                    "cross_d_overlay": True,
                    "cross_d_signal_source": "5y_d_down_overlay",
                    "label_overlay_applied": False,
                }
            ]
        )

        rows = module.prediction_frame_to_backtest_rows(predictions)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["scheme_id"], "weekly_7y_cross_d_overlay")
        self.assertEqual(rows[0]["target_tenor"], "7Y")
        self.assertEqual(rows[0]["predict_date"], "2026-02-21")
        self.assertEqual(rows[0]["feature_date"], "2026-02-20")
        self.assertEqual(rows[0]["target_date"], "2026-02-27")
        self.assertEqual(rows[0]["horizon"], 6)
        self.assertEqual(rows[0]["predicted_direction"], -1)
        self.assertEqual(rows[0]["extra"]["feature_week_id"], 202608)
        self.assertEqual(rows[0]["extra"]["target_week_id"], 202609)

    def test_weekly_7y_backtest_uses_common_weekly_input_artifact(self) -> None:
        module = importlib.import_module("backtests.weekly_7y_cross_d_overlay_reproduction")
        predictions = pd.DataFrame(
            [
                {
                    "week_id": 202608,
                    "week_date": pd.Timestamp("2026-02-20"),
                    "actual_label": -1,
                    "future_return": -0.001,
                    "cross_d_pred_label": -1,
                    "cross_d_prob_up": 0.45,
                    "main_pred_label": 1,
                    "main_prob_up": 0.525,
                    "cross_d_overlay": True,
                    "cross_d_signal_source": "5y_d_down_overlay",
                    "label_overlay_applied": False,
                }
            ]
        )
        artifact = SimpleNamespace(
            dataframe=_weekly_fixture(),
            path=Path("/tmp/weekly_output_historical_backtest.csv"),
            source="test_common_weekly_input_artifact",
        )
        fake_engine = SimpleNamespace()

        with patch.object(module, "build_weekly_input_artifact", return_value=artifact) as build:
            with patch.object(module, "build_weekly_7y_predictions", return_value=predictions) as predict:
                payload = module.run_weekly_7y_cross_d_overlay_reproduction(engine=fake_engine, persist=False)

        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["summary"]["weekly_input_artifact_source"], "test_common_weekly_input_artifact")
        self.assertEqual(payload["summary"]["weekly_input_artifact_path"], str(artifact.path))
        self.assertIs(predict.call_args.args[0], artifact.dataframe)
        self.assertEqual(build.call_args.kwargs["scheme_id"], "weekly_7y_cross_d_overlay")
        self.assertEqual(build.call_args.kwargs["predict_date"], "historical_backtest")


def _weekly_fixture() -> pd.DataFrame:
    rows = []
    for offset, week_id in enumerate(range(202601, 202610), start=1):
        rows.append(
            {
                "week_id": week_id,
                "TB1YWI3C": 1.00 + offset * 0.02,
                "TB3YWI3C": 1.30 - offset * 0.01,
                "TB5YWI3C": 1.55 + offset * 0.015,
                "TB7YWI3C": 1.70 + offset * 0.005,
                "TB0YWI3C": 1.90 + offset * 0.01,
            }
        )
    return pd.DataFrame(rows)


if __name__ == "__main__":
    unittest.main()
