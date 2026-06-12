from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd


class Weekly5YDirect0529BacktestTests(unittest.TestCase):
    """weekly_5y_direct_0529 历史回测 runner 测试。"""

    def test_config_declares_backtest_runner_and_range(self) -> None:
        from harness.config_loader import load_config_raw

        project_root = Path(__file__).resolve().parents[1]
        config = load_config_raw(project_root / "schemes" / "weekly_5y_direct_0529" / "config.yaml")

        backtest = config.get("backtest")
        self.assertIsInstance(backtest, dict)
        self.assertEqual(backtest["runner"], "backtests.weekly_5y_direct_0529_reproduction")
        self.assertEqual(backtest["benchmark_id"], "model_muti_0529")
        self.assertIsInstance(backtest["start_week"], int)
        self.assertIsInstance(backtest["end_week"], int)
        self.assertLess(backtest["start_week"], backtest["end_week"])

    def test_prediction_rows_use_db_calendar_and_compact_json_rows(self) -> None:
        from backtests import weekly_5y_direct_0529_reproduction as runner

        weekly_df = _weekly_frame([202601, 202602, 202603, 202604])
        calendar = _calendar_for(weekly_df["week_id"])
        vote_calls: list[list[int]] = []

        def fake_vote(frame: pd.DataFrame) -> pd.DataFrame:
            weeks = [int(value) for value in frame["week_id"].tolist()]
            vote_calls.append(weeks)
            feature_week = weeks[-1]
            return pd.DataFrame(
                {
                    "week_id": [feature_week],
                    "final_pred_label": [1 if feature_week % 2 == 0 else -1],
                    "final_prob_up": [0.55 if feature_week % 2 == 0 else 0.45],
                    "rule_vote": [1.0 if feature_week % 2 == 0 else -1.0],
                    "source_spec": ["demo_rule"],
                    "score_spec": ["demo_rule:1.0000"],
                }
            )

        with patch.object(runner, "build_rule_vote", side_effect=fake_vote):
            rows = runner.build_backtest_rows(
                weekly_df,
                calendar=calendar,
                artifact_path=Path("/tmp/weekly.csv"),
                artifact_source="unit_test",
            )

        self.assertEqual(vote_calls, [[202601], [202601, 202602], [202601, 202602, 202603]])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["predict_date"], "2026-01-03")
        self.assertEqual(rows[0]["target_date"], "2026-01-09")
        self.assertEqual(rows[0]["extra"]["feature_week_id"], 202601)
        self.assertEqual(rows[0]["extra"]["target_week_id"], 202602)
        self.assertEqual(rows[0]["extra"]["input_artifact_source"], "unit_test")
        self.assertEqual(rows[0]["label"], 1)

        compact = runner.compact_prediction_rows(rows)
        self.assertEqual(
            compact[0],
            {
                "predict_date": "2026-01-03",
                "target_date": "2026-01-09",
                "target_tenor": "5Y",
                "predicted_direction": -1,
                "confidence": 0.45,
            },
        )

    def test_backtest_rows_start_from_predict_date_2025_01_01(self) -> None:
        from backtests import weekly_5y_direct_0529_reproduction as runner

        weekly_df = _weekly_frame([202452, 202501, 202502])
        calendar = SimpleNamespace(
            week_id_to_last_trading_day=lambda week_id: {
                202452: "2024-12-27",
                202501: "2025-01-03",
                202502: "2025-01-10",
            }[int(week_id)]
        )

        def fake_vote(frame: pd.DataFrame) -> pd.DataFrame:
            feature_week = int(frame["week_id"].iloc[-1])
            return pd.DataFrame(
                {
                    "week_id": [feature_week],
                    "final_pred_label": [1],
                    "final_prob_up": [0.55],
                    "rule_vote": [1.0],
                    "source_spec": ["unit"],
                    "score_spec": ["unit:1.0000"],
                }
            )

        with patch.object(runner, "build_rule_vote", side_effect=fake_vote):
            rows = runner.build_backtest_rows(
                weekly_df,
                calendar=calendar,
                artifact_path=Path("/tmp/weekly.csv"),
                artifact_source="unit_test",
            )

        self.assertEqual([row["predict_date"] for row in rows], ["2025-01-04"])
        self.assertTrue(all(row["predict_date"] >= runner.BACKTEST_PREDICT_START_DATE for row in rows))

    def test_run_no_persist_returns_backtest_gate_and_sop_payload(self) -> None:
        from backtests import weekly_5y_direct_0529_reproduction as runner

        weekly_df = _weekly_frame([202601, 202602, 202603, 202604, 202605, 202606, 202607])
        artifact = SimpleNamespace(dataframe=weekly_df, path=Path("/tmp/weekly.csv"), source="unit_test")
        engine = MagicMock()
        calendar = _calendar_for(weekly_df["week_id"])

        with patch.object(runner, "create_sqlalchemy_engine", return_value=engine):
            with patch.object(runner, "get_calendar", return_value=calendar):
                with patch.object(runner, "build_weekly_input_artifact", return_value=artifact) as build_artifact:
                    with patch.object(runner, "persist_run_output") as persist:
                        payload = runner.run_weekly_5y_direct_0529_reproduction(persist=False)

        persist.assert_not_called()
        build_artifact.assert_called_once()
        self.assertEqual(build_artifact.call_args.kwargs["schema_columns"], runner.SCHEMA_COLUMNS)
        self.assertEqual(build_artifact.call_args.kwargs["start_week"], runner.BACKTEST_START_WEEK)
        self.assertEqual(build_artifact.call_args.kwargs["end_week"], runner.BACKTEST_END_WEEK)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["scheme_id"], "weekly_5y_direct_0529")
        self.assertEqual(payload["data_source"], "framework_db_aligned")
        self.assertEqual(payload["row_count"], len(payload["rows"]))
        self.assertEqual(payload["runs"][0]["rows"], payload["rows"])
        self.assertGreater(payload["monthly_count"], 0)
        engine.dispose.assert_called_once()

    def test_run_persist_writes_full_output_rows(self) -> None:
        from backtests import weekly_5y_direct_0529_reproduction as runner

        weekly_df = _weekly_frame([202601, 202602, 202603, 202604, 202605, 202606, 202607])
        artifact = SimpleNamespace(dataframe=weekly_df, path=Path("/tmp/weekly.csv"), source="unit_test")
        engine = MagicMock()
        calendar = _calendar_for(weekly_df["week_id"])

        with patch.object(runner, "create_sqlalchemy_engine", return_value=engine):
            with patch.object(runner, "get_calendar", return_value=calendar):
                with patch.object(runner, "build_weekly_input_artifact", return_value=artifact):
                    with patch.object(runner, "persist_run_output", return_value=321) as persist:
                        payload = runner.run_weekly_5y_direct_0529_reproduction(persist=True)

        persist.assert_called_once()
        self.assertEqual(persist.call_args.kwargs["benchmark_id"], "model_muti_0529")
        output = persist.call_args.args[1]
        self.assertEqual(payload["run_id"], 321)
        self.assertEqual(len(output.rows), payload["row_count"])
        self.assertIn("label", output.rows[0])
        self.assertIn("source_row", output.rows[0])
        self.assertEqual(output.rows[0]["benchmark_id"], "model_muti_0529")


def _weekly_frame(week_ids: list[int]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "week_id": week_ids,
            "TB1YWI3C": [1.0 + index * 0.01 for index in range(len(week_ids))],
            "TB5YWI3C": [2.0 + index * 0.1 for index in range(len(week_ids))],
            "TB7YWI3C": [2.5 + index * 0.02 for index in range(len(week_ids))],
            "TB0YWI3C": [3.0 + index * 0.03 for index in range(len(week_ids))],
        }
    )


def _calendar_for(week_ids: pd.Series) -> SimpleNamespace:
    week_dates = {
        int(week_id): pd.Timestamp("2026-01-02") + pd.Timedelta(days=7 * index)
        for index, week_id in enumerate(week_ids)
    }

    def last_trading_day(week_id: int) -> str:
        return week_dates[int(week_id)].strftime("%Y-%m-%d")

    return SimpleNamespace(week_id_to_last_trading_day=last_trading_day)
