"""liwei_0616 7Y_03 ALL 共识方案回测 runner 测试。"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd


SCHEME_ID = "liwei_0616_7y03_cons_all_k3_div_k8"


class Liwei06167Y03BacktestTests(unittest.TestCase):
    """历史回测 runner、strict benchmark 与加速层测试。"""

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
        self.assertEqual(set(original["target_tenor"]), {"7Y"})
        self.assertEqual(int((pd.to_datetime(original["target_date"]) < pd.Timestamp("2026-06-01")).sum()), 13)
        self.assertEqual(int((pd.to_datetime(original["target_date"]) >= pd.Timestamp("2026-06-01")).sum()), 8)
        strict_pit_sensitive = original.loc[
            original["feature_date"].isin(["2026-05-18", "2026-05-19", "2026-05-20", "2026-05-22"]),
            ["feature_date", "direction", "confidence", "label", "is_correct"],
        ].to_dict("records")
        self.assertEqual(
            strict_pit_sensitive,
            [
                {"feature_date": "2026-05-18", "direction": 1, "confidence": 1.0, "label": -1, "is_correct": False},
                {"feature_date": "2026-05-19", "direction": 1, "confidence": 1.0, "label": -1, "is_correct": False},
                {"feature_date": "2026-05-20", "direction": 1, "confidence": 1.0, "label": -1, "is_correct": False},
                {"feature_date": "2026-05-22", "direction": 1, "confidence": 1.0, "label": -1, "is_correct": False},
            ],
        )
        self.assertEqual(int((original["direction"] == 0).sum()), 0)
        self.assertTrue((original.loc[original["direction"] == 0, "confidence"] == 0.0).all())
        self.assertTrue((original.loc[original["direction"] != 0, "confidence"] == 1.0).all())
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
            self.assertEqual(summary["scheme_id"], SCHEME_ID)
            self.assertEqual(summary["source_model_id"], "7Y_03_cons_ALL_k_3_DIV_K_8")
            self.assertEqual(summary["benchmark_scope"], "source_original_full_oos_targeted_sample")
            self.assertEqual(int(summary["row_count"]), 21)
            self.assertEqual(int(summary["samples"]), 21)
            self.assertEqual(int(summary["metric_samples"]), 21)
            self.assertEqual(int(summary["correct"]), 11)
            self.assertEqual(int(summary["no_trade"]), 0)
            self.assertEqual(int(summary["backtest_rows_before_live_cutoff"]), 13)
            self.assertEqual(int(summary["gray_live_boundary_rows"]), 8)
            self.assertEqual(summary["source_oos_start"], "2024-01-01")
            self.assertEqual(summary["source_end"], "2026-06-10")
            self.assertEqual(summary["source_execution_scope"], "single full-OOS test sequence from 2024-01-01 through source_end, then select target feature dates")
            self.assertEqual(summary["vote_baselines"], ["STD", "DIV", "ACCWT", "CROSS_5Y"])
            self.assertEqual(summary["fallback_baseline"], "DIV")
            self.assertEqual(int(summary["streak_K"]), 8)
            self.assertIn("feature_date", summary["source_month_bucket"])
            self.assertIn("target_date", summary["platform_month_bucket"])
            self.assertEqual(len(summary["source_batch_differences"]), 0)

    def test_build_backtest_rows_use_feature_date_and_target_cutoff(self) -> None:
        from backtests import liwei_0616_7y03_cons_all_k3_div_k8_reproduction as runner

        detail = pd.DataFrame(
            {
                "anchor_date": ["2024-12-31", "2026-05-22", "2026-05-25"],
                "prediction": [1, 0, 1],
                "true_label": [1, -1, 1],
                "confidence": [1.0, 0.0, 1.0],
                "vote_score": [0.8, 0.0, 0.7],
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
        self.assertEqual(rows[0]["predicted_direction"], 0)
        self.assertEqual(rows[0]["confidence"], 0.0)
        self.assertEqual(rows[0]["extra"]["source_model_id"], "7Y_03_cons_ALL_k_3_DIV_K_8")

    def test_full_historical_feature_dates_stop_before_live_target_cutoff(self) -> None:
        from backtests import liwei_0616_7y03_cons_all_k3_div_k8_reproduction as runner

        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-05-22", "2026-05-25", "2026-05-29"]),
                "TB7YWI0C": [2.0, 2.01, 2.02],
            }
        )

        dates = runner._historical_feature_dates(daily_df)

        self.assertEqual(dates, ["2026-05-22"])

    @patch("backtests.liwei_0616_7y03_cons_all_k3_div_k8_reproduction.run_7y03_for_window_silent")
    def test_historical_prediction_runs_each_feature_date_as_pit_cutoff(self, mock_runner: MagicMock) -> None:
        from backtests import liwei_0616_7y03_cons_all_k3_div_k8_reproduction as runner

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
            disable_cache=True,
            batch_mode="daily",
        )

        self.assertEqual(detail["anchor_date"].tolist(), ["2026-05-18", "2026-05-19", "2026-05-20"])
        self.assertEqual(mock_runner.call_count, 3)
        called_feature_dates = [call.kwargs["feature_date"] for call in mock_runner.call_args_list]
        called_current_starts = [call.kwargs["current_start"] for call in mock_runner.call_args_list]
        called_current_ends = [call.kwargs["current_end"] for call in mock_runner.call_args_list]
        called_test_ranges = [call.kwargs["test_ranges"] for call in mock_runner.call_args_list]
        self.assertEqual(called_feature_dates, ["2026-05-18", "2026-05-19", "2026-05-20"])
        self.assertEqual(called_current_starts, called_feature_dates)
        self.assertEqual(called_current_ends, called_feature_dates)
        self.assertEqual(called_test_ranges, [(("2024-01-01", "2026-05-20"),)] * 3)

    @patch("backtests.liwei_0616_7y03_cons_all_k3_div_k8_reproduction.run_7y03_for_window_silent")
    def test_monthly_batch_runs_one_window_for_same_month_and_returns_each_date(self, mock_runner: MagicMock) -> None:
        from backtests import liwei_0616_7y03_cons_all_k3_div_k8_reproduction as runner

        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-05-18", "2026-05-19", "2026-05-20"]),
                "TB7YWI0C": [2.0, 2.01, 2.02],
            }
        )

        def fake_window(**kwargs):
            current_end = kwargs["current_end"]
            dates = ["2026-05-18", "2026-05-19", "2026-05-20"]
            dates = [day for day in dates if day <= current_end]
            return pd.DataFrame(
                {
                    "anchor_date": dates,
                    "prediction": [-1 if day < "2026-05-20" else 1 for day in dates],
                    "true_label": [-1 if day < "2026-05-20" else 1 for day in dates],
                    "confidence": [1.0] * len(dates),
                    "vote_score": [-1.0 if day < "2026-05-20" else 1.0 for day in dates],
                }
            )

        mock_runner.side_effect = fake_window
        detail = runner.run_historical_prediction(
            daily_df=daily_df,
            weekly_df=pd.DataFrame({"week_id": [202620]}),
            monthly_df=pd.DataFrame({"month_id": ["202605"]}),
            date_to_week={},
            n_workers=1,
            feature_dates=["2026-05-18", "2026-05-19", "2026-05-20"],
            disable_cache=True,
            batch_mode="monthly",
        )

        self.assertEqual(detail["anchor_date"].tolist(), ["2026-05-18", "2026-05-19", "2026-05-20"])
        self.assertEqual(mock_runner.call_count, 1)
        self.assertEqual(mock_runner.call_args.kwargs["feature_date"], "2026-05-20")
        self.assertEqual(mock_runner.call_args.kwargs["current_start"], "2026-05-18")
        self.assertEqual(mock_runner.call_args.kwargs["current_end"], "2026-05-20")
        self.assertEqual(mock_runner.call_args.kwargs["test_ranges"], (("2024-01-01", "2026-05-20"),))

    @patch("backtests.liwei_0616_7y03_cons_all_k3_div_k8_reproduction.run_7y03_for_window_silent")
    def test_cache_on_off_and_sharded_paths_match_serial_on_targeted_dates(self, mock_runner: MagicMock) -> None:
        from backtests import liwei_0616_7y03_cons_all_k3_div_k8_reproduction as runner

        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-05-18", "2026-05-19", "2026-05-20"]),
                "TB7YWI0C": [2.0, 2.01, 2.02],
            }
        )

        def fake_window(**kwargs):
            feature_date = kwargs["feature_date"]
            value = -1 if feature_date < "2026-05-20" else 1
            return pd.DataFrame(
                {
                    "anchor_date": [feature_date],
                    "prediction": [value],
                    "true_label": [value],
                    "confidence": [1.0],
                    "vote_score": [float(value)],
                }
            )

        mock_runner.side_effect = fake_window
        kwargs = {
            "daily_df": daily_df,
            "weekly_df": pd.DataFrame({"week_id": [202620]}),
            "monthly_df": pd.DataFrame({"month_id": ["202605"]}),
            "date_to_week": {},
            "n_workers": 1,
            "feature_dates": ["2026-05-18", "2026-05-19", "2026-05-20"],
            "batch_mode": "daily",
        }
        with tempfile.TemporaryDirectory() as tmp:
            serial = runner.run_historical_prediction(**kwargs, disable_cache=True)
            cached_first = runner.run_historical_prediction(**kwargs, cache_dir=Path(tmp), cache_key_parts={"test": "v1"})
            cached_second = runner.run_historical_prediction(**kwargs, cache_dir=Path(tmp), cache_key_parts={"test": "v1"})
            sharded = runner.run_historical_prediction(
                **kwargs,
                parallel_shards=2,
                parallel_backend="inline",
                disable_cache=True,
            )

        self.assertEqual(serial.to_dict("records"), cached_first.to_dict("records"))
        self.assertEqual(serial.to_dict("records"), cached_second.to_dict("records"))
        self.assertEqual(serial.to_dict("records"), sharded.to_dict("records"))
        self.assertEqual(mock_runner.call_count, 9)

    def test_cache_key_separates_daily_and_monthly_batch_windows(self) -> None:
        from backtests import liwei_0616_7y03_cons_all_k3_div_k8_reproduction as runner

        cache_root = Path("/tmp/cache")
        daily_path = runner._cache_path(
            cache_root,
            "2026-05-18",
            {"artifact": "hash"},
            cache_mode="daily",
            window_end="2026-05-18",
            model_context_end="2026-05-20",
        )
        monthly_path = runner._cache_path(
            cache_root,
            "2026-05-18",
            {"artifact": "hash"},
            cache_mode="monthly",
            window_end="2026-05-22",
            model_context_end="2026-05-20",
        )

        self.assertNotEqual(daily_path, monthly_path)

    @patch("backtests.liwei_0616_7y03_cons_all_k3_div_k8_reproduction.run_7y03_for_window_silent")
    def test_monthly_batch_matches_daily_reference_on_mocked_targeted_dates(self, mock_runner: MagicMock) -> None:
        from backtests import liwei_0616_7y03_cons_all_k3_div_k8_reproduction as runner

        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-05-18", "2026-05-19", "2026-05-20"]),
                "TB7YWI0C": [2.0, 2.01, 2.02],
            }
        )

        def fake_window(**kwargs):
            dates = ["2026-05-18", "2026-05-19", "2026-05-20"]
            dates = [day for day in dates if day <= kwargs["current_end"]]
            return pd.DataFrame(
                {
                    "anchor_date": dates,
                    "prediction": [-1 if day < "2026-05-20" else 1 for day in dates],
                    "true_label": [-1 if day < "2026-05-20" else 1 for day in dates],
                    "confidence": [1.0] * len(dates),
                    "vote_score": [-1.0 if day < "2026-05-20" else 1.0 for day in dates],
                }
            )

        mock_runner.side_effect = fake_window
        kwargs = {
            "daily_df": daily_df,
            "weekly_df": pd.DataFrame({"week_id": [202620]}),
            "monthly_df": pd.DataFrame({"month_id": ["202605"]}),
            "date_to_week": {},
            "n_workers": 1,
            "feature_dates": ["2026-05-18", "2026-05-19", "2026-05-20"],
            "disable_cache": True,
        }

        daily = runner.run_historical_prediction(**kwargs, batch_mode="daily")
        monthly = runner.run_historical_prediction(**kwargs, batch_mode="monthly")

        self.assertEqual(daily.to_dict("records"), monthly.to_dict("records"))

    @patch("backtests.liwei_0616_7y03_cons_all_k3_div_k8_reproduction._run_historical_prediction_process_shards")
    def test_parallel_shards_default_to_process_backend_not_thread_backend(self, mock_process_backend: MagicMock) -> None:
        from backtests import liwei_0616_7y03_cons_all_k3_div_k8_reproduction as runner

        mock_process_backend.return_value = [
            {"anchor_date": "2026-05-18", "prediction": -1, "true_label": -1, "confidence": 1.0},
            {"anchor_date": "2026-05-19", "prediction": -1, "true_label": -1, "confidence": 1.0},
        ]

        detail = runner.run_historical_prediction(
            daily_df=pd.DataFrame(
                {
                    "date": pd.to_datetime(["2026-05-18", "2026-05-19"]),
                    "TB7YWI0C": [2.0, 2.01],
                }
            ),
            weekly_df=pd.DataFrame({"week_id": [202620]}),
            monthly_df=pd.DataFrame({"month_id": ["202605"]}),
            date_to_week={},
            n_workers=1,
            feature_dates=["2026-05-18", "2026-05-19"],
            parallel_shards=2,
            disable_cache=True,
        )

        self.assertEqual(detail["anchor_date"].tolist(), ["2026-05-18", "2026-05-19"])
        mock_process_backend.assert_called_once()

    def test_sample_scope_cannot_persist(self) -> None:
        from backtests import liwei_0616_7y03_cons_all_k3_div_k8_reproduction as runner

        with self.assertRaisesRegex(ValueError, "sample mode cannot persist"):
            runner.run_liwei_0616_7y03_cons_all_k3_div_k8_reproduction(
                persist=True,
                sample_dates=["2026-05-18"],
            )

    def test_missing_calendar_target_date_does_not_fallback_to_anchor(self) -> None:
        from backtests import liwei_0616_7y03_cons_all_k3_div_k8_reproduction as runner

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

    @patch("backtests.liwei_0616_7y03_cons_all_k3_div_k8_reproduction.get_calendar")
    @patch("backtests.liwei_0616_7y03_cons_all_k3_div_k8_reproduction.run_historical_prediction")
    @patch("backtests.liwei_0616_7y03_cons_all_k3_div_k8_reproduction.build_monthly_input_artifact")
    @patch("backtests.liwei_0616_7y03_cons_all_k3_div_k8_reproduction.build_weekly_input_artifact")
    @patch("backtests.liwei_0616_7y03_cons_all_k3_div_k8_reproduction.build_daily_input_artifact")
    @patch("backtests.liwei_0616_7y03_cons_all_k3_div_k8_reproduction.create_sqlalchemy_engine")
    def test_run_no_persist_uses_all_three_artifacts_and_does_not_write(
        self,
        mock_engine_factory: MagicMock,
        mock_daily_builder: MagicMock,
        mock_weekly_builder: MagicMock,
        mock_monthly_builder: MagicMock,
        mock_historical: MagicMock,
        mock_get_calendar: MagicMock,
    ) -> None:
        from backtests import liwei_0616_7y03_cons_all_k3_div_k8_reproduction as runner

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
            content_hash="daily_hash",
        )
        mock_weekly_builder.return_value = SimpleNamespace(
            dataframe=pd.DataFrame({"week_id": [202621]}),
            path=Path("/tmp/weekly.csv"),
            source="weekly_source",
            data_version="weekly_v",
            content_hash="weekly_hash",
        )
        mock_monthly_builder.return_value = SimpleNamespace(
            dataframe=pd.DataFrame({"month_id": ["202504"]}),
            path=Path("/tmp/monthly.csv"),
            source="monthly_source",
            data_version="monthly_v",
            content_hash="monthly_hash",
        )
        mock_historical.return_value = pd.DataFrame(
            {
                "anchor_date": ["2026-05-22"],
                "prediction": [0],
                "true_label": [-1],
                "confidence": [0.0],
                "vote_score": [0.0],
                "baseline_signs": [{"STD": -1, "DIV": -1, "ACCWT": 1, "CROSS_5Y": 1}],
            }
        )

        payload = runner.run_liwei_0616_7y03_cons_all_k3_div_k8_reproduction(persist=False)

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["scheme_id"], SCHEME_ID)
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["rows"][0]["target_date"], "2026-05-29")
        self.assertEqual(payload["runs"][0]["rows"], payload["rows"])
        self.assertEqual(payload["summary"]["backtest_scope"], "full_historical")
        calendar.nth_trading_day_after.assert_called_with("2026-05-22", runner.HORIZON)
        mock_daily_builder.assert_called_once()
        mock_weekly_builder.assert_called_once()
        mock_monthly_builder.assert_called_once()
        self.assertEqual(mock_weekly_builder.call_args.kwargs["as_of_date"], runner.BACKTEST_INPUT_END)
        self.assertEqual(mock_historical.call_args.kwargs["parallel_shards"], 1)
        self.assertEqual(mock_historical.call_args.kwargs["batch_mode"], "daily")
        self.assertEqual(payload["summary"]["batch_mode"], "daily")
        engine.dispose.assert_called_once()

    @patch("backtests.liwei_0616_7y03_cons_all_k3_div_k8_reproduction.run_liwei_0616_7y03_cons_all_k3_div_k8_reproduction")
    def test_main_emits_json_object_for_backtest_gate(self, mock_run: MagicMock) -> None:
        from backtests import liwei_0616_7y03_cons_all_k3_div_k8_reproduction as runner

        mock_run.return_value = {"status": "success", "row_count": 1}
        stream = io.StringIO()
        with patch(
            "sys.argv",
            ["runner", "--no-persist", "--sample-dates", "2026-05-18,2026-05-22", "--batch-mode", "daily"],
        ), redirect_stdout(stream):
            runner.main()

        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(mock_run.call_args.kwargs["sample_dates"], ["2026-05-18", "2026-05-22"])
        self.assertEqual(mock_run.call_args.kwargs["batch_mode"], "daily")


if __name__ == "__main__":
    unittest.main()
