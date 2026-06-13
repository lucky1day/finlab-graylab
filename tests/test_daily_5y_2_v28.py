"""daily_5y_2_v28 方案单元测试。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd


class Daily5Y2ConfigTests(unittest.TestCase):
    """config.yaml 入库契约测试。"""

    def test_config_declares_auxiliary_inputs_and_benchmark_required(self) -> None:
        from harness.config_loader import load_config_raw

        project_root = Path(__file__).resolve().parents[1]
        config = load_config_raw(project_root / "schemes" / "daily_5y_2_v28" / "config.yaml")

        self.assertEqual(config["scheme_id"], "daily_5y_2_v28")
        self.assertEqual(config["horizon"], 5)
        self.assertEqual(config["tenors"], ["5Y"])
        self.assertEqual(config["frequency"], "daily")
        self.assertEqual(config["status"], "active")
        self.assertEqual(config["schedule"]["cron"], "3 7 * * 1-5")
        self.assertEqual(config["input_spec"]["data_version"], "shared_data_service_daily.v1")
        aux = config["input_spec"]["auxiliary_inputs"]
        self.assertEqual([item["frequency"] for item in aux], ["weekly", "monthly"])
        self.assertEqual(aux[0]["data_version"], "shared_data_service_weekly.v1")
        self.assertEqual(aux[1]["data_version"], "shared_data_service_monthly.v1")
        self.assertTrue(config["backtest"]["benchmark_required"])
        self.assertEqual(config["backtest"]["runner"], "backtests.daily_5y_2_v28_reproduction")


class Daily5Y2AlignmentTests(unittest.TestCase):
    """core 输入对齐和日期语义测试。"""

    def test_make_labels_uses_anchor_to_t_plus_5(self) -> None:
        from schemes.daily_5y_2_v28.core.v28_common import make_labels

        df = pd.DataFrame({"TB5YWI0C": [100.0, 101.0, 102.0, 103.0, 104.0, 99.0, 105.0]})

        labels = make_labels(df, "TB5YWI0C", horizon=5)

        self.assertEqual(labels[0], -1)
        self.assertEqual(labels[1], 1)
        self.assertTrue(pd.isna(labels[2]))

    def test_weekly_alignment_uses_previous_complete_week_only(self) -> None:
        from schemes.daily_5y_2_v28.core.data_alignment import align_weekly_previous_complete

        daily_dates = pd.to_datetime(["2026-01-05", "2026-01-09", "2026-01-12"])
        weekly = pd.DataFrame(
            {
                "week_id": [202601, 202602, 202603],
                "N1355677": [10.0, 20.0, 30.0],
            }
        )
        date_to_week = {
            "2026-01-05": 202602,
            "2026-01-09": 202602,
            "2026-01-12": 202603,
        }

        aligned = align_weekly_previous_complete(weekly, daily_dates, date_to_week)

        self.assertEqual(aligned["N1355677"].tolist(), [10.0, 10.0, 20.0])

    def test_monthly_alignment_uses_previous_month_id_without_zfill(self) -> None:
        from schemes.daily_5y_2_v28.core.data_alignment import align_monthly_previous_month

        daily_dates = pd.to_datetime(["2026-01-05", "2026-02-02", "2026-03-16"])
        monthly = pd.DataFrame(
            {
                "month_id": ["202512", "202601", "202602"],
                "M0061518": [9.5, 9.0, 8.8],
            }
        )

        aligned = align_monthly_previous_month(monthly, daily_dates)

        self.assertEqual(aligned["M0061518"].tolist(), [9.5, 9.0, 8.8])

    def test_backtest_passes_db_calendar_week_mapping_to_core(self) -> None:
        from backtests import daily_5y_2_v28_reproduction as runner

        daily_df = pd.DataFrame({"date": pd.to_datetime(["2025-01-02", "2025-01-03"]), "TB5YWI0C": [2.0, 2.1]})
        weekly_df = pd.DataFrame({"week_id": [202501], "N1355677": [1.0]})
        monthly_df = pd.DataFrame({"month_id": ["202412"], "M0061518": [9.0]})
        date_to_week = {"2025-01-02": 202501, "2025-01-03": 202501}

        with patch.object(runner, "run_prediction", return_value=pd.DataFrame()) as mock_run:
            runner.run_historical_prediction(
                daily_df=daily_df,
                weekly_df=weekly_df,
                monthly_df=monthly_df,
                date_to_week=date_to_week,
                n_workers=1,
            )

        cfg = mock_run.call_args.args[0]
        self.assertEqual(cfg["date_to_week"], date_to_week)


class Daily5Y2PredictionRecordTests(unittest.TestCase):
    """predict.py adapter 输出合规性测试。"""

    @patch("schemes.daily_5y_2_v28.predict.run_latest_prediction")
    @patch("schemes.daily_5y_2_v28.predict.build_monthly_input_artifact")
    @patch("schemes.daily_5y_2_v28.predict.build_weekly_input_artifact")
    @patch("schemes.daily_5y_2_v28.predict.build_daily_input_artifact")
    @patch("schemes.daily_5y_2_v28.predict.create_input_engine")
    @patch("schemes.daily_5y_2_v28.predict.get_calendar")
    def test_run_uses_previous_trading_day_as_anchor_and_predict_date_as_signal_date(
        self,
        mock_get_calendar: MagicMock,
        mock_data_service: MagicMock,
        mock_daily_builder: MagicMock,
        mock_weekly_builder: MagicMock,
        mock_monthly_builder: MagicMock,
        mock_latest: MagicMock,
    ) -> None:
        mock_engine = MagicMock()
        mock_data_service.return_value = mock_engine
        mock_calendar = MagicMock()
        mock_calendar.next_trading_days.side_effect = lambda day, count: [
            "2026-05-25",
            "2026-05-26",
            "2026-05-27",
            "2026-05-28",
            "2026-05-29",
            "2026-06-01",
            "2026-06-02",
            "2026-06-03",
            "2026-06-04",
            "2026-06-05",
        ]
        mock_calendar.nth_trading_day_after.return_value = "2026-06-05"
        mock_calendar.week_id_for_date.side_effect = lambda day: 202622 if day == "2026-05-29" else 202623
        mock_get_calendar.return_value = mock_calendar
        mock_daily_builder.return_value = SimpleNamespace(
            dataframe=pd.DataFrame({"date": pd.to_datetime(["2026-05-29"]), "TB5YWI0C": [2.0]}),
            path=Path("/tmp/daily.csv"),
            source="shared_data_service_daily",
            data_version="shared_data_service_daily.v1",
        )
        mock_weekly_builder.return_value = SimpleNamespace(
            dataframe=pd.DataFrame({"week_id": [202621], "N1355677": [1.0]}),
            path=Path("/tmp/weekly.csv"),
            source="shared_data_service_weekly",
            data_version="shared_data_service_weekly.v1",
        )
        mock_monthly_builder.return_value = SimpleNamespace(
            dataframe=pd.DataFrame({"month_id": ["202504"], "M0061518": [9.0]}),
            path=Path("/tmp/monthly.csv"),
            source="shared_data_service_monthly",
            data_version="shared_data_service_monthly.v1",
        )
        mock_latest.return_value = {
            "anchor_date": "2026-05-29",
            "prediction": -1,
            "confidence": 1.0,
            "vote_score": -0.72,
            "true_label": None,
            "model_version": "5y_2_v28",
        }

        from schemes.daily_5y_2_v28 import predict

        records = predict.run("2026-06-01")

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.scheme_id, "daily_5y_2_v28")
        self.assertEqual(record.target_tenor, "5Y")
        self.assertEqual(record.horizon, 5)
        self.assertEqual(record.predict_date, "2026-06-01")
        self.assertEqual(record.target_date, "2026-06-05")
        self.assertEqual(record.predicted_direction, -1)
        self.assertEqual(record.extra["feature_date"], "2026-05-29")
        self.assertEqual(record.extra["anchor_date"], "2026-05-29")
        self.assertEqual(record.extra["signal_date"], "2026-06-01")
        self.assertEqual(record.extra["daily_input_artifact_source"], "shared_data_service_daily")
        self.assertEqual(record.extra["weekly_input_artifact_source"], "shared_data_service_weekly")
        self.assertEqual(record.extra["monthly_input_artifact_source"], "shared_data_service_monthly")
        self.assertEqual(mock_daily_builder.call_args.kwargs["end_date"], "2026-05-29")
        mock_weekly_builder.assert_called_once()
        mock_monthly_builder.assert_called_once()
        mock_engine.dispose.assert_called_once()


class Daily5Y2StaticBoundaryTests(unittest.TestCase):
    """确保 active core 不越过项目边界。"""

    def test_core_has_no_db_or_platform_imports(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        core_files = [
            project_root / "schemes" / "daily_5y_2_v28" / "core" / "data_alignment.py",
            project_root / "schemes" / "daily_5y_2_v28" / "core" / "v28_common.py",
        ]
        banned = {"sqlalchemy", "pymysql", "scheduler", "shared", "backend", "backtests"}
        for core_path in core_files:
            tree = ast.parse(core_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module = getattr(node, "module", None) or ""
                    top = module.split(".")[0]
                    self.assertNotIn(top, banned, f"core 禁止 import {module}")

    def test_core_has_no_csv_input_fallback(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        core_path = project_root / "schemes" / "daily_5y_2_v28" / "core" / "v28_common.py"
        source = core_path.read_text(encoding="utf-8")

        self.assertNotIn("daily_output.csv", source)
        self.assertNotIn("weekly_output.csv", source)
        self.assertNotIn("monthly_output.csv", source)
        self.assertNotIn("pd.read_csv", source)


if __name__ == "__main__":
    unittest.main()
