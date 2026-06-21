"""liwei_0616 5Y_01 SDA 共识方案回测 runner 测试。"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd


SCHEME_ID = "liwei_0616_cons_sda_k3_div_k10"


class Liwei0616BacktestTests(unittest.TestCase):
    """历史回测 runner 与 strict benchmark 测试。"""

    def test_benchmark_files_use_strict_feature_target_schema_and_match(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        bench = project_root / "schemes" / SCHEME_ID / "benchmarks"
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

        original = pd.read_csv(bench / "original_predictions_sample.csv")
        current = pd.read_csv(bench / "current_predictions_sample.csv")

        self.assertEqual(list(original.columns), expected_columns)
        self.assertEqual(list(current.columns), expected_columns)
        self.assertEqual(len(original), 21)
        self.assertEqual(len(original), len(current))
        self.assertEqual(str(original["feature_date"].iloc[0]), "2026-05-06")
        self.assertEqual(str(original["feature_date"].iloc[-1]), "2026-06-03")
        self.assertEqual(str(original["target_date"].iloc[-1]), "2026-06-10")
        self.assertTrue((pd.to_datetime(original["target_date"]) >= pd.Timestamp("2026-06-01")).any())
        merged = original.merge(
            current,
            on=["feature_date", "target_date", "target_tenor", "horizon"],
            suffixes=("_original", "_current"),
        )
        self.assertEqual(len(merged), len(original))
        self.assertTrue((merged["direction_original"] == merged["direction_current"]).all())
        self.assertTrue((merged["label_original"] == merged["label_current"]).all())
        self.assertTrue((merged["confidence_original"] == merged["confidence_current"]).all())

        for summary_name in ("original_backtest_summary.json", "current_backtest_summary.json"):
            summary = pd.read_json(bench / summary_name, typ="series")
            self.assertEqual(int(summary["row_count"]), 21)
            self.assertEqual(int(summary["samples"]), 21)
            self.assertEqual(int(summary["metric_samples"]), 19)
            self.assertEqual(int(summary["correct"]), 12)

    def test_build_backtest_rows_use_feature_date_and_target_cutoff(self) -> None:
        from backtests import liwei_0616_cons_sda_k3_div_k10_reproduction as runner

        detail = pd.DataFrame(
            {
                "anchor_date": ["2024-12-31", "2026-05-22", "2026-05-25"],
                "prediction": [1, -1, 1],
                "true_label": [1, -1, 1],
                "confidence": [1.0, 1.0, 1.0],
                "vote_score": [0.8, -0.6, 0.7],
                "baseline_signs": [{"STD": 1}, {"STD": -1}, {"STD": 1}],
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
        self.assertEqual(rows[0]["scheme_id"], SCHEME_ID)
        self.assertEqual(rows[0]["predict_date"], "2026-05-22")
        self.assertEqual(rows[0]["feature_date"], "2026-05-22")
        self.assertEqual(rows[0]["target_date"], "2026-05-29")
        self.assertEqual(rows[0]["target_tenor"], "5Y")
        self.assertEqual(rows[0]["horizon"], 5)
        self.assertEqual(rows[0]["predicted_direction"], -1)
        self.assertEqual(rows[0]["confidence"], 1.0)
        self.assertEqual(rows[0]["extra"]["source_model_id"], "5Y_01_cons_SDA_k_3_DIV_K_10")

    def test_missing_calendar_target_date_does_not_fallback_to_anchor(self) -> None:
        from backtests import liwei_0616_cons_sda_k3_div_k10_reproduction as runner

        detail = pd.DataFrame(
            {
                "anchor_date": ["2026-05-29"],
                "prediction": [1],
                "true_label": [1],
                "confidence": [1.0],
            }
        )

        rows = runner.build_backtest_rows(
            detail,
            target_date_for_anchor=lambda anchor: None,
            daily_artifact=SimpleNamespace(path=Path("/tmp/daily.csv"), source="daily_source", data_version="daily_v"),
            weekly_artifact=SimpleNamespace(path=Path("/tmp/weekly.csv"), source="weekly_source", data_version="weekly_v"),
            monthly_artifact=SimpleNamespace(path=Path("/tmp/monthly.csv"), source="monthly_source", data_version="monthly_v"),
        )

        self.assertEqual(rows, [])

    @patch("backtests.liwei_0616_cons_sda_k3_div_k10_reproduction.get_calendar")
    @patch("backtests.liwei_0616_cons_sda_k3_div_k10_reproduction.run_historical_prediction")
    @patch("backtests.liwei_0616_cons_sda_k3_div_k10_reproduction.build_monthly_input_artifact")
    @patch("backtests.liwei_0616_cons_sda_k3_div_k10_reproduction.build_weekly_input_artifact")
    @patch("backtests.liwei_0616_cons_sda_k3_div_k10_reproduction.build_daily_input_artifact")
    @patch("backtests.liwei_0616_cons_sda_k3_div_k10_reproduction.create_sqlalchemy_engine")
    def test_run_no_persist_uses_all_three_artifacts_and_does_not_write(
        self,
        mock_engine_factory: MagicMock,
        mock_daily_builder: MagicMock,
        mock_weekly_builder: MagicMock,
        mock_monthly_builder: MagicMock,
        mock_historical: MagicMock,
        mock_get_calendar: MagicMock,
    ) -> None:
        from backtests import liwei_0616_cons_sda_k3_div_k10_reproduction as runner

        engine = MagicMock()
        mock_engine_factory.return_value = engine
        calendar = mock_get_calendar.return_value
        calendar.week_id_for_date.return_value = 202621
        calendar.nth_trading_day_after.return_value = "2026-05-29"
        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(
                    ["2026-05-22", "2026-05-25", "2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29"]
                ),
                "TB5YWI0C": [2.0, 2.01, 2.02, 2.03, 2.04, 2.05],
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
                "baseline_signs": [{"STD": -1, "DIV": -1, "ACCWT": -1}],
            }
        )

        payload = runner.run_liwei_0616_cons_sda_k3_div_k10_reproduction(persist=False)

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["scheme_id"], SCHEME_ID)
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["rows"][0]["target_date"], "2026-05-29")
        self.assertEqual(payload["runs"][0]["rows"], payload["rows"])
        calendar.nth_trading_day_after.assert_called_with("2026-05-22", runner.HORIZON)
        mock_daily_builder.assert_called_once()
        mock_weekly_builder.assert_called_once()
        mock_monthly_builder.assert_called_once()
        self.assertEqual(mock_weekly_builder.call_args.kwargs["as_of_date"], runner.BACKTEST_INPUT_END)
        engine.dispose.assert_called_once()

    @patch("backtests.liwei_0616_cons_sda_k3_div_k10_reproduction.run_liwei_0616_cons_sda_k3_div_k10_reproduction")
    def test_main_emits_json_object_for_backtest_gate(self, mock_run: MagicMock) -> None:
        from backtests import liwei_0616_cons_sda_k3_div_k10_reproduction as runner

        mock_run.return_value = {"status": "success", "row_count": 1}
        stream = io.StringIO()
        with patch("sys.argv", ["runner", "--no-persist"]), redirect_stdout(stream):
            runner.main()

        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["row_count"], 1)


if __name__ == "__main__":
    unittest.main()
