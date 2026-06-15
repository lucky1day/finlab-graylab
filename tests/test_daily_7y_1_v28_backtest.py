"""daily_7y_1_v28 回测 runner 测试。"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd


class Daily7Y1BacktestTests(unittest.TestCase):
    """日频 7Y_1 v28 历史回测 runner 测试。"""

    def test_benchmark_files_use_may_auxiliary_source_scope(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        bench = project_root / "schemes" / "daily_7y_1_v28" / "benchmarks"

        original = pd.read_csv(bench / "original_predictions_sample.csv")
        current = pd.read_csv(bench / "current_predictions_sample.csv")
        expected_columns = [
            "feature_date",
            "target_date",
            "target_tenor",
            "horizon",
            "direction",
            "confidence",
            "label",
            "is_correct",
        ]

        self.assertEqual(list(original.columns), expected_columns)
        self.assertEqual(list(current.columns), expected_columns)
        self.assertEqual(len(original), 18)
        self.assertEqual(len(current), 18)
        self.assertEqual(original["feature_date"].min(), "2026-05-06")
        self.assertEqual(original["feature_date"].max(), "2026-05-29")
        self.assertEqual(original["target_date"].iloc[0], "2026-05-13")
        self.assertEqual(original["target_date"].iloc[-1], "2026-06-05")

        merged = original.merge(
            current,
            on=["feature_date", "target_date", "target_tenor", "horizon"],
            suffixes=("_original", "_current"),
        )
        self.assertEqual(len(merged), 18)
        self.assertTrue((merged["direction_original"] == merged["direction_current"]).all())
        self.assertTrue((merged["label_original"] == merged["label_current"]).all())
        self.assertTrue((merged["confidence_original"] == merged["confidence_current"]).all())

        summary = pd.read_json(bench / "original_backtest_summary.json", typ="series")
        self.assertEqual(summary["benchmark_scope"], "may2026_target_split_pit")
        self.assertEqual(summary["route_counts"]["backtest"], 13)
        self.assertEqual(summary["route_counts"]["live"], 5)
        self.assertEqual(summary["row_count"], 18)
        self.assertEqual(summary["eval_samples"], 18)
        self.assertAlmostEqual(float(summary["direction_accuracy"]), 12 / 18)
        self.assertEqual(summary["pred_up"], 6)
        self.assertEqual(summary["pred_down"], 12)
        self.assertEqual(summary["true_down"], 18)

    def test_backtest_end_covers_may_target_month_before_live_cutoff(self) -> None:
        from backtests import daily_7y_1_v28_reproduction as runner

        project_root = Path(__file__).resolve().parents[1]
        config_text = (project_root / "schemes" / "daily_7y_1_v28" / "config.yaml").read_text(encoding="utf-8")

        self.assertEqual(runner.BACKTEST_INPUT_END, "2026-05-29")
        self.assertEqual(runner.BACKTEST_END, "2026-05-29")
        self.assertEqual(runner.LIVE_TARGET_CUTOFF, "2026-06-01")
        self.assertIn('end_date: "2026-05-29"', config_text)

    def test_build_backtest_rows_use_anchor_predict_date_and_target_date_filter(self) -> None:
        from backtests import daily_7y_1_v28_reproduction as runner

        detail = pd.DataFrame(
            {
                "anchor_date": ["2024-12-31", "2026-05-22", "2026-05-25"],
                "prediction": [1, -1, 1],
                "true_label": [1, -1, 1],
                "confidence": [1.0, 1.0, 1.0],
                "vote_score": [0.8, -0.6, 0.7],
            }
        )
        target_dates = {
            "2024-12-31": "2025-01-08",
            "2026-05-22": "2026-05-29",
            "2026-05-25": "2026-06-01",
        }

        rows = runner.build_backtest_rows(
            detail,
            target_date_for_anchor=lambda anchor: target_dates[anchor],
            daily_artifact=SimpleNamespace(path=Path("/tmp/daily.csv"), source="daily_source", data_version="daily_v"),
            weekly_artifact=SimpleNamespace(path=Path("/tmp/weekly.csv"), source="weekly_source", data_version="weekly_v"),
            monthly_artifact=SimpleNamespace(path=Path("/tmp/monthly.csv"), source="monthly_source", data_version="monthly_v"),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["predict_date"], "2026-05-22")
        self.assertEqual(rows[0]["feature_date"], "2026-05-22")
        self.assertEqual(rows[0]["target_date"], "2026-05-29")
        self.assertEqual(rows[0]["target_tenor"], "7Y")
        self.assertEqual(rows[0]["horizon"], 5)
        self.assertEqual(rows[0]["predicted_direction"], -1)
        self.assertEqual(rows[0]["extra"]["weekly_input_artifact_source"], "weekly_source")
        self.assertEqual(rows[0]["extra"]["monthly_input_artifact_source"], "monthly_source")

    def test_target_date_from_daily_does_not_fallback_to_anchor(self) -> None:
        from backtests import daily_7y_1_v28_reproduction as runner

        daily_df = pd.DataFrame({"date": pd.to_datetime(["2026-05-29"])})

        self.assertIsNone(runner._target_date_from_daily(daily_df, "2026-05-29"))

    @patch("backtests.daily_7y_1_v28_reproduction.get_calendar")
    @patch("backtests.daily_7y_1_v28_reproduction.run_historical_prediction")
    @patch("backtests.daily_7y_1_v28_reproduction.build_monthly_input_artifact")
    @patch("backtests.daily_7y_1_v28_reproduction.build_weekly_input_artifact")
    @patch("backtests.daily_7y_1_v28_reproduction.build_daily_input_artifact")
    @patch("backtests.daily_7y_1_v28_reproduction.create_sqlalchemy_engine")
    def test_run_no_persist_uses_all_three_input_artifacts_without_writes(
        self,
        mock_engine_factory: MagicMock,
        mock_daily_builder: MagicMock,
        mock_weekly_builder: MagicMock,
        mock_monthly_builder: MagicMock,
        mock_historical: MagicMock,
        mock_get_calendar: MagicMock,
    ) -> None:
        from backtests import daily_7y_1_v28_reproduction as runner

        engine = MagicMock()
        mock_engine_factory.return_value = engine
        mock_get_calendar.return_value.week_id_for_date.return_value = 202621
        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(
                    ["2026-05-22", "2026-05-25", "2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29"]
                ),
                "TB7YWI0C": [2.0, 2.01, 2.02, 2.03, 2.04, 2.05],
            }
        )
        mock_daily_builder.return_value = SimpleNamespace(
            dataframe=daily_df,
            path=Path("/tmp/daily.csv"),
            source="daily_source",
            data_version="daily_v",
        )
        mock_weekly_builder.return_value = SimpleNamespace(
            dataframe=pd.DataFrame({"week_id": [202621]}),
            path=Path("/tmp/weekly.csv"),
            source="weekly_source",
            data_version="weekly_v",
        )
        mock_monthly_builder.return_value = SimpleNamespace(
            dataframe=pd.DataFrame({"month_id": ["202504"]}),
            path=Path("/tmp/monthly.csv"),
            source="monthly_source",
            data_version="monthly_v",
        )
        mock_historical.return_value = pd.DataFrame(
            {
                "anchor_date": ["2026-05-22"],
                "prediction": [-1],
                "true_label": [-1],
                "confidence": [1.0],
                "vote_score": [-0.6],
            }
        )

        payload = runner.run_daily_7y_1_v28_reproduction(persist=False)

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["scheme_id"], "daily_7y_1_v28")
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["runs"][0]["rows"], payload["rows"])
        mock_daily_builder.assert_called_once()
        mock_weekly_builder.assert_called_once()
        mock_monthly_builder.assert_called_once()
        self.assertEqual(mock_weekly_builder.call_args.kwargs["end_week"], 202621)
        self.assertEqual(mock_weekly_builder.call_args.kwargs["as_of_date"], runner.BACKTEST_INPUT_END)
        engine.dispose.assert_called_once()


if __name__ == "__main__":
    unittest.main()
