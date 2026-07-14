"""Independent continuous full-OOS gray models for Liwei 10Y01 and 5Y01."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEN_Y_SCHEME_ID = "liwei_0616_10y01_full_oos_k3_div_k10"
FIVE_Y_SCHEME_ID = "liwei_0616_5y01_full_oos_k3_div_k10"


class FullOosConfigTests(unittest.TestCase):
    def _load(self, scheme_id: str) -> dict:
        from harness.config_loader import load_config_raw

        return load_config_raw(PROJECT_ROOT / "schemes" / scheme_id / "config.yaml")

    def test_10y_config_declares_independent_active_full_oos_model(self) -> None:
        config = self._load(TEN_Y_SCHEME_ID)

        self.assertEqual(config["scheme_id"], TEN_Y_SCHEME_ID)
        self.assertEqual(config["name"], "liwei_0616 10Y_01 原脚本Full-OOS")
        self.assertEqual(config["tenors"], ["10Y"])
        self.assertEqual(config["horizon"], 5)
        self.assertEqual(config["task_type"], "T+5")
        self.assertEqual(config["frequency"], "daily")
        self.assertEqual(config["status"], "active")
        self.assertEqual(config["schedule"]["cron"], "3 7 * * 1-5")
        self.assertEqual(config["schedule"]["timezone"], "Asia/Shanghai")
        self.assertEqual(config["schedule"]["timeout_sec"], 3600)
        self.assertIn("TB0YWI0C", config["input_spec"]["required_columns"])
        self.assertEqual(
            config["backtest"]["runner"],
            f"backtests.{TEN_Y_SCHEME_ID}_reproduction",
        )
        self.assertEqual(config["backtest"]["benchmark_id"], "liwei_0616_10y01_full_oos")
        self.assertNotIn("--input-end", config["backtest"]["runner_args"])

    def test_5y_config_declares_independent_active_full_oos_model(self) -> None:
        config = self._load(FIVE_Y_SCHEME_ID)

        self.assertEqual(config["scheme_id"], FIVE_Y_SCHEME_ID)
        self.assertEqual(config["name"], "liwei_0616 5Y_01 原脚本Full-OOS")
        self.assertEqual(config["tenors"], ["5Y"])
        self.assertEqual(config["horizon"], 5)
        self.assertEqual(config["task_type"], "T+5")
        self.assertEqual(config["frequency"], "daily")
        self.assertEqual(config["status"], "active")
        self.assertEqual(config["schedule"]["cron"], "3 7 * * 1-5")
        self.assertEqual(config["schedule"]["timezone"], "Asia/Shanghai")
        self.assertEqual(config["schedule"]["timeout_sec"], 3600)
        self.assertIn("TB5YWI0C", config["input_spec"]["required_columns"])
        self.assertEqual(
            config["backtest"]["runner"],
            f"backtests.{FIVE_Y_SCHEME_ID}_reproduction",
        )
        self.assertEqual(config["backtest"]["benchmark_id"], "liwei_0616_5y01_full_oos")
        self.assertNotIn("--input-end", config["backtest"]["runner_args"])


class FullOosWindowTests(unittest.TestCase):
    def test_10y_window_is_one_continuous_source_sequence(self) -> None:
        from schemes.liwei_0616_10y01_full_oos_k3_div_k10.inference import full_oos_window

        window = full_oos_window("2026-07-10")

        self.assertEqual(window.source_end, "2026-07-10")
        self.assertEqual(window.current_start, "2026-07-10")
        self.assertEqual(window.current_end, "2026-07-10")
        self.assertEqual(window.test_ranges, (("2024-01-01", "2026-07-10"),))

    def test_5y_window_keeps_batch_selection_separate_from_full_source_end(self) -> None:
        from schemes.liwei_0616_5y01_full_oos_k3_div_k10.inference import full_oos_window

        window = full_oos_window(
            "2026-05-06",
            source_end="2026-05-29",
            current_start="2026-05-01",
            current_end="2026-05-22",
        )

        self.assertEqual(window.source_end, "2026-05-29")
        self.assertEqual(window.current_start, "2026-05-01")
        self.assertEqual(window.current_end, "2026-05-22")
        self.assertEqual(window.test_ranges, (("2024-01-01", "2026-05-29"),))


class FullOosLiveAdapterTests(unittest.TestCase):
    @staticmethod
    def _artifact(frame: pd.DataFrame, name: str) -> SimpleNamespace:
        return SimpleNamespace(
            dataframe=frame,
            path=Path(f"/tmp/{name}.csv"),
            source=f"shared_data_service_{name}",
            data_version=f"shared_data_service_{name}.v1",
            content_hash=f"{name}_hash",
        )

    @patch("schemes.liwei_0616_10y01_full_oos_k3_div_k10.predict.run_for_feature_date")
    @patch("schemes.liwei_0616_10y01_full_oos_k3_div_k10.predict.build_monthly_input_artifact")
    @patch("schemes.liwei_0616_10y01_full_oos_k3_div_k10.predict.build_weekly_input_artifact")
    @patch("schemes.liwei_0616_10y01_full_oos_k3_div_k10.predict.build_daily_input_artifact")
    @patch("schemes.liwei_0616_10y01_full_oos_k3_div_k10.predict.create_input_engine")
    @patch("schemes.liwei_0616_10y01_full_oos_k3_div_k10.predict.get_calendar")
    def test_10y_live_dates_match_existing_7y_daily_conversion(
        self,
        mock_get_calendar: MagicMock,
        mock_engine_factory: MagicMock,
        mock_daily_builder: MagicMock,
        mock_weekly_builder: MagicMock,
        mock_monthly_builder: MagicMock,
        mock_inference: MagicMock,
    ) -> None:
        self._set_live_mocks(
            mock_get_calendar,
            mock_engine_factory,
            mock_daily_builder,
            mock_weekly_builder,
            mock_monthly_builder,
            tenor_column="TB0YWI0C",
        )
        mock_inference.return_value = self._result("liwei_0616_10y01_full_oos_v1")

        from schemes.liwei_0616_10y01_full_oos_k3_div_k10 import predict

        record = predict.run("2026-07-13")[0]

        self._assert_live_record(record, TEN_Y_SCHEME_ID, "10Y")
        self.assertEqual(record.model_version, "liwei_0616_10y01_full_oos_v1")
        self.assertEqual(record.extra["model_scope"], "source_original_full_oos")
        self.assertEqual(record.extra["model_test_ranges"], [["2024-01-01", "2026-07-10"]])

    @patch("schemes.liwei_0616_5y01_full_oos_k3_div_k10.predict.run_for_feature_date")
    @patch("schemes.liwei_0616_5y01_full_oos_k3_div_k10.predict.build_monthly_input_artifact")
    @patch("schemes.liwei_0616_5y01_full_oos_k3_div_k10.predict.build_weekly_input_artifact")
    @patch("schemes.liwei_0616_5y01_full_oos_k3_div_k10.predict.build_daily_input_artifact")
    @patch("schemes.liwei_0616_5y01_full_oos_k3_div_k10.predict.create_input_engine")
    @patch("schemes.liwei_0616_5y01_full_oos_k3_div_k10.predict.get_calendar")
    def test_5y_live_dates_match_existing_7y_daily_conversion(
        self,
        mock_get_calendar: MagicMock,
        mock_engine_factory: MagicMock,
        mock_daily_builder: MagicMock,
        mock_weekly_builder: MagicMock,
        mock_monthly_builder: MagicMock,
        mock_inference: MagicMock,
    ) -> None:
        self._set_live_mocks(
            mock_get_calendar,
            mock_engine_factory,
            mock_daily_builder,
            mock_weekly_builder,
            mock_monthly_builder,
            tenor_column="TB5YWI0C",
        )
        mock_inference.return_value = self._result("liwei_0616_5y01_full_oos_v1")

        from schemes.liwei_0616_5y01_full_oos_k3_div_k10 import predict

        record = predict.run("2026-07-13")[0]

        self._assert_live_record(record, FIVE_Y_SCHEME_ID, "5Y")
        self.assertEqual(record.model_version, "liwei_0616_5y01_full_oos_v1")
        self.assertEqual(record.extra["model_scope"], "source_original_full_oos")
        self.assertEqual(record.extra["model_test_ranges"], [["2024-01-01", "2026-07-10"]])

    def _set_live_mocks(
        self,
        mock_get_calendar: MagicMock,
        mock_engine_factory: MagicMock,
        mock_daily_builder: MagicMock,
        mock_weekly_builder: MagicMock,
        mock_monthly_builder: MagicMock,
        *,
        tenor_column: str,
    ) -> None:
        mock_engine_factory.return_value = MagicMock()
        calendar = MagicMock()
        calendar.previous_trading_day.return_value = "2026-07-10"
        calendar.nth_trading_day_after.return_value = "2026-07-17"
        calendar.week_id_for_date.return_value = 202628
        mock_get_calendar.return_value = calendar
        mock_daily_builder.return_value = self._artifact(
            pd.DataFrame({"date": pd.to_datetime(["2026-07-10"]), tenor_column: [2.0]}),
            "daily",
        )
        mock_weekly_builder.return_value = self._artifact(
            pd.DataFrame({"week_id": [202628], "S0114089": [1.0]}),
            "weekly",
        )
        mock_monthly_builder.return_value = self._artifact(
            pd.DataFrame({"month_id": ["202606"], "M0000545": [1.0]}),
            "monthly",
        )

    @staticmethod
    def _result(model_version: str) -> dict:
        return {
            "anchor_date": "2026-07-10",
            "prediction": -1,
            "confidence": 1.0,
            "vote_score": -0.75,
            "true_label": None,
            "baseline_signs": {"STD": -1, "DIV": -1, "ACCWT": -1},
            "baseline_scores": {"STD": -0.7, "DIV": -0.8, "ACCWT": -0.75},
            "model_version": model_version,
            "phase_a_cache_audit": {
                "status": "hit",
                "watermark": "2026-07-10",
                "missing_dates": [],
                "version": "liwei_0616.phase_a.v1",
                "fingerprint": "full-oos-cache",
            },
        }

    def _assert_live_record(self, record, scheme_id: str, tenor: str) -> None:
        self.assertEqual(record.scheme_id, scheme_id)
        self.assertEqual(record.target_tenor, tenor)
        self.assertEqual(record.horizon, 5)
        self.assertEqual(record.predict_date, "2026-07-13")
        self.assertEqual(record.feature_date, "2026-07-10")
        self.assertEqual(record.target_date, "2026-07-17")
        self.assertEqual(record.predicted_direction, -1)


class FullOosBacktestTests(unittest.TestCase):
    def test_5y_historical_feature_dates_stop_before_june_gray_targets(self) -> None:
        from backtests import liwei_0616_5y01_full_oos_k3_div_k10_reproduction as runner

        dates = runner._historical_feature_dates(
            pd.DataFrame(
                {
                    "date": pd.to_datetime(
                        ["2026-05-21", "2026-05-22", "2026-05-25", "2026-05-29"]
                    )
                }
            )
        )

        self.assertEqual(dates, ["2026-05-21", "2026-05-22"])

    def test_10y_monthly_groups_follow_target_month_and_target_month_end(self) -> None:
        from backtests import liwei_0616_10y01_full_oos_k3_div_k10_reproduction as runner

        target_dates = {
            "2026-01-29": "2026-02-05",
            "2026-01-30": "2026-02-06",
            "2026-02-02": "2026-02-09",
            "2026-02-24": "2026-03-03",
        }
        groups = runner._monthly_source_groups(
            dates=list(target_dates),
            model_context_end="2026-03-03",
            group_all_dates=False,
            target_date_for_anchor=target_dates.get,
        )

        self.assertEqual(
            groups,
            [
                {
                    "dates": ["2026-01-29", "2026-01-30", "2026-02-02"],
                    "window_end": "2026-02-02",
                    "source_end": "2026-02-09",
                },
                {
                    "dates": ["2026-02-24"],
                    "window_end": "2026-02-24",
                    "source_end": "2026-03-03",
                },
            ],
        )

    def test_5y_monthly_batch_passes_one_full_oos_range(self) -> None:
        from backtests import liwei_0616_5y01_full_oos_k3_div_k10_reproduction as runner

        detail = pd.DataFrame(
            {
                "anchor_date": ["2026-05-06", "2026-05-07"],
                "prediction": [1, -1],
                "true_label": [1, -1],
                "confidence": [1.0, 1.0],
            }
        )
        with patch.object(runner, "run_for_window_silent", return_value=detail) as mock_run:
            rows = runner._run_monthly_batch_group(
                group_dates=["2026-05-06", "2026-05-07"],
                daily_df=pd.DataFrame({"date": pd.to_datetime(["2026-05-06", "2026-05-07"])}),
                weekly_df=pd.DataFrame({"week_id": [202619]}),
                monthly_df=pd.DataFrame({"month_id": ["202604"]}),
                date_to_week={},
                n_workers=1,
                model_context_end="2026-05-29",
                source_end="2026-05-15",
                cache_dir=None,
                cache_key_parts={},
            )

        self.assertEqual([row["anchor_date"] for row in rows], ["2026-05-06", "2026-05-07"])
        self.assertEqual(mock_run.call_args.kwargs["test_ranges"], (("2024-01-01", "2026-05-15"),))
        self.assertEqual(mock_run.call_args.kwargs["current_start"], "2026-05-06")
        self.assertEqual(mock_run.call_args.kwargs["current_end"], "2026-05-07")


class FullOosBenchmarkTests(unittest.TestCase):
    def test_benchmark_evidence_is_strict_and_exact_for_both_new_models(self) -> None:
        for scheme_id, tenor in ((TEN_Y_SCHEME_ID, "10Y"), (FIVE_Y_SCHEME_ID, "5Y")):
            with self.subTest(scheme_id=scheme_id):
                benchmark_dir = PROJECT_ROOT / "schemes" / scheme_id / "benchmarks"
                original = pd.read_csv(benchmark_dir / "original_predictions_sample.csv")
                current = pd.read_csv(benchmark_dir / "current_predictions_sample.csv")
                summary = __import__("json").loads(
                    (benchmark_dir / "original_backtest_summary.json").read_text(encoding="utf-8")
                )

                pd.testing.assert_frame_equal(original, current)
                self.assertEqual(len(original), 21)
                self.assertEqual(set(original["target_tenor"]), {tenor})
                self.assertEqual(set(original["horizon"]), {5})
                self.assertEqual(set(original["benchmark_role"]), {"historical/source-original"})
                self.assertEqual(summary["benchmark_scope"], "source_original_full_oos_targeted_sample")
                self.assertEqual(summary["source_oos_start"], "2024-01-01")
                self.assertEqual(summary["source_end"], "2026-04-30")
                self.assertEqual(summary["row_count"], 21)
                self.assertEqual(summary["internal_mismatch_count"], 0)

if __name__ == "__main__":
    unittest.main()
