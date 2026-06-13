"""weekly_10y_d_overlay_0529 方案单元测试。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd


def _make_weekly_df(week_ids: list[int] | None = None) -> pd.DataFrame:
    """构建 10Y D-overlay 所需的最小周频 DataFrame。"""
    if week_ids is None:
        week_ids = list(range(202601, 202625))
    return pd.DataFrame(
        {
            "week_id": week_ids,
            "TB0YWI3C": [2.30 + index * 0.01 for index in range(len(week_ids))],
            "TB1YWI3C": [1.60 + index * 0.01 for index in range(len(week_ids))],
            "TB5YWI3C": [2.00 + index * 0.01 for index in range(len(week_ids))],
        }
    )


class TenYDOverlayCoreTests(unittest.TestCase):
    """core/d_overlay.py 纯算法测试。"""

    def test_apply_d_overlay_uses_model2_when_score_low_confidence(self) -> None:
        from schemes.weekly_10y_d_overlay_0529.core.d_overlay import apply_d_overlay

        base = pd.DataFrame(
            {
                "week_id": [202610, 202611],
                "month_date": pd.to_datetime(["2026-03-06", "2026-03-13"]),
                "actual_label": [-1, 1],
                "score_pred_label": [1, 1],
                "score_prob_up": [0.54, 0.70],
                "model2_prob_up": [0.30, 0.30],
                "TB0YWI3C": [1.80, 1.82],
            }
        )

        result = apply_d_overlay(base)

        self.assertTrue(bool(result.loc[0, "d_model2_overlay"]))
        self.assertEqual(int(result.loc[0, "d_pred_label"]), -1)
        self.assertAlmostEqual(float(result.loc[0, "d_prob_up"]), 0.30)
        self.assertFalse(bool(result.loc[1, "d_model2_overlay"]))
        self.assertEqual(int(result.loc[1, "d_pred_label"]), 1)

    def test_build_d_overlay_requires_db_week_date_column(self) -> None:
        from schemes.weekly_10y_d_overlay_0529.core.d_overlay import build_d_overlay

        with self.assertRaisesRegex(ValueError, "week_date"):
            build_d_overlay(_make_weekly_df())

    def test_build_base_does_not_hardcode_2025h2_output_start(self) -> None:
        from schemes.weekly_10y_d_overlay_0529.core.d_overlay import build_base

        model2 = pd.DataFrame(
            {
                "week_id": [202501, 202527],
                "week_date": pd.to_datetime(["2025-01-03", "2025-07-04"]),
                "actual_label": [1, -1],
                "model2_prob_up": [0.61, 0.32],
                "TB0YWI3C": [2.30, 2.10],
            }
        )
        score = pd.DataFrame(
            {
                "week_id": [202501, 202527],
                "score_pred_label": [1, -1],
                "score_prob_up": [0.60, 0.35],
                "score_actual_label": [1, -1],
            }
        )

        result = build_base(model2, score)

        self.assertEqual(result["week_id"].astype(int).tolist(), [202501, 202527])

    def test_broad_features_excludes_db_week_date(self) -> None:
        from schemes.weekly_10y_d_overlay_0529.core.d_overlay import broad_features

        weekly = _make_weekly_df(list(range(202601, 202610)))
        weekly["week_date"] = pd.date_range("2026-01-02", periods=len(weekly), freq="7D")
        weekly["model_date"] = pd.date_range("2025-12-29", periods=len(weekly), freq="7D")
        weekly["numeric_factor"] = list(range(len(weekly)))
        base = pd.DataFrame({"base_signal": range(len(weekly))})

        features = broad_features(weekly, base, "TB0YWI3C", 202606)

        self.assertIn("numeric_factor", features.columns)
        self.assertNotIn("week_date", features.columns)
        self.assertNotIn("model_date", features.columns)


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
        mock_cal.previous_trading_day.side_effect = lambda day: {
            "2026-06-12": "2026-06-11",
            "2026-06-13": "2026-06-12",
        }.get(day, "2026-06-11")
        mock_cal.next_trading_days.side_effect = lambda day, count: {
            "2026-06-12": ["2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18", "2026-06-19"],
            "2026-12-31": ["2027-01-04"],
        }.get(day, [])
        def last_trading_day(wid: int) -> str:
            mapped = {
                202624: "2026-06-12",
                202625: "2026-06-19",
                202652: "2026-12-31",
                202701: "2027-01-04",
            }
            if int(wid) in mapped:
                return mapped[int(wid)]
            offset = int(wid) - 202601
            return (pd.Timestamp("2026-01-02") + pd.Timedelta(days=7 * offset)).strftime("%Y-%m-%d")

        mock_cal.week_id_to_last_trading_day.side_effect = last_trading_day

        def week_id_for_date(day: str) -> int | None:
            if day == "2026-06-13":
                return None
            if day == "2026-06-11":
                return 202624
            target = pd.Timestamp(day)
            for wid in range(202601, 202626):
                last = pd.Timestamp(last_trading_day(wid))
                if last - pd.Timedelta(days=4) <= target <= last:
                    return wid
            if day == "2027-01-04":
                return 202701
            return None

        mock_cal.week_id_for_date.side_effect = week_id_for_date

        mock_artifact = MagicMock()
        mock_artifact.dataframe = _make_weekly_df(list(range(202601, 202625)))
        mock_artifact.path = Path("/tmp/weekly_10y.csv")
        mock_artifact.source = "shared_data_service_weekly"

        mock_engine = MagicMock()
        return mock_cal, mock_artifact, mock_engine

    @patch("schemes.weekly_10y_d_overlay_0529.predict.build_d_overlay")
    @patch("schemes.weekly_10y_d_overlay_0529.predict.build_weekly_input_artifact")
    @patch("schemes.weekly_10y_d_overlay_0529.predict.create_input_engine")
    @patch("schemes.weekly_10y_d_overlay_0529.predict.get_calendar")
    def test_run_returns_one_10y_prediction_with_weekly_extra(
        self,
        mock_get_cal: MagicMock,
        mock_ds: MagicMock,
        mock_build: MagicMock,
        mock_d_overlay: MagicMock,
    ) -> None:
        mock_cal, mock_artifact, mock_engine = self._mock_dependencies()
        mock_get_cal.return_value = mock_cal
        mock_ds.return_value = mock_engine
        mock_build.return_value = mock_artifact
        mock_d_overlay.return_value = pd.DataFrame(
            {
                "week_id": [202624],
                "d_pred_label": [-1],
                "d_prob_up": [0.28],
                "d_model2_overlay": [False],
                "d_signal_source": ["score_main"],
                "score_pred_label": [-1],
                "score_prob_up": [0.28],
                "model2_prob_up": [0.61],
                "d_model2_pred_label": [1],
            }
        )

        from schemes.weekly_10y_d_overlay_0529 import predict

        records = predict.run("2026-06-13")

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.scheme_id, "weekly_10y_d_overlay_0529")
        self.assertEqual(record.horizon, 6)
        self.assertEqual(record.target_tenor, "10Y")
        self.assertEqual(record.predict_date, "2026-06-13")
        self.assertEqual(record.target_date, "2026-06-19")
        self.assertEqual(record.predicted_direction, -1)
        self.assertAlmostEqual(record.confidence, 0.28)
        self.assertSetEqual(self.WEEKLY_EXTRA_KEYS - set((record.extra or {}).keys()), set())
        self.assertEqual(record.extra["feature_week_id"], 202624)
        self.assertEqual(record.extra["target_week_id"], 202625)
        self.assertEqual(record.extra["target_date"], "2026-06-19")

    @patch("schemes.weekly_10y_d_overlay_0529.predict.build_d_overlay")
    @patch("schemes.weekly_10y_d_overlay_0529.predict.build_weekly_input_artifact")
    @patch("schemes.weekly_10y_d_overlay_0529.predict.create_input_engine")
    @patch("schemes.weekly_10y_d_overlay_0529.predict.get_calendar")
    def test_run_uses_feature_week_as_of_for_weekly_artifact(
        self,
        mock_get_cal: MagicMock,
        mock_ds: MagicMock,
        mock_build: MagicMock,
        mock_d_overlay: MagicMock,
    ) -> None:
        mock_cal, mock_artifact, mock_engine = self._mock_dependencies()
        mock_get_cal.return_value = mock_cal
        mock_ds.return_value = mock_engine
        mock_build.return_value = mock_artifact
        mock_d_overlay.return_value = pd.DataFrame(
            {
                "week_id": [202624],
                "d_pred_label": [-1],
                "d_prob_up": [0.28],
                "d_model2_overlay": [False],
                "d_signal_source": ["score_main"],
                "score_pred_label": [-1],
                "score_prob_up": [0.28],
                "model2_prob_up": [0.61],
                "d_model2_pred_label": [1],
            }
        )

        from schemes.weekly_10y_d_overlay_0529 import predict

        predict.run("2026-06-13")

        self.assertEqual(mock_build.call_args.kwargs["start_week"], 202624 - predict.LOOKBACK_WEEKS)
        self.assertEqual(mock_build.call_args.kwargs["end_week"], 202624)
        self.assertEqual(mock_build.call_args.kwargs["as_of_date"], "2026-06-12")
        self.assertIsNone(mock_build.call_args.kwargs["schema_columns"])
        passed_to_core = mock_d_overlay.call_args.args[0]
        self.assertIn("week_date", passed_to_core.columns)
        self.assertIn("model_date", passed_to_core.columns)
        self.assertEqual(str(passed_to_core.loc[passed_to_core["week_id"].eq(202624), "week_date"].iloc[0])[:10], "2026-06-12")
        self.assertEqual(str(passed_to_core.loc[passed_to_core["week_id"].eq(202624), "model_date"].iloc[0])[:10], "2026-06-08")

    @patch("schemes.weekly_10y_d_overlay_0529.predict.build_d_overlay")
    @patch("schemes.weekly_10y_d_overlay_0529.predict.build_weekly_input_artifact")
    @patch("schemes.weekly_10y_d_overlay_0529.predict.create_input_engine")
    @patch("schemes.weekly_10y_d_overlay_0529.predict.get_calendar")
    def test_run_uses_previous_trading_day_for_trading_predict_date(
        self,
        mock_get_cal: MagicMock,
        mock_ds: MagicMock,
        mock_build: MagicMock,
        mock_d_overlay: MagicMock,
    ) -> None:
        mock_cal, mock_artifact, mock_engine = self._mock_dependencies()
        mock_get_cal.return_value = mock_cal
        mock_ds.return_value = mock_engine
        mock_build.return_value = mock_artifact
        mock_d_overlay.return_value = pd.DataFrame(
            {
                "week_id": [202624],
                "d_pred_label": [-1],
                "d_prob_up": [0.28],
                "d_model2_overlay": [False],
                "d_signal_source": ["score_main"],
                "score_pred_label": [-1],
                "score_prob_up": [0.28],
                "model2_prob_up": [0.61],
                "d_model2_pred_label": [1],
            }
        )

        from schemes.weekly_10y_d_overlay_0529 import predict

        records = predict.run("2026-06-12")

        self.assertEqual(records[0].feature_date, "2026-06-11")
        self.assertEqual(records[0].extra["feature_date"], "2026-06-11")
        self.assertEqual(mock_build.call_args.kwargs["as_of_date"], "2026-06-11")
        mock_cal.previous_trading_day.assert_called_with("2026-06-12")

    def test_next_week_id_uses_shared_prediction_context(self) -> None:
        from shared.prediction_context import next_calendar_week_id

        mock_cal, _, _ = self._mock_dependencies()

        self.assertEqual(next_calendar_week_id(mock_cal, 202652), 202701)


class WeekIdIntegrityTests(unittest.TestCase):
    """确保活跃代码不使用计算型周历。"""

    FORBIDDEN_PATTERNS = [
        "fromisocalendar",
        "isocalendar",
        "isoweek",
        "week_id_to_friday",
        "week_id_to_monday",
        "week_id_to_date",
    ]

    def test_active_files_do_not_contain_forbidden_calendar_patterns(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        files = [
            project_root / "schemes" / "weekly_10y_d_overlay_0529" / "predict.py",
            project_root / "schemes" / "weekly_10y_d_overlay_0529" / "core" / "d_overlay.py",
        ]
        for path in files:
            source = path.read_text(encoding="utf-8")
            for pattern in self.FORBIDDEN_PATTERNS:
                self.assertNotIn(pattern, source, f"{path} 禁止使用日历公式: {pattern}")

    def test_adapter_uses_shared_prediction_context(self) -> None:
        pred_path = Path(__file__).resolve().parents[1] / "schemes" / "weekly_10y_d_overlay_0529" / "predict.py"
        source = pred_path.read_text(encoding="utf-8")

        self.assertIn("from shared.prediction_context import", source)
        self.assertIn("build_weekly_live_context", source)
        self.assertNotIn("def _next_calendar_week_id", source)

    def test_core_has_no_db_or_platform_imports(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        core_path = project_root / "schemes" / "weekly_10y_d_overlay_0529" / "core" / "d_overlay.py"
        tree = ast.parse(core_path.read_text(encoding="utf-8"))
        banned = {"sqlalchemy", "pymysql", "scheduler", "shared", "backend", "backtests"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, "module", None) or ""
                top = module.split(".")[0]
                self.assertNotIn(top, banned, f"core 禁止 import {module}")


if __name__ == "__main__":
    unittest.main()
