"""daily_5y_2_v28 回测 runner 测试。"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd


class Daily5Y2BacktestTests(unittest.TestCase):
    """日频 5Y_2 v28 历史回测 runner 测试。"""

    def test_benchmark_files_use_may_auxiliary_source_scope(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        bench = project_root / "schemes" / "daily_5y_2_v28" / "benchmarks"

        original = pd.read_csv(bench / "original_predictions_sample.csv")
        current = pd.read_csv(bench / "current_predictions_sample.csv")

        self.assertEqual(len(original), 18)
        self.assertEqual(len(current), 18)
        self.assertEqual(original["predict_date"].min(), "2026-05-06")
        self.assertEqual(original["predict_date"].max(), "2026-05-29")
        self.assertEqual(original["target_date"].iloc[0], "2026-05-13")
        self.assertEqual(original["target_date"].iloc[-1], "2026-06-05")

        merged = original.merge(
            current,
            on=["predict_date", "tenor"],
            suffixes=("_original", "_current"),
        )
        self.assertEqual(len(merged), 18)
        self.assertTrue((merged["direction_original"] == merged["direction_current"]).all())
        self.assertTrue((merged["label_original"] == merged["label_current"]).all())
        self.assertTrue((merged["confidence_original"] == merged["confidence_current"]).all())

        summary = pd.read_json(bench / "original_backtest_summary.json", typ="series")
        self.assertEqual(summary["benchmark_scope"], "may2026_auxiliary_source")
        self.assertEqual(summary["row_count"], 18)
        self.assertEqual(summary["eval_samples"], 14)

    def test_build_backtest_rows_use_anchor_predict_date_and_target_date_filter(self) -> None:
        from backtests import daily_5y_2_v28_reproduction as runner

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
        self.assertEqual(rows[0]["target_tenor"], "5Y")
        self.assertEqual(rows[0]["horizon"], 5)
        self.assertEqual(rows[0]["predicted_direction"], -1)
        self.assertEqual(rows[0]["extra"]["weekly_input_artifact_source"], "weekly_source")
        self.assertEqual(rows[0]["extra"]["monthly_input_artifact_source"], "monthly_source")

    @patch("backtests.daily_5y_2_v28_reproduction.run_historical_prediction")
    @patch("backtests.daily_5y_2_v28_reproduction.build_monthly_input_artifact")
    @patch("backtests.daily_5y_2_v28_reproduction.build_weekly_input_artifact")
    @patch("backtests.daily_5y_2_v28_reproduction.build_daily_input_artifact")
    @patch("backtests.daily_5y_2_v28_reproduction.create_sqlalchemy_engine")
    def test_run_no_persist_uses_all_three_input_artifacts_without_writes(
        self,
        mock_engine_factory: MagicMock,
        mock_daily_builder: MagicMock,
        mock_weekly_builder: MagicMock,
        mock_monthly_builder: MagicMock,
        mock_historical: MagicMock,
    ) -> None:
        from backtests import daily_5y_2_v28_reproduction as runner

        engine = MagicMock()
        mock_engine_factory.return_value = engine
        daily_df = pd.DataFrame({"date": pd.to_datetime(["2026-05-22"]), "TB5YWI0C": [2.0]})
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

        payload = runner.run_daily_5y_2_v28_reproduction(persist=False)

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["scheme_id"], "daily_5y_2_v28")
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["runs"][0]["rows"], payload["rows"])
        mock_daily_builder.assert_called_once()
        mock_weekly_builder.assert_called_once()
        mock_monthly_builder.assert_called_once()
        engine.dispose.assert_called_once()


if __name__ == "__main__":
    unittest.main()
