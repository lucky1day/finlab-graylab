"""liwei_0616 5Y_01 SDA 共识方案单元测试。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd


SCHEME_ID = "liwei_0616_cons_sda_k3_div_k10"


class Liwei0616ConfigTests(unittest.TestCase):
    """config.yaml 入库契约测试。"""

    def test_config_declares_5y_t5_active_auxiliary_inputs_and_benchmark(self) -> None:
        from harness.config_loader import load_config_raw

        project_root = Path(__file__).resolve().parents[1]
        config = load_config_raw(project_root / "schemes" / SCHEME_ID / "config.yaml")

        self.assertEqual(config["scheme_id"], SCHEME_ID)
        self.assertEqual(config["name"], "liwei_0616 5Y_01 SDA共识-DIV回退")
        self.assertEqual(config["horizon"], 5)
        self.assertEqual(config["task_type"], "T+5")
        self.assertEqual(config["tenors"], ["5Y"])
        self.assertEqual(config["frequency"], "daily")
        self.assertEqual(config["status"], "active")
        self.assertEqual(config["schedule"]["cron"], "3 7 * * 1-5")
        self.assertIn("TB5YWI0C", config["input_spec"]["required_columns"])

        aux = config["input_spec"]["auxiliary_inputs"]
        self.assertEqual([item["frequency"] for item in aux], ["weekly", "monthly"])
        self.assertIn("S0114089", aux[0]["required_columns"])
        self.assertIn("M0061518", aux[1]["required_columns"])
        self.assertNotIn("M0041342", aux[1]["required_columns"])
        from schemes.liwei_0616_cons_sda_k3_div_k10.core.v31_common import MONTHLY_COLS

        self.assertIn("M0041342", MONTHLY_COLS)
        self.assertEqual(config["backtest"]["runner"], f"backtests.{SCHEME_ID}_reproduction")
        self.assertEqual(
            config["backtest"]["runner_args"],
            ["--batch-mode", "monthly", "--input-end", "2026-06-10", "--n-workers", "20", "--phase-a-cache"],
        )
        self.assertEqual(config["backtest"]["benchmark_id"], "liwei_0616_5y_01")
        self.assertTrue(config["backtest"]["benchmark_required"])


class Liwei0616CoreTests(unittest.TestCase):
    """共识和 source 配置逻辑测试。"""

    def test_consensus_requires_all_three_sda_baselines_to_agree(self) -> None:
        import numpy as np

        from schemes.liwei_0616_cons_sda_k3_div_k10.core.v31_common import apply_consensus

        signs = {
            "STD": np.array([1, 1, 1, -1, -1, 0]),
            "DIV": np.array([1, 1, -1, -1, 0, -1]),
            "ACCWT": np.array([1, 0, 1, -1, -1, -1]),
        }

        result = apply_consensus(signs, ["STD", "DIV", "ACCWT"], k_agree=3, n_rows=6)

        self.assertEqual(result.tolist(), [1, 0, 0, -1, 0, 0])

    def test_streak_break_uses_div_after_ten_repeated_predictions(self) -> None:
        import numpy as np

        from schemes.liwei_0616_cons_sda_k3_div_k10.core.v31_common import apply_streak_break

        pred = np.array([1] * 12, dtype=np.int32)
        fallback = np.array([1] * 10 + [-1, -1], dtype=np.int32)

        result = apply_streak_break(pred, fallback, streak_k=10)

        self.assertEqual(result.tolist(), [1] * 10 + [-1, -1])

    def test_source_v31_uses_seasonal_vt_and_spread_before_streak_features(self) -> None:
        import numpy as np

        from schemes.liwei_0616_cons_sda_k3_div_k10.core.v31_common import (
            BASELINE_CONFIGS,
            build_bond_features,
        )

        self.assertEqual(BASELINE_CONFIGS["STD"]["vt_mode"], "seasonal")
        self.assertEqual(BASELINE_CONFIGS["DIV"]["vt_mode"], "seasonal")
        self.assertEqual(BASELINE_CONFIGS["ACCWT"]["vt_mode"], "seasonal")
        daily = pd.DataFrame(
            {
                "date": pd.bdate_range("2026-01-01", periods=130),
                "TB5YWI0C": np.linspace(2.0, 2.3, 130),
                "TB3YWI0C": np.linspace(1.8, 2.0, 130),
                "TB0YWI0C": np.linspace(2.2, 2.4, 130),
            }
        )

        features = build_bond_features(daily, "TB5YWI0C", [("3Y", "TB3YWI0C"), ("10Y", "TB0YWI0C")])
        columns = list(features.columns)

        self.assertLess(columns.index("spread_5Y_3Y"), columns.index("5Y_streak_count"))

    def test_weekly_alignment_matches_source_last_trading_day_then_ffill(self) -> None:
        from schemes.liwei_0616_cons_sda_k3_div_k10.core.v31_common import prepare_model_frames

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
        from schemes.liwei_0616_cons_sda_k3_div_k10.core.v31_common import _dynamic_report_masks

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

        from schemes.liwei_0616_cons_sda_k3_div_k10.core import v31_common

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
                "TB5YWI0C": np.linspace(2.0, 2.2, len(dates)),
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
            patch.dict("sys.modules", {"lightgbm": MagicMock()}),
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


class Liwei0616InferenceTests(unittest.TestCase):
    """PIT 窗口与共享 inference 入口测试。"""

    def test_feature_window_uses_source_compatible_fixed_history_context(self) -> None:
        from schemes.liwei_0616_cons_sda_k3_div_k10.inference import liwei_0616_pit_window

        window = liwei_0616_pit_window("2026-06-03")

        self.assertEqual(window.prior_start, "2025-06-01")
        self.assertEqual(window.prior_end, "2025-06-30")
        self.assertEqual(window.latest_start, "2026-06-01")
        self.assertEqual(window.source_end, "2026-06-03")
        self.assertEqual(window.current_start, "2026-06-01")
        self.assertEqual(window.current_end, "2026-06-03")
        self.assertEqual(window.test_ranges, (("2025-06-01", "2025-06-30"), ("2026-06-01", "2026-06-03")))

    def test_source_batch_window_uses_latest_start_and_data_end_like_original_runner(self) -> None:
        from schemes.liwei_0616_cons_sda_k3_div_k10.inference import liwei_0616_pit_window

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
        self.assertEqual(window.test_ranges, (("2025-05-01", "2025-06-30"), ("2026-05-01", "2026-06-10")))

    def test_run_for_feature_date_uses_pit_window_and_selects_exact_feature_date(self) -> None:
        from schemes.liwei_0616_cons_sda_k3_div_k10 import inference

        detail = pd.DataFrame(
            {
                "anchor_date": ["2026-06-02", "2026-06-03"],
                "prediction": [-1, 1],
                "true_label": [None, None],
                "confidence": [1.0, 1.0],
                "vote_score": [-0.5, 0.75],
                "baseline_scores": [
                    {"STD": -0.5, "DIV": -0.4, "ACCWT": -0.6},
                    {"STD": 0.7, "DIV": 0.8, "ACCWT": 0.75},
                ],
            }
        )
        with patch.object(inference, "run_5y01_for_feature_window", return_value=detail) as mock_run:
            row = inference.run_5y01_for_feature_date(
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
        self.assertFalse(kwargs["require_labels"])
        self.assertEqual(row["anchor_date"], "2026-06-03")
        self.assertEqual(row["prediction"], 1)
        self.assertEqual(row["model_version"], "liwei_0616_5y_01_v31")

    def test_live_incremental_cache_is_passed_to_full_source_window(self) -> None:
        from schemes.liwei_0616_cons_sda_k3_div_k10 import inference

        detail = pd.DataFrame(
            {
                "anchor_date": ["2026-06-03"],
                "prediction": [1],
                "confidence": [1.0],
                "vote_score": [0.7],
            }
        )
        phase_a_caches = {"STD": {"test_dates": ["2026-06-03"], "results": []}}
        audit = {
            "status": "extended",
            "watermark": "2026-06-03",
            "missing_dates": ["2026-06-03"],
            "version": "liwei_0616.phase_a.v1",
            "fingerprint": "abc",
            "baselines": {},
        }
        with (
            patch.object(
                inference,
                "build_auxiliary_dependency_projection",
                return_value=object(),
            ),
            patch.object(inference, "prepare_phase_a_caches", return_value=(phase_a_caches, audit)),
            patch.object(inference, "run_5y01_for_feature_window", return_value=detail) as mock_run,
        ):
            row = inference.run_5y01_for_feature_date(
                daily_df=pd.DataFrame(
                    {
                        "date": pd.to_datetime(["2026-06-03"]),
                        "TB5YWI0C": [2.0],
                    }
                ),
                weekly_df=pd.DataFrame({"week_id": [202623]}),
                monthly_df=pd.DataFrame({"month_id": ["202605"]}),
                date_to_week={"2026-06-03": 202623},
                feature_date="2026-06-03",
                require_labels=False,
                n_workers=1,
                use_incremental_cache=True,
            )

        self.assertEqual(mock_run.call_args.kwargs["phase_a_caches"], phase_a_caches)
        self.assertEqual(row["phase_a_cache_audit"], audit)


class Liwei0616PredictionRecordTests(unittest.TestCase):
    """predict.py adapter 输出合规性测试。"""

    @patch("schemes.liwei_0616_cons_sda_k3_div_k10.predict.run_5y01_for_feature_date")
    @patch("schemes.liwei_0616_cons_sda_k3_div_k10.predict.build_monthly_input_artifact")
    @patch("schemes.liwei_0616_cons_sda_k3_div_k10.predict.build_weekly_input_artifact")
    @patch("schemes.liwei_0616_cons_sda_k3_div_k10.predict.build_daily_input_artifact")
    @patch("schemes.liwei_0616_cons_sda_k3_div_k10.predict.create_input_engine")
    @patch("schemes.liwei_0616_cons_sda_k3_div_k10.predict.get_calendar")
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
            dataframe=pd.DataFrame({"date": pd.to_datetime(["2026-06-10"]), "TB5YWI0C": [2.0]}),
            path=Path("/tmp/daily.csv"),
            source="shared_data_service_daily",
            data_version="shared_data_service_daily.v1",
        )
        mock_weekly_builder.return_value = SimpleNamespace(
            dataframe=pd.DataFrame({"week_id": [202623], "S0114089": [1.0]}),
            path=Path("/tmp/weekly.csv"),
            source="shared_data_service_weekly",
            data_version="shared_data_service_weekly.v1",
        )
        mock_monthly_builder.return_value = SimpleNamespace(
            dataframe=pd.DataFrame({"month_id": ["202605"], "M0041342": [9.0]}),
            path=Path("/tmp/monthly.csv"),
            source="shared_data_service_monthly",
            data_version="shared_data_service_monthly.v1",
        )
        mock_inference.return_value = {
            "anchor_date": "2026-06-10",
            "prediction": -1,
            "confidence": 1.0,
            "vote_score": -0.72,
            "true_label": None,
            "baseline_signs": {"STD": -1, "DIV": -1, "ACCWT": -1},
            "baseline_scores": {"STD": -0.71, "DIV": -0.70, "ACCWT": -0.75},
            "model_version": "liwei_0616_5y_01_v31",
            "phase_a_cache_audit": {
                "status": "extended",
                "watermark": "2026-06-10",
                "missing_dates": ["2026-06-10"],
                "version": "liwei_0616.phase_a.v1",
                "fingerprint": "cache-fingerprint",
                "baselines": {"STD": {"status": "extended"}},
            },
        }

        from schemes.liwei_0616_cons_sda_k3_div_k10 import predict

        records = predict.run("2026-06-11")

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.scheme_id, SCHEME_ID)
        self.assertEqual(record.target_tenor, "5Y")
        self.assertEqual(record.horizon, 5)
        self.assertEqual(record.predict_date, "2026-06-11")
        self.assertEqual(record.feature_date, "2026-06-10")
        self.assertEqual(record.target_date, "2026-06-17")
        self.assertEqual(record.predicted_direction, -1)
        self.assertEqual(record.confidence, 1.0)
        self.assertEqual(record.extra["feature_date"], "2026-06-10")
        self.assertEqual(record.extra["source_model_id"], "5Y_01_cons_SDA_k_3_DIV_K_10")
        self.assertEqual(record.extra["baseline_scores"], {"STD": -0.71, "DIV": -0.70, "ACCWT": -0.75})
        self.assertEqual(record.extra["model_scope"], "liwei_0616_5y01_source_compatible_context")
        self.assertEqual(record.extra["model_latest_start"], "2026-06-01")
        self.assertEqual(record.extra["model_source_end"], "2026-06-10")
        self.assertEqual(record.extra["vote_baselines"], ["STD", "DIV", "ACCWT"])
        self.assertEqual(record.extra["fallback_baseline"], "DIV")
        self.assertEqual(record.extra["weekly_input_artifact_source"], "shared_data_service_weekly")
        self.assertEqual(record.extra["monthly_input_artifact_source"], "shared_data_service_monthly")
        self.assertEqual(record.extra["phase_a_cache_status"], "extended")
        self.assertEqual(record.extra["phase_a_cache_watermark"], "2026-06-10")
        self.assertEqual(record.extra["phase_a_cache_missing_dates"], ["2026-06-10"])
        self.assertEqual(record.extra["phase_a_cache_version"], "liwei_0616.phase_a.v1")
        self.assertEqual(record.extra["phase_a_cache_fingerprint"], "cache-fingerprint")
        self.assertEqual(mock_inference.call_args.kwargs["use_incremental_cache"], True)
        self.assertEqual(mock_daily_builder.call_args.kwargs["start_date"], predict.INPUT_START_DATE)
        self.assertEqual(mock_daily_builder.call_args.kwargs["end_date"], "2026-06-10")
        self.assertEqual(mock_weekly_builder.call_args.kwargs["as_of_date"], "2026-06-10")
        mock_engine.dispose.assert_called_once()


class Liwei0616StaticBoundaryTests(unittest.TestCase):
    """确保 active core 不越过项目边界。"""

    def test_core_has_no_db_platform_csv_or_pickle_runtime_dependency(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        core_files = [
            project_root / "schemes" / SCHEME_ID / "core" / "data_alignment.py",
            project_root / "schemes" / SCHEME_ID / "core" / "v31_common.py",
        ]
        banned = {"sqlalchemy", "pymysql", "scheduler", "shared", "backend", "backtests"}
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
