"""liwei_0616 7Y_01 SAY 共识方案回测 runner 测试。"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd


SCHEME_ID = "liwei_0616_7y01_cons_say_k3_div_k10"


class Liwei06167Y01BacktestTests(unittest.TestCase):
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
            "vote_score",
            "STD_score",
            "STD_dir",
            "ACCWT_score",
            "ACCWT_dir",
            "CROSS_5Y_score",
            "CROSS_5Y_dir",
            "DIV_score",
            "DIV_dir",
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
        self.assertEqual(set(original["target_tenor"]), {"7Y"})
        self.assertEqual(int((pd.to_datetime(original["target_date"]) < pd.Timestamp("2026-06-01")).sum()), 13)
        self.assertEqual(int((pd.to_datetime(original["target_date"]) >= pd.Timestamp("2026-06-01")).sum()), 8)
        self.assertEqual(int((original["direction"] == 0).sum()), 5)
        self.assertTrue((original.loc[original["direction"] == 0, "confidence"] == 0.0).all())
        self.assertTrue((original.loc[original["direction"] != 0, "confidence"] == 1.0).all())
        self.assertEqual(int(original.loc[original["feature_date"] == "2026-05-18", "CROSS_5Y_dir"].iloc[0]), 1)
        self.assertAlmostEqual(
            float(original.loc[original["feature_date"] == "2026-05-18", "CROSS_5Y_score"].iloc[0]),
            0.008396337253219598,
        )
        merged = original.merge(
            current,
            on=["feature_date", "target_date", "target_tenor", "horizon"],
            suffixes=("_original", "_current"),
        )
        self.assertEqual(len(merged), len(original))
        self.assertTrue((merged["direction_original"] == merged["direction_current"]).all())
        self.assertTrue((merged["label_original"] == merged["label_current"]).all())
        self.assertTrue((merged["confidence_original"] == merged["confidence_current"]).all())
        internal_columns = [name for name in expected_columns if name.endswith(("_score", "_dir")) or name == "vote_score"]
        for column in internal_columns:
            if column.endswith("_score") or column == "vote_score":
                max_abs = (merged[f"{column}_original"] - merged[f"{column}_current"]).abs().max()
                self.assertLessEqual(float(max_abs), 1e-8, column)
            else:
                self.assertTrue((merged[f"{column}_original"] == merged[f"{column}_current"]).all(), column)

        for summary_name in ("original_backtest_summary.json", "current_backtest_summary.json"):
            summary = pd.read_json(bench / summary_name, typ="series")
            self.assertEqual(summary["scheme_id"], SCHEME_ID)
            self.assertEqual(summary["source_model_id"], "7Y_01_cons_SAY_k_3_DIV_K_10")
            self.assertEqual(int(summary["row_count"]), 21)
            self.assertEqual(int(summary["samples"]), 21)
            self.assertEqual(int(summary["metric_samples"]), 16)
            self.assertEqual(int(summary["correct"]), 12)
            self.assertEqual(int(summary["backtest_rows_before_live_cutoff"]), 13)
            self.assertEqual(int(summary["gray_live_boundary_rows"]), 8)
            self.assertIn("feature_date", summary["source_month_bucket"])
            self.assertIn("target_date", summary["platform_month_bucket"])

    def test_build_backtest_rows_use_feature_date_and_target_cutoff(self) -> None:
        from backtests import liwei_0616_7y01_cons_say_k3_div_k10_reproduction as runner

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
        self.assertEqual(rows[0]["target_tenor"], "7Y")
        self.assertEqual(rows[0]["horizon"], 5)
        self.assertEqual(rows[0]["predicted_direction"], -1)
        self.assertEqual(rows[0]["confidence"], 1.0)
        self.assertEqual(rows[0]["extra"]["source_model_id"], "7Y_01_cons_SAY_k_3_DIV_K_10")

    def test_sample_input_end_uses_sample_target_date_not_global_backtest_end(self) -> None:
        from backtests import liwei_0616_7y01_cons_say_k3_div_k10_reproduction as runner

        calendar = MagicMock()
        calendar.nth_trading_day_after.side_effect = lambda day, horizon: {
            ("2026-03-25", 5): "2026-04-01",
            ("2026-04-23", 5): "2026-04-30",
        }[(day, horizon)]

        input_end = runner._effective_input_end(["2026-03-25", "2026-04-23"], calendar)

        self.assertEqual(input_end, "2026-04-30")

    def test_explicit_input_end_can_pin_source_latest_oos_context(self) -> None:
        from backtests import liwei_0616_7y01_cons_say_k3_div_k10_reproduction as runner

        calendar = MagicMock()

        input_end = runner._effective_input_end(["2026-05-06"], calendar, requested_input_end="2026-06-10")

        self.assertEqual(input_end, "2026-06-10")
        calendar.nth_trading_day_after.assert_not_called()

    @patch("backtests.liwei_0616_7y01_cons_say_k3_div_k10_reproduction.run_liwei_0616_7y01_cons_say_k3_div_k10_reproduction")
    def test_main_forwards_input_end_to_runner(self, mock_run: MagicMock) -> None:
        from backtests import liwei_0616_7y01_cons_say_k3_div_k10_reproduction as runner

        mock_run.return_value = {"status": "success", "row_count": 1}
        stream = io.StringIO()
        with patch(
            "sys.argv",
            ["runner", "--no-persist", "--batch-mode", "monthly", "--input-end", "2026-06-10"],
        ), redirect_stdout(stream):
            runner.main()

        self.assertEqual(mock_run.call_args.kwargs["batch_mode"], "monthly")
        self.assertEqual(mock_run.call_args.kwargs["input_end"], "2026-06-10")

    @patch("backtests.liwei_0616_7y01_cons_say_k3_div_k10_reproduction.run_liwei_0616_7y01_cons_say_k3_div_k10_reproduction")
    def test_main_forwards_phase_a_cache_to_runner(self, mock_run: MagicMock) -> None:
        from backtests import liwei_0616_7y01_cons_say_k3_div_k10_reproduction as runner

        mock_run.return_value = {"status": "success", "row_count": 1}
        stream = io.StringIO()
        with patch("sys.argv", ["runner", "--no-persist", "--phase-a-cache"]), redirect_stdout(stream):
            runner.main()

        self.assertTrue(mock_run.call_args.kwargs["use_phase_a_cache"])

    @patch("backtests.liwei_0616_7y01_cons_say_k3_div_k10_reproduction.run_7y01_for_window_silent")
    def test_historical_prediction_runs_each_feature_date_as_pit_cutoff(self, mock_runner: MagicMock) -> None:
        from backtests import liwei_0616_7y01_cons_say_k3_div_k10_reproduction as runner

        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-05-18", "2026-05-19", "2026-05-20"]),
                "TB7YWI0C": [2.0, 2.01, 2.02],
            }
        )

        def fake_window(**kwargs):
            feature_date = kwargs["feature_date"]
            return pd.DataFrame(
                {
                    "anchor_date": [feature_date],
                    "prediction": [1],
                    "true_label": [1],
                    "confidence": [1.0],
                }
            )

        mock_runner.side_effect = fake_window

        detail = runner.run_historical_prediction(
            daily_df=daily_df,
            weekly_df=pd.DataFrame({"week_id": [202620]}),
            monthly_df=pd.DataFrame({"month_id": ["202605"]}),
            date_to_week={},
            n_workers=1,
        )

        self.assertEqual(detail["anchor_date"].tolist(), ["2026-05-18", "2026-05-19", "2026-05-20"])
        self.assertEqual(mock_runner.call_count, 3)
        called_feature_dates = [call.kwargs["feature_date"] for call in mock_runner.call_args_list]
        called_current_starts = [call.kwargs["current_start"] for call in mock_runner.call_args_list]
        called_current_ends = [call.kwargs["current_end"] for call in mock_runner.call_args_list]
        called_test_ranges = [call.kwargs["test_ranges"] for call in mock_runner.call_args_list]
        self.assertEqual(called_feature_dates, ["2026-05-18", "2026-05-19", "2026-05-20"])
        self.assertEqual(called_current_starts, ["2026-05-01"] * 3)
        self.assertEqual(called_current_ends, called_feature_dates)
        self.assertEqual(called_test_ranges, [(("2025-05-01", "2025-05-31"), ("2026-05-01", "2026-05-20"))] * 3)

    @patch("backtests.liwei_0616_7y01_cons_say_k3_div_k10_reproduction.run_7y01_for_window_silent")
    def test_targeted_monthly_sample_runs_one_source_context_across_feature_months(self, mock_runner: MagicMock) -> None:
        from backtests import liwei_0616_7y01_cons_say_k3_div_k10_reproduction as runner

        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-03-31", "2026-04-01", "2026-04-02"]),
                "TB7YWI0C": [2.0, 2.01, 2.02],
            }
        )

        def fake_window(**kwargs):
            return pd.DataFrame(
                {
                    "anchor_date": ["2026-03-31", "2026-04-01"],
                    "prediction": [1, -1],
                    "true_label": [1, -1],
                    "confidence": [1.0, 1.0],
                    "vote_score": [0.6, -0.6],
                }
            )

        mock_runner.side_effect = fake_window

        detail = runner.run_historical_prediction(
            daily_df=daily_df,
            weekly_df=pd.DataFrame({"week_id": [202613, 202614]}),
            monthly_df=pd.DataFrame({"month_id": ["202603", "202604"]}),
            date_to_week={},
            n_workers=1,
            feature_dates=["2026-03-31", "2026-04-01"],
            batch_mode="monthly",
        )

        self.assertEqual(detail["anchor_date"].tolist(), ["2026-03-31", "2026-04-01"])
        self.assertEqual(mock_runner.call_count, 1)
        kwargs = mock_runner.call_args.kwargs
        self.assertEqual(kwargs["feature_date"], "2026-04-01")
        self.assertEqual(kwargs["current_start"], "2026-03-01")
        self.assertEqual(kwargs["current_end"], "2026-04-01")
        self.assertEqual(kwargs["test_ranges"], (("2025-03-01", "2025-04-30"), ("2026-03-01", "2026-04-02")))

    @patch("backtests.liwei_0616_7y01_cons_say_k3_div_k10_reproduction._build_phase_a_caches")
    @patch("backtests.liwei_0616_7y01_cons_say_k3_div_k10_reproduction.run_7y01_for_window_silent")
    def test_full_monthly_mode_builds_phase_a_cache_once_and_reuses_for_groups(
        self,
        mock_runner: MagicMock,
        mock_build_cache: MagicMock,
    ) -> None:
        from backtests import liwei_0616_7y01_cons_say_k3_div_k10_reproduction as runner

        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-04-30", "2026-05-06"]),
                "TB7YWI0C": [2.0, 2.01],
            }
        )
        phase_a_caches = {"STD": {"cache": "std"}}
        mock_build_cache.return_value = phase_a_caches

        def fake_window(**kwargs):
            if kwargs["current_start"] == "2026-04-01":
                return pd.DataFrame({"anchor_date": ["2026-04-30"], "prediction": [1], "true_label": [1]})
            return pd.DataFrame({"anchor_date": ["2026-05-06"], "prediction": [-1], "true_label": [-1]})

        mock_runner.side_effect = fake_window

        detail = runner.run_historical_prediction(
            daily_df=daily_df,
            weekly_df=pd.DataFrame({"week_id": [202618, 202619]}),
            monthly_df=pd.DataFrame({"month_id": ["202604", "202605"]}),
            date_to_week={},
            n_workers=1,
            batch_mode="monthly",
            use_phase_a_cache=True,
        )

        self.assertEqual(detail["anchor_date"].tolist(), ["2026-04-30", "2026-05-06"])
        mock_build_cache.assert_called_once()
        self.assertEqual(mock_runner.call_count, 2)
        self.assertTrue(all(call.kwargs["phase_a_caches"] is phase_a_caches for call in mock_runner.call_args_list))

    @patch("backtests.liwei_0616_7y01_cons_say_k3_div_k10_reproduction.run_prediction")
    def test_build_phase_a_caches_redirects_source_logs_away_from_stdout(self, mock_run_prediction: MagicMock) -> None:
        import numpy as np

        from backtests import liwei_0616_7y01_cons_say_k3_div_k10_reproduction as runner

        def fake_run_prediction(cfg):
            print("source log on stdout")
            return np.array([0]), {"phase_a_cache": {"baseline": cfg["name"]}}

        mock_run_prediction.side_effect = fake_run_prediction
        stream = io.StringIO()
        with redirect_stdout(stream):
            caches = runner._build_phase_a_caches(
                dates=["2026-05-06"],
                daily_df=pd.DataFrame({"date": pd.to_datetime(["2026-05-06"]), "TB7YWI0C": [2.0]}),
                weekly_df=pd.DataFrame({"week_id": [202620]}),
                monthly_df=pd.DataFrame({"month_id": ["202605"]}),
                date_to_week={"2026-05-06": 202620},
                n_workers=1,
                model_context_end="2026-06-10",
            )

        self.assertEqual(stream.getvalue(), "")
        self.assertEqual(set(caches), {"STD", "ACCWT", "CROSS_5Y", "DIV"})

    def test_missing_calendar_target_date_does_not_fallback_to_anchor(self) -> None:
        from backtests import liwei_0616_7y01_cons_say_k3_div_k10_reproduction as runner

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

    @patch("backtests.liwei_0616_7y01_cons_say_k3_div_k10_reproduction.get_calendar")
    @patch("backtests.liwei_0616_7y01_cons_say_k3_div_k10_reproduction.run_historical_prediction")
    @patch("backtests.liwei_0616_7y01_cons_say_k3_div_k10_reproduction.build_monthly_input_artifact")
    @patch("backtests.liwei_0616_7y01_cons_say_k3_div_k10_reproduction.build_weekly_input_artifact")
    @patch("backtests.liwei_0616_7y01_cons_say_k3_div_k10_reproduction.build_daily_input_artifact")
    @patch("backtests.liwei_0616_7y01_cons_say_k3_div_k10_reproduction.create_sqlalchemy_engine")
    def test_run_no_persist_uses_all_three_artifacts_and_does_not_write(
        self,
        mock_engine_factory: MagicMock,
        mock_daily_builder: MagicMock,
        mock_weekly_builder: MagicMock,
        mock_monthly_builder: MagicMock,
        mock_historical: MagicMock,
        mock_get_calendar: MagicMock,
    ) -> None:
        from backtests import liwei_0616_7y01_cons_say_k3_div_k10_reproduction as runner

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
                "baseline_signs": [{"STD": -1, "ACCWT": -1, "CROSS_5Y": -1, "DIV": -1}],
                "baseline_scores": [{"STD": -0.7, "ACCWT": -0.8, "CROSS_5Y": -0.9, "DIV": -1.0}],
            }
        )

        payload = runner.run_liwei_0616_7y01_cons_say_k3_div_k10_reproduction(persist=False)

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["scheme_id"], SCHEME_ID)
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["rows"][0]["target_date"], "2026-05-29")
        self.assertEqual(payload["rows"][0]["vote_score"], -0.6)
        self.assertEqual(payload["rows"][0]["STD_score"], -0.7)
        self.assertEqual(payload["rows"][0]["STD_dir"], -1)
        self.assertEqual(payload["rows"][0]["CROSS_5Y_score"], -0.9)
        self.assertEqual(payload["runs"][0]["rows"], payload["rows"])
        calendar.nth_trading_day_after.assert_called_with("2026-05-22", runner.HORIZON)
        mock_daily_builder.assert_called_once()
        mock_weekly_builder.assert_called_once()
        mock_monthly_builder.assert_called_once()
        self.assertEqual(mock_weekly_builder.call_args.kwargs["as_of_date"], runner.BACKTEST_INPUT_END)
        engine.dispose.assert_called_once()

    @patch("backtests.liwei_0616_7y01_cons_say_k3_div_k10_reproduction.run_liwei_0616_7y01_cons_say_k3_div_k10_reproduction")
    def test_main_emits_json_object_for_backtest_gate(self, mock_run: MagicMock) -> None:
        from backtests import liwei_0616_7y01_cons_say_k3_div_k10_reproduction as runner

        mock_run.return_value = {"status": "success", "row_count": 1}
        stream = io.StringIO()
        with patch("sys.argv", ["runner", "--no-persist"]), redirect_stdout(stream):
            runner.main()

        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["row_count"], 1)


if __name__ == "__main__":
    unittest.main()
