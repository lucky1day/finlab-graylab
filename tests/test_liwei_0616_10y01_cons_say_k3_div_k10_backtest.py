"""liwei_0616 10Y_01 SAY 共识方案回测 runner 测试。"""

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


SCHEME_ID = "liwei_0616_10y01_cons_say_k3_div_k10"


class Liwei061610Y01BacktestTests(unittest.TestCase):
    """历史回测 runner、source-original benchmark 与加速层测试。"""

    def test_benchmark_files_use_source_original_feature_target_schema_and_match(self) -> None:
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
            "V55_7Y_score",
            "V55_7Y_dir",
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
        self.assertEqual(set(original["target_tenor"]), {"10Y"})
        self.assertEqual(int((pd.to_datetime(original["target_date"]) < pd.Timestamp("2026-06-01")).sum()), 13)
        self.assertEqual(int((pd.to_datetime(original["target_date"]) >= pd.Timestamp("2026-06-01")).sum()), 8)
        source_original_sensitive = original.loc[
            original["feature_date"].isin(["2026-05-19", "2026-05-20", "2026-05-21", "2026-05-22", "2026-05-26", "2026-05-28"]),
            ["feature_date", "direction", "confidence", "label", "is_correct"],
        ].to_dict("records")
        self.assertEqual(
            source_original_sensitive,
            [
                {"feature_date": "2026-05-19", "direction": 0, "confidence": 0.0, "label": 1, "is_correct": False},
                {"feature_date": "2026-05-20", "direction": 0, "confidence": 0.0, "label": -1, "is_correct": False},
                {"feature_date": "2026-05-21", "direction": 0, "confidence": 0.0, "label": -1, "is_correct": False},
                {"feature_date": "2026-05-22", "direction": 0, "confidence": 0.0, "label": -1, "is_correct": False},
                {"feature_date": "2026-05-26", "direction": 1, "confidence": 1.0, "label": -1, "is_correct": False},
                {"feature_date": "2026-05-28", "direction": 1, "confidence": 1.0, "label": -1, "is_correct": False},
            ],
        )
        self.assertEqual(int((original["direction"] == 0).sum()), 5)
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
        internal_columns = [name for name in expected_columns if name == "vote_score" or name.endswith(("_score", "_dir"))]
        for column in internal_columns:
            if column.endswith("_score") or column == "vote_score":
                max_abs = (merged[f"{column}_original"] - merged[f"{column}_current"]).abs().max()
                self.assertLessEqual(float(max_abs), 1e-8, column)
            else:
                self.assertTrue((merged[f"{column}_original"] == merged[f"{column}_current"]).all(), column)

        for summary_name in ("original_backtest_summary.json", "current_backtest_summary.json"):
            summary = pd.read_json(bench / summary_name, typ="series")
            self.assertEqual(summary["scheme_id"], SCHEME_ID)
            self.assertEqual(summary["source_model_id"], "10Y_01_cons_SAY_k_3_DIV_K_10")
            self.assertEqual(summary["benchmark_scope"], "source_latest_oos_20260616_full_strict_feature_target")
            self.assertEqual(int(summary["row_count"]), 21)
            self.assertEqual(int(summary["samples"]), 21)
            self.assertEqual(int(summary["metric_samples"]), 16)
            self.assertEqual(int(summary["correct"]), 12)
            self.assertEqual(int(summary["no_trade"]), 5)
            self.assertEqual(int(summary["backtest_rows_before_live_cutoff"]), 13)
            self.assertEqual(int(summary["gray_live_boundary_rows"]), 8)
            self.assertEqual(summary["source_latest_start"], "2026-05-01")
            self.assertEqual(summary["source_end"], "2026-06-10")
            self.assertIn("prior-year window plus latest window", summary["source_execution_scope"])
            self.assertEqual(
                summary["source_test_ranges"],
                [["2025-05-01", "2025-06-30"], ["2026-05-01", "2026-06-10"]],
            )
            self.assertEqual(int(summary["source_predictions_alignment"]["pred_diff_count"]), 0)
            self.assertEqual(int(summary["source_internal_alignment"]["internal_score_diff_count"]), 0)
            self.assertEqual(summary["vote_baselines"], ["STD", "ACCWT", "V55_7Y"])
            self.assertEqual(summary["fallback_baseline"], "DIV")
            self.assertEqual(int(summary["streak_K"]), 10)

    def test_build_backtest_rows_use_feature_date_and_target_cutoff(self) -> None:
        from backtests import liwei_0616_10y01_cons_say_k3_div_k10_reproduction as runner

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
        self.assertEqual(rows[0]["target_tenor"], "10Y")
        self.assertEqual(rows[0]["horizon"], 5)
        self.assertEqual(rows[0]["predicted_direction"], 0)
        self.assertEqual(rows[0]["confidence"], 0.0)
        self.assertEqual(rows[0]["extra"]["source_model_id"], "10Y_01_cons_SAY_k_3_DIV_K_10")

    def test_full_historical_feature_dates_stop_before_live_target_cutoff(self) -> None:
        from backtests import liwei_0616_10y01_cons_say_k3_div_k10_reproduction as runner

        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-05-22", "2026-05-25", "2026-05-29"]),
                "TB0YWI0C": [2.0, 2.01, 2.02],
            }
        )

        dates = runner._historical_feature_dates(daily_df)

        self.assertEqual(dates, ["2026-05-22"])

    def test_sample_input_end_uses_sample_target_date_not_global_backtest_end(self) -> None:
        from backtests import liwei_0616_10y01_cons_say_k3_div_k10_reproduction as runner

        calendar = MagicMock()
        calendar.nth_trading_day_after.side_effect = lambda day, horizon: {
            ("2026-03-25", 5): "2026-04-01",
            ("2026-04-23", 5): "2026-04-30",
        }[(day, horizon)]

        input_end = runner._effective_input_end(["2026-03-25", "2026-04-23"], calendar)

        self.assertEqual(input_end, "2026-04-30")

    def test_explicit_input_end_can_pin_source_latest_oos_context(self) -> None:
        from backtests import liwei_0616_10y01_cons_say_k3_div_k10_reproduction as runner

        calendar = MagicMock()

        input_end = runner._effective_input_end(["2026-05-06"], calendar, requested_input_end="2026-06-10")

        self.assertEqual(input_end, "2026-06-10")
        calendar.nth_trading_day_after.assert_not_called()

    @patch("backtests.liwei_0616_10y01_cons_say_k3_div_k10_reproduction.run_liwei_0616_10y01_cons_say_k3_div_k10_reproduction")
    def test_main_forwards_input_end_and_phase_a_cache_to_runner(self, mock_run: MagicMock) -> None:
        from backtests import liwei_0616_10y01_cons_say_k3_div_k10_reproduction as runner

        mock_run.return_value = {"status": "success", "row_count": 1}
        stream = io.StringIO()
        with patch(
            "sys.argv",
            ["runner", "--no-persist", "--batch-mode", "monthly", "--input-end", "2026-06-10", "--phase-a-cache"],
        ), redirect_stdout(stream):
            runner.main()

        self.assertEqual(mock_run.call_args.kwargs["batch_mode"], "monthly")
        self.assertEqual(mock_run.call_args.kwargs["input_end"], "2026-06-10")
        self.assertTrue(mock_run.call_args.kwargs["use_phase_a_cache"])

    @patch("backtests.liwei_0616_10y01_cons_say_k3_div_k10_reproduction.run_10y01_for_window_silent")
    def test_historical_prediction_runs_each_feature_date_as_pit_cutoff(self, mock_runner: MagicMock) -> None:
        from backtests import liwei_0616_10y01_cons_say_k3_div_k10_reproduction as runner

        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-05-18", "2026-05-19", "2026-05-20"]),
                "TB0YWI0C": [2.0, 2.01, 2.02],
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
        self.assertEqual(called_current_starts, ["2026-05-01"] * 3)
        self.assertEqual(called_current_ends, called_feature_dates)
        self.assertEqual(called_test_ranges, [(("2025-05-01", "2025-05-31"), ("2026-05-01", "2026-05-20"))] * 3)

    @patch("backtests.liwei_0616_10y01_cons_say_k3_div_k10_reproduction.run_10y01_for_window_silent")
    def test_monthly_batch_runs_one_window_for_same_month_and_returns_each_date(self, mock_runner: MagicMock) -> None:
        from backtests import liwei_0616_10y01_cons_say_k3_div_k10_reproduction as runner

        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-05-18", "2026-05-19", "2026-05-20"]),
                "TB0YWI0C": [2.0, 2.01, 2.02],
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
        self.assertEqual(mock_runner.call_args.kwargs["current_start"], "2026-05-01")
        self.assertEqual(mock_runner.call_args.kwargs["current_end"], "2026-05-20")
        self.assertEqual(mock_runner.call_args.kwargs["test_ranges"], (("2025-05-01", "2025-05-31"), ("2026-05-01", "2026-05-20")))

    @patch("backtests.liwei_0616_10y01_cons_say_k3_div_k10_reproduction.run_10y01_for_window_silent")
    def test_targeted_monthly_sample_runs_one_source_context_across_feature_months(self, mock_runner: MagicMock) -> None:
        from backtests import liwei_0616_10y01_cons_say_k3_div_k10_reproduction as runner

        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-03-31", "2026-04-01", "2026-04-30"]),
                "TB0YWI0C": [2.0, 2.01, 2.02],
            }
        )

        def fake_window(**kwargs):
            return pd.DataFrame(
                {
                    "anchor_date": ["2026-03-31", "2026-04-01"],
                    "prediction": [-1, 1],
                    "true_label": [-1, 1],
                    "confidence": [1.0, 1.0],
                }
            )

        mock_runner.side_effect = fake_window

        detail = runner.run_historical_prediction(
            daily_df=daily_df,
            weekly_df=pd.DataFrame({"week_id": [202613, 202614, 202618]}),
            monthly_df=pd.DataFrame({"month_id": ["202603", "202604"]}),
            date_to_week={},
            n_workers=1,
            feature_dates=["2026-03-31", "2026-04-01"],
            disable_cache=True,
            batch_mode="monthly",
        )

        self.assertEqual(detail["anchor_date"].tolist(), ["2026-03-31", "2026-04-01"])
        self.assertEqual(mock_runner.call_count, 1)
        self.assertEqual(mock_runner.call_args.kwargs["feature_date"], "2026-04-01")
        self.assertEqual(mock_runner.call_args.kwargs["current_start"], "2026-03-01")
        self.assertEqual(mock_runner.call_args.kwargs["current_end"], "2026-04-01")
        self.assertEqual(mock_runner.call_args.kwargs["test_ranges"], (("2025-03-01", "2025-04-30"), ("2026-03-01", "2026-04-30")))

    @patch("backtests.liwei_0616_10y01_cons_say_k3_div_k10_reproduction._build_phase_a_caches")
    @patch("backtests.liwei_0616_10y01_cons_say_k3_div_k10_reproduction.run_10y01_for_window_silent")
    def test_full_monthly_mode_builds_phase_a_cache_once_and_reuses_for_groups(
        self,
        mock_runner: MagicMock,
        mock_build_cache: MagicMock,
    ) -> None:
        from backtests import liwei_0616_10y01_cons_say_k3_div_k10_reproduction as runner

        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-04-30", "2026-05-06"]),
                "TB0YWI0C": [2.0, 2.01],
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
            disable_cache=True,
            use_phase_a_cache=True,
        )

        self.assertEqual(detail["anchor_date"].tolist(), ["2026-04-30", "2026-05-06"])
        mock_build_cache.assert_called_once()
        self.assertEqual(mock_runner.call_count, 2)
        self.assertTrue(all(call.kwargs["phase_a_caches"] is phase_a_caches for call in mock_runner.call_args_list))

    @patch("backtests.liwei_0616_10y01_cons_say_k3_div_k10_reproduction._build_phase_a_caches")
    def test_full_monthly_mode_skips_phase_a_cache_when_all_row_cache_exists(
        self,
        mock_build_cache: MagicMock,
    ) -> None:
        from backtests import liwei_0616_10y01_cons_say_k3_div_k10_reproduction as runner

        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-04-30", "2026-05-06"]),
                "TB0YWI0C": [2.0, 2.01],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp)
            cache_key_parts = {"test": "v1"}
            for feature_date, window_end, prediction in [
                ("2026-04-30", "2026-04-30", 1),
                ("2026-05-06", "2026-05-06", -1),
            ]:
                cache_path = runner._cache_path(
                    cache_root,
                    feature_date,
                    cache_key_parts,
                    cache_mode="monthly",
                    window_end=window_end,
                    model_context_end="2026-05-06",
                )
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps(
                        {
                            "anchor_date": feature_date,
                            "prediction": prediction,
                            "true_label": prediction,
                            "confidence": 1.0,
                        }
                    ),
                    encoding="utf-8",
                )

            detail = runner.run_historical_prediction(
                daily_df=daily_df,
                weekly_df=pd.DataFrame({"week_id": [202618, 202619]}),
                monthly_df=pd.DataFrame({"month_id": ["202604", "202605"]}),
                date_to_week={},
                n_workers=1,
                batch_mode="monthly",
                cache_dir=cache_root,
                cache_key_parts=cache_key_parts,
                use_phase_a_cache=True,
            )

        self.assertEqual(detail["anchor_date"].tolist(), ["2026-04-30", "2026-05-06"])
        mock_build_cache.assert_not_called()

    @patch("backtests.liwei_0616_10y01_cons_say_k3_div_k10_reproduction.run_prediction")
    def test_build_phase_a_caches_redirects_source_logs_away_from_stdout(self, mock_run_prediction: MagicMock) -> None:
        import numpy as np

        from backtests import liwei_0616_10y01_cons_say_k3_div_k10_reproduction as runner

        def fake_run_prediction(cfg):
            print("source log on stdout")
            return np.array([0]), {"phase_a_cache": {"baseline": cfg["name"]}}

        mock_run_prediction.side_effect = fake_run_prediction
        stream = io.StringIO()
        with redirect_stdout(stream):
            caches = runner._build_phase_a_caches(
                dates=["2026-05-06"],
                daily_df=pd.DataFrame({"date": pd.to_datetime(["2026-05-06"]), "TB0YWI0C": [2.0]}),
                weekly_df=pd.DataFrame({"week_id": [202620]}),
                monthly_df=pd.DataFrame({"month_id": ["202605"]}),
                date_to_week={"2026-05-06": 202620},
                n_workers=1,
                model_context_end="2026-06-10",
            )

        self.assertEqual(stream.getvalue(), "")
        self.assertEqual(set(caches), {"STD", "ACCWT", "V55_7Y", "DIV"})

    @patch("backtests.liwei_0616_10y01_cons_say_k3_div_k10_reproduction.run_10y01_for_window_silent")
    def test_cache_on_off_and_sharded_paths_match_serial_on_targeted_dates(self, mock_runner: MagicMock) -> None:
        from backtests import liwei_0616_10y01_cons_say_k3_div_k10_reproduction as runner

        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-05-18", "2026-05-19", "2026-05-20"]),
                "TB0YWI0C": [2.0, 2.01, 2.02],
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
        from backtests import liwei_0616_10y01_cons_say_k3_div_k10_reproduction as runner

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

    @patch("backtests.liwei_0616_10y01_cons_say_k3_div_k10_reproduction.run_10y01_for_window_silent")
    def test_monthly_batch_matches_daily_reference_on_mocked_targeted_dates(self, mock_runner: MagicMock) -> None:
        from backtests import liwei_0616_10y01_cons_say_k3_div_k10_reproduction as runner

        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-05-18", "2026-05-19", "2026-05-20"]),
                "TB0YWI0C": [2.0, 2.01, 2.02],
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

    @patch("backtests.liwei_0616_10y01_cons_say_k3_div_k10_reproduction._run_historical_prediction_process_shards")
    def test_parallel_shards_default_to_process_backend_not_thread_backend(self, mock_process_backend: MagicMock) -> None:
        from backtests import liwei_0616_10y01_cons_say_k3_div_k10_reproduction as runner

        mock_process_backend.return_value = [
            {"anchor_date": "2026-05-18", "prediction": -1, "true_label": -1, "confidence": 1.0},
            {"anchor_date": "2026-05-19", "prediction": -1, "true_label": -1, "confidence": 1.0},
        ]

        detail = runner.run_historical_prediction(
            daily_df=pd.DataFrame(
                {
                    "date": pd.to_datetime(["2026-05-18", "2026-05-19"]),
                    "TB0YWI0C": [2.0, 2.01],
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
        from backtests import liwei_0616_10y01_cons_say_k3_div_k10_reproduction as runner

        with self.assertRaisesRegex(ValueError, "sample mode cannot persist"):
            runner.run_liwei_0616_10y01_cons_say_k3_div_k10_reproduction(
                persist=True,
                sample_dates=["2026-05-18"],
            )

    def test_missing_calendar_target_date_does_not_fallback_to_anchor(self) -> None:
        from backtests import liwei_0616_10y01_cons_say_k3_div_k10_reproduction as runner

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

    @patch("backtests.liwei_0616_10y01_cons_say_k3_div_k10_reproduction.get_calendar")
    @patch("backtests.liwei_0616_10y01_cons_say_k3_div_k10_reproduction.run_historical_prediction")
    @patch("backtests.liwei_0616_10y01_cons_say_k3_div_k10_reproduction.build_monthly_input_artifact")
    @patch("backtests.liwei_0616_10y01_cons_say_k3_div_k10_reproduction.build_weekly_input_artifact")
    @patch("backtests.liwei_0616_10y01_cons_say_k3_div_k10_reproduction.build_daily_input_artifact")
    @patch("backtests.liwei_0616_10y01_cons_say_k3_div_k10_reproduction.create_sqlalchemy_engine")
    def test_run_no_persist_uses_all_three_artifacts_and_does_not_write(
        self,
        mock_engine_factory: MagicMock,
        mock_daily_builder: MagicMock,
        mock_weekly_builder: MagicMock,
        mock_monthly_builder: MagicMock,
        mock_historical: MagicMock,
        mock_get_calendar: MagicMock,
    ) -> None:
        from backtests import liwei_0616_10y01_cons_say_k3_div_k10_reproduction as runner

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
                "TB0YWI0C": [2.0, 2.01, 2.02, 2.03, 2.04, 2.05],
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
                "baseline_signs": [{"STD": -1, "DIV": -1, "ACCWT": 1, "V55_7Y": 1}],
            }
        )

        payload = runner.run_liwei_0616_10y01_cons_say_k3_div_k10_reproduction(persist=False)

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

    @patch("backtests.liwei_0616_10y01_cons_say_k3_div_k10_reproduction.run_liwei_0616_10y01_cons_say_k3_div_k10_reproduction")
    def test_main_emits_json_object_for_backtest_gate(self, mock_run: MagicMock) -> None:
        from backtests import liwei_0616_10y01_cons_say_k3_div_k10_reproduction as runner

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
