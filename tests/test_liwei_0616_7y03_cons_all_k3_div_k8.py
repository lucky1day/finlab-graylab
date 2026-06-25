"""liwei_0616 7Y_03 ALL 共识方案单元测试。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd


SCHEME_ID = "liwei_0616_7y03_cons_all_k3_div_k8"


class Liwei06167Y03ConfigTests(unittest.TestCase):
    """config.yaml 入库契约测试。"""

    def test_config_declares_7y_t5_active_auxiliary_inputs_and_benchmark(self) -> None:
        from harness.config_loader import load_config_raw

        project_root = Path(__file__).resolve().parents[1]
        config = load_config_raw(project_root / "schemes" / SCHEME_ID / "config.yaml")

        self.assertEqual(config["scheme_id"], SCHEME_ID)
        self.assertEqual(config["name"], "liwei_0616 7Y_03 ALL共识-DIV回退")
        self.assertIn("7Y_03_cons_ALL_k_3_DIV_K_8", config["description"])
        self.assertIn("STD/DIV/ACCWT/CROSS_5Y", config["description"])
        self.assertEqual(config["horizon"], 5)
        self.assertEqual(config["task_type"], "T+5")
        self.assertEqual(config["tenors"], ["7Y"])
        self.assertEqual(config["frequency"], "daily")
        self.assertEqual(config["status"], "active")
        self.assertEqual(config["schedule"]["cron"], "3 7 * * 1-5")
        self.assertIn("TB7YWI0C", config["input_spec"]["required_columns"])
        self.assertNotIn("cross5y", config["scheme_id"].lower())

        aux = config["input_spec"]["auxiliary_inputs"]
        self.assertEqual([item["frequency"] for item in aux], ["weekly", "monthly"])
        self.assertIn("S0114089", aux[0]["required_columns"])
        self.assertIn("M0061518", aux[1]["required_columns"])
        self.assertNotIn("M0041342", aux[1]["required_columns"])
        from schemes.liwei_0616_7y03_cons_all_k3_div_k8.core.v31_common import MONTHLY_COLS

        self.assertIn("M0041342", MONTHLY_COLS)
        self.assertEqual(config["backtest"]["runner"], f"backtests.{SCHEME_ID}_reproduction")
        self.assertEqual(config["backtest"]["runner_args"], ["--batch-mode", "monthly"])
        self.assertEqual(config["backtest"]["benchmark_id"], "liwei_0616_7y_03")
        self.assertEqual(config["backtest"]["end_date"], "2026-05-22")
        self.assertTrue(config["backtest"]["benchmark_required"])


class Liwei06167Y03CoreTests(unittest.TestCase):
    """共识、fallback 和 source 配置逻辑测试。"""

    def test_consensus_requires_three_of_four_all_baselines_to_agree(self) -> None:
        import numpy as np

        from schemes.liwei_0616_7y03_cons_all_k3_div_k8.core.v31_common import apply_consensus

        signs = {
            "STD": np.array([1, 1, 1, -1, -1, 0, 1]),
            "DIV": np.array([1, 1, -1, -1, 0, -1, -1]),
            "ACCWT": np.array([1, 0, 1, -1, -1, -1, 1]),
            "CROSS_5Y": np.array([0, 1, 1, -1, -1, -1, -1]),
        }

        result = apply_consensus(signs, ["STD", "DIV", "ACCWT", "CROSS_5Y"], k_agree=3, n_rows=7)

        self.assertEqual(result.tolist(), [1, 1, 1, -1, -1, -1, 0])

    def test_build_prediction_uses_div_fallback_after_k8_and_resets_on_direction_change(self) -> None:
        import numpy as np

        from schemes.liwei_0616_7y03_cons_all_k3_div_k8.core.v31_common import (
            PROD_CONFIG,
            build_prediction,
        )

        self.assertEqual(PROD_CONFIG["baselines"], ["STD", "DIV", "ACCWT", "CROSS_5Y"])
        self.assertEqual(PROD_CONFIG["fallback"], "DIV")
        self.assertEqual(PROD_CONFIG["streak_K"], 8)
        signs = {
            "STD": np.array([1] * 11, dtype=np.int32),
            "DIV": np.array([1] * 8 + [-1, -1, -1], dtype=np.int32),
            "ACCWT": np.array([1] * 11, dtype=np.int32),
            "CROSS_5Y": np.array([1] * 11, dtype=np.int32),
        }

        result = build_prediction(signs, 11)

        self.assertEqual(result.tolist(), [1] * 8 + [-1, 1, 1])

    def test_required_baselines_does_not_duplicate_div_when_fallback_is_in_vote_set(self) -> None:
        from schemes.liwei_0616_7y03_cons_all_k3_div_k8.core.v31_common import required_baselines

        self.assertEqual(required_baselines(), ["STD", "DIV", "ACCWT", "CROSS_5Y"])

    def test_lgbm_grid_and_worker_keep_source_training_knobs(self) -> None:
        import numpy as np

        from schemes.liwei_0616_7y03_cons_all_k3_div_k8.core import v31_common

        grid = v31_common.build_lgbm_grid(
            [350, 504],
            [5, 7],
            [20],
            [0.0, 0.5],
            [1.0, 1.5],
            [0.60, 0.65],
            slow_path=True,
            very_slow_path=True,
        )

        self.assertTrue(any(row.get("learning_rate") == 0.01 and row.get("n_estimators") == 350 for row in grid))
        self.assertTrue(any(row.get("learning_rate") == 0.005 and row.get("n_estimators") == 600 for row in grid))

        v31_common._init_worker(
            3,
            np.ones((3, 1)),
            np.array([1.0, -1.0, 1.0]),
            np.array([1.0, 1.1, 1.2]),
            pd.Series([1, -1, 1]),
            np.array([2]),
            5,
            5,
            [7],
            0.77,
            0.66,
            -0.25,
            11,
        )

        self.assertEqual(v31_common._G["subsample"], 0.77)
        self.assertEqual(v31_common._G["colsample_bytree"], 0.66)
        self.assertEqual(v31_common._G["time_weight_alpha"], -0.25)
        self.assertEqual(v31_common._G["early_stopping"], 11)

    def test_weekly_alignment_matches_source_last_trading_day_then_ffill(self) -> None:
        from schemes.liwei_0616_7y03_cons_all_k3_div_k8.core.v31_common import prepare_model_frames

        daily_dates = pd.to_datetime(
            ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
        )
        weekly_df = pd.DataFrame(
            {
                "week_id": [202601, 202602],
                "S0114089": [100.0, 200.0],
            }
        )
        date_to_week = {
            "2026-01-02": 202601,
            "2026-01-05": 202602,
            "2026-01-06": 202602,
            "2026-01-07": 202602,
            "2026-01-08": 202602,
            "2026-01-09": 202602,
        }

        weekly, monthly = prepare_model_frames(
            daily_df=pd.DataFrame({"date": daily_dates}),
            weekly_df=weekly_df,
            monthly_df=None,
            daily_dates=daily_dates,
            date_to_week=date_to_week,
        )

        self.assertEqual(weekly["S0114089"].tolist(), [100.0, 100.0, 100.0, 100.0, 100.0, 200.0])
        self.assertTrue(monthly.empty)

    def test_report_masks_match_source_fixed_oos_periods(self) -> None:
        from schemes.liwei_0616_7y03_cons_all_k3_div_k8.core.v31_common import _dynamic_report_masks

        dates = pd.DatetimeIndex(
            [
                "2024-12-31",
                "2025-01-01",
                "2025-06-30",
                "2025-07-01",
                "2026-04-30",
            ]
        )

        pre_m, sim_m, real_m = _dynamic_report_masks(dates)

        self.assertEqual(pre_m.tolist(), [True, False, False, False, False])
        self.assertEqual(sim_m.tolist(), [False, True, True, False, False])
        self.assertEqual(real_m.tolist(), [False, False, False, True, True])

    def test_ic_screening_cutoff_remains_source_20240101_when_test_window_moves(self) -> None:
        import numpy as np

        from schemes.liwei_0616_7y03_cons_all_k3_div_k8.core import v31_common

        dates = pd.DatetimeIndex(
            [
                *pd.bdate_range("2023-12-01", "2024-01-10"),
                pd.Timestamp("2025-04-30"),
                pd.Timestamp("2025-05-01"),
            ]
        )
        daily = pd.DataFrame(
            {
                "date": dates,
                "TB7YWI0C": np.linspace(2.0, 2.2, len(dates)),
            }
        )
        expected_cutoff = int(np.flatnonzero(dates >= pd.Timestamp("2024-01-01"))[0])
        expected_rows = expected_cutoff - v31_common.HORIZON
        captured: dict[str, int] = {}

        class StopAfterIcScreen(Exception):
            pass

        def fake_ic_screen(feat, labels, *args, **kwargs):
            captured["rows"] = len(labels)
            raise StopAfterIcScreen

        labels = np.ones(len(dates), dtype=np.float64)
        with (
            patch.object(v31_common, "make_labels", return_value=labels),
            patch.object(v31_common, "build_bond_features", return_value=pd.DataFrame({"f0": np.arange(len(dates))})),
            patch.object(v31_common, "build_mf_features", return_value=(pd.DataFrame(index=range(len(dates))), {})),
            patch.object(v31_common, "build_wkmo_features", return_value=pd.DataFrame(index=range(len(dates)))),
            patch.object(v31_common, "ic_screen", side_effect=fake_ic_screen),
        ):
            with self.assertRaises(StopAfterIcScreen):
                v31_common.run_prediction(
                    {
                        **v31_common.model_config("STD"),
                        "daily_df": daily,
                        "weekly_df": pd.DataFrame({"week_id": []}),
                        "monthly_df": pd.DataFrame({"month_id": []}),
                        "date_to_week": {},
                        "test_start": "2025-05-01",
                        "test_end": "2025-05-01",
                        "test_ranges": (("2025-05-01", "2025-05-01"),),
                        "n_workers": 1,
                        "emit_report": False,
                    }
                )

        self.assertEqual(captured["rows"], expected_rows)


class Liwei06167Y03InferenceTests(unittest.TestCase):
    """PIT 窗口与共享 inference 入口测试。"""

    def test_feature_window_uses_source_compatible_fixed_history_context(self) -> None:
        from schemes.liwei_0616_7y03_cons_all_k3_div_k8.inference import liwei_0616_pit_window

        window = liwei_0616_pit_window("2026-06-03")

        self.assertEqual(window.prior_start, "2025-06-03")
        self.assertEqual(window.prior_end, "2025-06-30")
        self.assertEqual(window.latest_start, "2026-06-03")
        self.assertEqual(window.source_end, "2026-06-03")
        self.assertEqual(window.current_start, "2026-06-03")
        self.assertEqual(window.current_end, "2026-06-03")
        self.assertEqual(window.test_ranges, (("2024-01-01", "2026-06-03"),))

    def test_source_batch_window_uses_latest_start_and_data_end_like_original_runner(self) -> None:
        from schemes.liwei_0616_7y03_cons_all_k3_div_k8.inference import liwei_0616_pit_window

        window = liwei_0616_pit_window(
            "2026-06-03",
            source_end="2026-06-10",
            current_start="2026-05-01",
            current_end="2026-06-03",
        )

        self.assertEqual(window.prior_start, "2025-05-01")
        self.assertEqual(window.prior_end, "2025-06-30")
        self.assertEqual(window.latest_start, "2026-05-01")
        self.assertEqual(window.source_end, "2026-06-10")
        self.assertEqual(window.current_start, "2026-05-01")
        self.assertEqual(window.current_end, "2026-06-03")
        self.assertEqual(window.test_ranges, (("2024-01-01", "2026-06-10"),))

    def test_run_for_feature_date_uses_pit_window_and_selects_exact_feature_date(self) -> None:
        from schemes.liwei_0616_7y03_cons_all_k3_div_k8 import inference

        detail = pd.DataFrame(
            {
                "anchor_date": ["2026-06-02", "2026-06-03"],
                "prediction": [-1, 1],
                "true_label": [None, None],
                "confidence": [1.0, 1.0],
                "vote_score": [-0.5, 0.75],
            }
        )
        with patch.object(inference, "run_7y03_for_feature_window", return_value=detail) as mock_run:
            row = inference.run_7y03_for_feature_date(
                daily_df=pd.DataFrame({"date": pd.to_datetime(["2026-06-02", "2026-06-03"])}),
                weekly_df=pd.DataFrame({"week_id": [202622]}),
                monthly_df=pd.DataFrame({"month_id": ["202605"]}),
                date_to_week={"2026-06-03": 202623},
                feature_date="2026-06-03",
                require_labels=False,
                n_workers=1,
            )

        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs["feature_date"], "2026-06-03")
        self.assertEqual(kwargs["current_start"], "2026-06-03")
        self.assertEqual(kwargs["current_end"], "2026-06-03")
        self.assertEqual(kwargs["test_ranges"], (("2024-01-01", "2026-06-03"),))
        self.assertFalse(kwargs["require_labels"])
        self.assertEqual(row["anchor_date"], "2026-06-03")
        self.assertEqual(row["prediction"], 1)
        self.assertEqual(row["model_version"], "liwei_0616_7y_03_v31")


class Liwei06167Y03PredictionRecordTests(unittest.TestCase):
    """predict.py adapter 输出合规性测试。"""

    @patch("schemes.liwei_0616_7y03_cons_all_k3_div_k8.predict.run_7y03_for_feature_date")
    @patch("schemes.liwei_0616_7y03_cons_all_k3_div_k8.predict.build_monthly_input_artifact")
    @patch("schemes.liwei_0616_7y03_cons_all_k3_div_k8.predict.build_weekly_input_artifact")
    @patch("schemes.liwei_0616_7y03_cons_all_k3_div_k8.predict.build_daily_input_artifact")
    @patch("schemes.liwei_0616_7y03_cons_all_k3_div_k8.predict.create_input_engine")
    @patch("schemes.liwei_0616_7y03_cons_all_k3_div_k8.predict.get_calendar")
    def test_run_uses_signal_date_previous_trading_feature_date_and_t5_target_date(
        self,
        mock_get_calendar: MagicMock,
        mock_engine_factory: MagicMock,
        mock_daily_builder: MagicMock,
        mock_weekly_builder: MagicMock,
        mock_monthly_builder: MagicMock,
        mock_inference: MagicMock,
    ) -> None:
        mock_engine = MagicMock()
        mock_engine_factory.return_value = mock_engine
        mock_calendar = MagicMock()
        mock_calendar.previous_trading_day.return_value = "2026-06-10"
        mock_calendar.nth_trading_day_after.return_value = "2026-06-17"
        mock_calendar.week_id_for_date.side_effect = lambda day: 202624 if day == "2026-06-10" else 202623
        mock_get_calendar.return_value = mock_calendar
        mock_daily_builder.return_value = SimpleNamespace(
            dataframe=pd.DataFrame({"date": pd.to_datetime(["2026-06-10"]), "TB7YWI0C": [2.0]}),
            path=Path("/tmp/daily.csv"),
            source="shared_data_service_daily",
            data_version="shared_data_service_daily.v1",
            content_hash="daily_hash",
        )
        mock_weekly_builder.return_value = SimpleNamespace(
            dataframe=pd.DataFrame({"week_id": [202623], "S0114089": [1.0]}),
            path=Path("/tmp/weekly.csv"),
            source="shared_data_service_weekly",
            data_version="shared_data_service_weekly.v1",
            content_hash="weekly_hash",
        )
        mock_monthly_builder.return_value = SimpleNamespace(
            dataframe=pd.DataFrame({"month_id": ["202605"], "M0041342": [9.0]}),
            path=Path("/tmp/monthly.csv"),
            source="shared_data_service_monthly",
            data_version="shared_data_service_monthly.v1",
            content_hash="monthly_hash",
        )
        mock_inference.return_value = {
            "anchor_date": "2026-06-10",
            "prediction": -1,
            "confidence": 1.0,
            "vote_score": -0.72,
            "true_label": None,
            "baseline_signs": {"STD": -1, "DIV": -1, "ACCWT": -1, "CROSS_5Y": -1},
            "baseline_scores": {"STD": -0.7, "DIV": -0.6, "ACCWT": -0.8, "CROSS_5Y": -0.9},
            "model_version": "liwei_0616_7y_03_v31",
        }

        from schemes.liwei_0616_7y03_cons_all_k3_div_k8 import predict

        records = predict.run("2026-06-11")

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.scheme_id, SCHEME_ID)
        self.assertEqual(record.target_tenor, "7Y")
        self.assertEqual(record.horizon, 5)
        self.assertEqual(record.predict_date, "2026-06-11")
        self.assertEqual(record.feature_date, "2026-06-10")
        self.assertEqual(record.target_date, "2026-06-17")
        self.assertEqual(record.predicted_direction, -1)
        self.assertEqual(record.confidence, 1.0)
        self.assertEqual(record.extra["feature_date"], "2026-06-10")
        self.assertEqual(record.extra["source_model_id"], "7Y_03_cons_ALL_k_3_DIV_K_8")
        self.assertEqual(record.extra["baseline_scores"], {"STD": -0.7, "DIV": -0.6, "ACCWT": -0.8, "CROSS_5Y": -0.9})
        self.assertEqual(record.extra["vote_baselines"], ["STD", "DIV", "ACCWT", "CROSS_5Y"])
        self.assertEqual(record.extra["fallback_baseline"], "DIV")
        self.assertEqual(record.extra["streak_K"], 8)
        self.assertEqual(record.extra["model_scope"], "liwei_0616_7y03_source_compatible_context")
        self.assertEqual(record.extra["model_source_end"], "2026-06-10")
        self.assertEqual(record.extra["weekly_input_artifact_source"], "shared_data_service_weekly")
        self.assertEqual(record.extra["monthly_input_artifact_source"], "shared_data_service_monthly")
        self.assertEqual(mock_daily_builder.call_args.kwargs["end_date"], "2026-06-10")
        self.assertEqual(mock_weekly_builder.call_args.kwargs["as_of_date"], "2026-06-10")
        mock_engine.dispose.assert_called_once()


class Liwei06167Y03StaticBoundaryTests(unittest.TestCase):
    """确保 active core 不越过项目边界。"""

    def test_core_has_no_db_platform_csv_pickle_or_cross_scheme_runtime_dependency(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        core_files = [
            project_root / "schemes" / SCHEME_ID / "core" / "data_alignment.py",
            project_root / "schemes" / SCHEME_ID / "core" / "v31_common.py",
        ]
        banned = {"sqlalchemy", "pymysql", "scheduler", "shared", "backend", "backtests", "schemes"}
        banned_text = (
            "pd.read_csv",
            "pickle",
            "daily_output.csv",
            "weekly_output.csv",
            "monthly_output.csv",
            "2026-05-01",
            "2026-04-30",
            "2025-07",
        )
        for core_path in core_files:
            source = core_path.read_text(encoding="utf-8")
            for needle in banned_text:
                self.assertNotIn(needle, source)
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module = getattr(node, "module", None) or ""
                    top = module.split(".")[0]
                    self.assertNotIn(top, banned, f"core 禁止 import {module}")
        source = (project_root / "schemes" / SCHEME_ID / "core" / "v31_common.py").read_text(encoding="utf-8")
        self.assertEqual(source.count('SOURCE_IC_SCREEN_START = "2024-01-01"'), 1)


if __name__ == "__main__":
    unittest.main()
