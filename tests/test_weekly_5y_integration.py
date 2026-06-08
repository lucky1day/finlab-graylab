from __future__ import annotations

import importlib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Weekly5YIntegrationTests(unittest.TestCase):
    def test_scheme_files_exist_and_config_declares_weekly_5y(self) -> None:
        scheme_dir = PROJECT_ROOT / "schemes" / "weekly_5y_direct_production"
        config_path = scheme_dir / "config.yaml"
        predict_path = scheme_dir / "predict.py"

        self.assertTrue(config_path.exists(), "weekly_5y_direct_production config.yaml must exist")
        self.assertTrue(predict_path.exists(), "weekly_5y_direct_production predict.py must exist")

        text = config_path.read_text(encoding="utf-8")
        self.assertIn("scheme_id: weekly_5y_direct_production", text)
        self.assertIn("frequency: weekly", text)
        self.assertIn('tenors: ["5Y"]', text)
        self.assertIn('cron: "30 11 * * 6"', text)
        self.assertIn("status: paused", text)

    def test_direct_5y_predictor_votes_from_required_weekly_columns(self) -> None:
        module = importlib.import_module("schemes.weekly_5y_direct_production.core.predictors")
        weekly_df = pd.DataFrame(
            [
                {"week_id": 202601, "TB1YWI3C": 1.0, "TB5YWI3C": 2.0, "TB7YWI3C": 2.0, "TB0YWI3C": 2.0},
                {"week_id": 202602, "TB1YWI3C": 1.0, "TB5YWI3C": 2.0, "TB7YWI3C": 2.0, "TB0YWI3C": 2.0},
                {"week_id": 202603, "TB1YWI3C": 1.0, "TB5YWI3C": 2.1, "TB7YWI3C": 2.2, "TB0YWI3C": 2.0},
                {"week_id": 202604, "TB1YWI3C": 1.0, "TB5YWI3C": 2.1, "TB7YWI3C": 2.2, "TB0YWI3C": 2.0},
                {"week_id": 202605, "TB1YWI3C": 1.2, "TB5YWI3C": 1.9, "TB7YWI3C": 2.4, "TB0YWI3C": 2.0},
            ]
        )

        result = module.predict_w5y(weekly_df, rdate="2026-02-07", target_week_id=202605)

        self.assertEqual(result.tenor, "5Y")
        self.assertEqual(result.week_id, 202605)
        self.assertEqual(result.pred_label, 1)
        self.assertAlmostEqual(result.prob_up, 0.55)
        self.assertEqual(result.prediction_column, "final_pred_label")
        self.assertEqual(result.probability_column, "final_prob_up")
        self.assertIn("five_year_1y_momentum_4w", result.source_spec)

    def test_weekly_5y_adapter_uses_common_weekly_input_artifact(self) -> None:
        module = importlib.import_module("schemes.weekly_5y_direct_production.predict")
        weekly_df = pd.DataFrame(
            [
                {"week_id": 202601, "TB1YWI3C": 1.0, "TB5YWI3C": 2.0, "TB7YWI3C": 2.0, "TB0YWI3C": 2.0},
                {"week_id": 202602, "TB1YWI3C": 1.0, "TB5YWI3C": 2.0, "TB7YWI3C": 2.0, "TB0YWI3C": 2.0},
                {"week_id": 202603, "TB1YWI3C": 1.0, "TB5YWI3C": 2.1, "TB7YWI3C": 2.2, "TB0YWI3C": 2.0},
                {"week_id": 202604, "TB1YWI3C": 1.0, "TB5YWI3C": 2.1, "TB7YWI3C": 2.2, "TB0YWI3C": 2.0},
                {"week_id": 202605, "TB1YWI3C": 1.2, "TB5YWI3C": 1.9, "TB7YWI3C": 2.4, "TB0YWI3C": 2.0},
            ]
        )
        artifact = SimpleNamespace(
            dataframe=weekly_df,
            path=Path("/tmp/weekly_output.csv"),
            source="test_weekly_input_artifact",
        )
        fake_engine = SimpleNamespace(dispose=lambda: None)

        with patch.object(module, "create_sqlalchemy_engine", return_value=fake_engine):
            with patch.object(module, "read_source_week_id_for_date", return_value=202605):
                with patch.object(module, "build_weekly_input_artifact", return_value=artifact) as build:
                    records = module.run("2026-02-07")

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.scheme_id, "weekly_5y_direct_production")
        self.assertEqual(record.target_tenor, "5Y")
        self.assertEqual(record.horizon, 6)
        self.assertEqual(record.predict_date, "2026-02-07")
        self.assertEqual(record.target_date, "2026-02-13")
        self.assertEqual(record.predicted_direction, 1)
        self.assertEqual(record.extra["feature_week_id"], 202605)
        self.assertEqual(record.extra["target_week_id"], 202606)
        self.assertEqual(record.extra["input_artifact_source"], "test_weekly_input_artifact")
        self.assertEqual(build.call_args.kwargs["scheme_id"], "weekly_5y_direct_production")
        self.assertEqual(build.call_args.kwargs["end_week"], 202605)

    def test_weekly_5y_backtest_rows_follow_saturday_prediction_rule(self) -> None:
        module = importlib.import_module("backtests.weekly_5y_direct_production_reproduction")
        predictions = pd.DataFrame(
            [
                {
                    "week_id": 202605,
                    "week_date": pd.Timestamp("2026-02-06"),
                    "actual_label": -1,
                    "final_pred_label": -1,
                    "final_prob_up": 0.45,
                    "future_return": -0.001,
                    "rule_vote": -3.0,
                    "source_spec": "rule_a;rule_b;rule_c",
                    "score_spec": "rule_a:1.0000;rule_b:1.0000;rule_c:1.0000",
                }
            ]
        )

        rows = module.prediction_frame_to_backtest_rows(predictions)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["scheme_id"], "weekly_5y_direct_production")
        self.assertEqual(rows[0]["target_tenor"], "5Y")
        self.assertEqual(rows[0]["predict_date"], "2026-02-07")
        self.assertEqual(rows[0]["feature_date"], "2026-02-06")
        self.assertEqual(rows[0]["target_date"], "2026-02-13")
        self.assertEqual(rows[0]["horizon"], 6)
        self.assertEqual(rows[0]["predicted_direction"], -1)
        self.assertEqual(rows[0]["extra"]["feature_week_id"], 202605)
        self.assertEqual(rows[0]["extra"]["target_week_id"], 202606)

    def test_weekly_5y_backtest_uses_legacy_week_dates(self) -> None:
        module = importlib.import_module("backtests.weekly_5y_direct_production_reproduction")
        weekly_df = pd.DataFrame(
            [
                {"week_id": 202553, "TB1YWI3C": 1.0, "TB5YWI3C": 2.0, "TB7YWI3C": 2.0, "TB0YWI3C": 2.0},
                {"week_id": 202601, "TB1YWI3C": 1.0, "TB5YWI3C": 1.9, "TB7YWI3C": 2.0, "TB0YWI3C": 2.0},
                {"week_id": 202602, "TB1YWI3C": 1.0, "TB5YWI3C": 1.8, "TB7YWI3C": 2.0, "TB0YWI3C": 2.0},
                {"week_id": 202603, "TB1YWI3C": 1.0, "TB5YWI3C": 1.7, "TB7YWI3C": 2.0, "TB0YWI3C": 2.0},
                {"week_id": 202604, "TB1YWI3C": 1.2, "TB5YWI3C": 1.6, "TB7YWI3C": 2.1, "TB0YWI3C": 2.0},
                {"week_id": 202618, "TB1YWI3C": 1.3, "TB5YWI3C": 1.5, "TB7YWI3C": 2.2, "TB0YWI3C": 2.0},
            ]
        )

        predictions = module.build_weekly_5y_predictions(weekly_df)
        dates = {
            int(row.week_id): pd.Timestamp(row.week_date).strftime("%Y-%m-%d")
            for row in predictions.itertuples()
        }

        self.assertEqual(module.legacy_week_id_to_friday(202553).strftime("%Y-%m-%d"), "2026-01-04")
        self.assertEqual(dates[202618], "2026-05-01")

    def test_weekly_5y_monthly_metrics_bucket_by_feature_month(self) -> None:
        module = importlib.import_module("backtests.weekly_5y_direct_production_reproduction")
        base = {
            "benchmark_id": "model_muti_0529",
            "scheme_id": "weekly_5y_direct_production",
            "target_tenor": "5Y",
            "horizon": 6,
            "model_pred": -1,
            "confidence": 0.45,
            "source_row": {},
            "extra": {"frequency": "weekly"},
        }
        rows = [
            {
                **base,
                "predict_date": "2025-10-25",
                "feature_date": "2025-10-24",
                "target_date": "2025-10-31",
                "label": -1,
                "predicted_direction": -1,
            },
            {
                **base,
                "predict_date": "2025-11-01",
                "feature_date": "2025-10-31",
                "target_date": "2025-11-07",
                "label": 1,
                "predicted_direction": -1,
            },
        ]

        output = module.make_weekly_run_output("2025-10-25", "2025-11-01", rows)
        metrics = {row["month"]: row for row in output.monthly_metrics}

        self.assertEqual(metrics["2025-10"]["sample_count"], 2)
        self.assertEqual(metrics["2025-10"]["correct_count"], 1)
        self.assertNotIn("2025-11", metrics)

    def test_weekly_5y_backtest_uses_common_weekly_input_artifact(self) -> None:
        module = importlib.import_module("backtests.weekly_5y_direct_production_reproduction")
        weekly_df = pd.DataFrame([{"week_id": 202605, "TB1YWI3C": 1.2, "TB5YWI3C": 1.9, "TB7YWI3C": 2.4, "TB0YWI3C": 2.0}])
        predictions = pd.DataFrame(
            [
                {
                    "week_id": 202605,
                    "week_date": pd.Timestamp("2026-02-06"),
                    "actual_label": -1,
                    "final_pred_label": -1,
                    "final_prob_up": 0.45,
                    "future_return": -0.001,
                    "rule_vote": -3.0,
                    "source_spec": "rule_a;rule_b;rule_c",
                    "score_spec": "rule_a:1.0000;rule_b:1.0000;rule_c:1.0000",
                }
            ]
        )
        artifact = SimpleNamespace(
            dataframe=weekly_df,
            path=Path("/tmp/weekly_output_historical_backtest.csv"),
            source="test_common_weekly_input_artifact",
        )
        fake_engine = SimpleNamespace()

        with patch.object(module, "build_weekly_input_artifact", return_value=artifact) as build:
            with patch.object(module, "build_weekly_5y_predictions", return_value=predictions) as predict:
                payload = module.run_weekly_5y_direct_production_reproduction(engine=fake_engine, persist=False)

        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["summary"]["weekly_input_artifact_source"], "test_common_weekly_input_artifact")
        self.assertEqual(payload["summary"]["weekly_input_artifact_path"], str(artifact.path))
        self.assertIs(predict.call_args.args[0], weekly_df)
        self.assertEqual(build.call_args.kwargs["scheme_id"], "weekly_5y_direct_production")
        self.assertEqual(build.call_args.kwargs["predict_date"], "historical_backtest")


if __name__ == "__main__":
    unittest.main()
