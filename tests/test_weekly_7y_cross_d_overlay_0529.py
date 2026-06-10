"""weekly_7y_cross_d_overlay_0529 方案单元测试。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd


def _make_weekly_df(week_ids: list[int] | None = None) -> pd.DataFrame:
    """构建覆盖 7Y Cross-D 所需列的周频 DataFrame。"""
    if week_ids is None:
        week_ids = list(range(202601, 202633))
    n = len(week_ids)
    idx = np.arange(n, dtype=float)
    return pd.DataFrame(
        {
            "week_id": week_ids,
            "TB1YWI3C": 1.80 + 0.025 * idx + 0.010 * np.sin(idx),
            "TB3YWI3C": 1.90 + 0.018 * idx + 0.015 * np.cos(idx / 2),
            "TB5YWI3C": 2.00 + 0.031 * idx + 0.020 * np.sin(idx / 3),
            "TB7YWI3C": 1.70 + 0.020 * idx + 0.015 * np.cos(idx / 4),
            "TB0YWI3C": 2.30 + 0.027 * idx + 0.010 * np.sin(idx / 5),
        }
    )


class CrossDOverlayCoreTests(unittest.TestCase):
    """core/cross_d_overlay.py 纯算法测试。"""

    def test_build_cross_d_overlay_returns_final_7y_signal(self) -> None:
        from schemes.weekly_7y_cross_d_overlay_0529.core.cross_d_overlay import (
            build_cross_d_overlay,
        )

        result = build_cross_d_overlay(_make_weekly_df())

        self.assertFalse(result.empty)
        for col in [
            "week_id",
            "main_pred_label",
            "main_prob_up",
            "d5_d_pred_label",
            "cross_d_overlay",
            "cross_d_pred_label",
            "cross_d_prob_up",
            "cross_d_signal_source",
        ]:
            self.assertIn(col, result.columns)
        self.assertTrue(result["cross_d_pred_label"].isin([-1, 1]).all())
        self.assertTrue(result["cross_d_prob_up"].between(0.0, 1.0).all())

    def test_core_requires_weekly_columns(self) -> None:
        from schemes.weekly_7y_cross_d_overlay_0529.core.cross_d_overlay import (
            build_cross_d_overlay,
        )

        with self.assertRaisesRegex(ValueError, "缺少必要列"):
            build_cross_d_overlay(_make_weekly_df().drop(columns=["TB3YWI3C"]))


class PredictionRecordTests(unittest.TestCase):
    """predict.py adapter 输出合规性测试。"""

    WEEKLY_EXTRA_KEYS = {
        "feature_week_id",
        "target_week_id",
        "feature_date",
        "target_date",
        "target_rule",
        "input_artifact_path",
        "input_artifact_source",
    }

    def _mock_dependencies(self) -> tuple[MagicMock, MagicMock, MagicMock]:
        mock_cal = MagicMock()
        mock_cal.week_id_for_date.side_effect = lambda day: {
            "2026-06-13": None,
            "2026-06-12": 202624,
            "2026-06-19": 202625,
            "2027-01-04": 202701,
        }.get(day)
        mock_cal.next_trading_days.side_effect = lambda day, count: {
            "2026-06-12": ["2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18", "2026-06-19"],
            "2026-12-31": ["2027-01-04"],
        }.get(day, [])
        mock_cal.week_id_to_last_trading_day.side_effect = lambda wid: {
            202624: "2026-06-12",
            202625: "2026-06-19",
            202652: "2026-12-31",
            202701: "2027-01-04",
        }[int(wid)]

        mock_artifact = MagicMock()
        mock_artifact.dataframe = _make_weekly_df(list(range(202601, 202625)))
        mock_artifact.path = Path("/tmp/weekly_7y.csv")
        mock_artifact.source = "shared_data_service_weekly"

        mock_engine = MagicMock()
        return mock_cal, mock_artifact, mock_engine

    @patch("schemes.weekly_7y_cross_d_overlay_0529.predict.build_weekly_input_artifact")
    @patch("schemes.weekly_7y_cross_d_overlay_0529.predict.data_service")
    @patch("schemes.weekly_7y_cross_d_overlay_0529.predict.get_calendar")
    def test_run_returns_one_7y_prediction_with_weekly_extra(
        self,
        mock_get_cal: MagicMock,
        mock_ds: MagicMock,
        mock_build: MagicMock,
    ) -> None:
        mock_cal, mock_artifact, mock_engine = self._mock_dependencies()
        mock_get_cal.return_value = mock_cal
        mock_ds.create_sqlalchemy_engine.return_value = mock_engine
        mock_build.return_value = mock_artifact

        from schemes.weekly_7y_cross_d_overlay_0529 import predict

        records = predict.run("2026-06-13")

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.scheme_id, "weekly_7y_cross_d_overlay_0529")
        self.assertEqual(record.horizon, 6)
        self.assertEqual(record.target_tenor, "7Y")
        self.assertEqual(record.predict_date, "2026-06-13")
        self.assertEqual(record.target_date, "2026-06-19")
        self.assertIn(record.predicted_direction, {-1, 0, 1})
        self.assertSetEqual(self.WEEKLY_EXTRA_KEYS - set((record.extra or {}).keys()), set())
        self.assertEqual(record.extra["feature_week_id"], 202624)
        self.assertEqual(record.extra["target_week_id"], 202625)
        self.assertEqual(record.extra["target_date"], "2026-06-19")

    @patch("schemes.weekly_7y_cross_d_overlay_0529.predict.build_weekly_input_artifact")
    @patch("schemes.weekly_7y_cross_d_overlay_0529.predict.data_service")
    @patch("schemes.weekly_7y_cross_d_overlay_0529.predict.get_calendar")
    def test_run_loads_at_least_six_future_weeks(
        self,
        mock_get_cal: MagicMock,
        mock_ds: MagicMock,
        mock_build: MagicMock,
    ) -> None:
        mock_cal, mock_artifact, mock_engine = self._mock_dependencies()
        mock_get_cal.return_value = mock_cal
        mock_ds.create_sqlalchemy_engine.return_value = mock_engine
        mock_build.return_value = mock_artifact

        from schemes.weekly_7y_cross_d_overlay_0529 import predict

        predict.run("2026-06-13")

        self.assertEqual(mock_build.call_args.kwargs["start_week"], 202624 - predict.LOOKBACK_WEEKS)
        self.assertGreaterEqual(mock_build.call_args.kwargs["end_week"], 202624 + 6)
        self.assertEqual(mock_build.call_args.kwargs["schema_columns"], predict.SCHEMA_COLUMNS)

    def test_next_week_id_uses_db_calendar_not_numeric_increment(self) -> None:
        from schemes.weekly_7y_cross_d_overlay_0529 import predict

        mock_cal, _, _ = self._mock_dependencies()

        self.assertEqual(predict._next_calendar_week_id(mock_cal, 202652), 202701)


class WeekIdIntegrityTests(unittest.TestCase):
    """确保活跃代码不使用计算型周历。"""

    FORBIDDEN_PATTERNS = [
        "fromisocalendar",
        "isocalendar",
        "isoweek",
        "week_id_to_friday",
        "week_id_to_monday",
    ]

    def test_active_files_do_not_contain_forbidden_calendar_patterns(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        files = [
            project_root / "schemes" / "weekly_7y_cross_d_overlay_0529" / "predict.py",
            project_root / "schemes" / "weekly_7y_cross_d_overlay_0529" / "core" / "cross_d_overlay.py",
        ]
        for path in files:
            source = path.read_text(encoding="utf-8")
            for pattern in self.FORBIDDEN_PATTERNS:
                self.assertNotIn(pattern, source, f"{path} 禁止使用日历公式: {pattern}")

    def test_core_has_no_db_or_platform_imports(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        core_path = project_root / "schemes" / "weekly_7y_cross_d_overlay_0529" / "core" / "cross_d_overlay.py"
        tree = ast.parse(core_path.read_text(encoding="utf-8"))
        banned = {"sqlalchemy", "pymysql", "scheduler", "shared", "backend", "backtests"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, "module", None) or ""
                top = module.split(".")[0]
                self.assertNotIn(top, banned, f"core 禁止 import {module}")


if __name__ == "__main__":
    unittest.main()
