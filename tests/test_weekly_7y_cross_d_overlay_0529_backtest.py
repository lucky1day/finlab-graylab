"""weekly_7y_cross_d_overlay_0529 回测 runner 测试。"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd


class Weekly7YCrossDOverlay0529BacktestTests(unittest.TestCase):
    """周频 7Y Cross-D 历史回测 runner 测试。"""

    def test_config_declares_backtest_runner_and_range(self) -> None:
        from harness.config_loader import load_config_raw

        project_root = Path(__file__).resolve().parents[1]
        config = load_config_raw(project_root / "schemes" / "weekly_7y_cross_d_overlay_0529" / "config.yaml")

        self.assertEqual(config["scheme_id"], "weekly_7y_cross_d_overlay_0529")
        self.assertEqual(config["horizon"], 6)
        self.assertEqual(config["tenors"], ["7Y"])
        self.assertEqual(config["frequency"], "weekly")
        self.assertEqual(config["schedule"]["cron"], "30 11 * * 6")
        backtest = config.get("backtest")
        self.assertIsInstance(backtest, dict)
        self.assertEqual(backtest["runner"], "backtests.weekly_7y_cross_d_overlay_0529_reproduction")
        self.assertLess(backtest["start_week"], backtest["end_week"])

    def test_prediction_rows_use_target_date_and_db_calendar(self) -> None:
        from backtests import weekly_7y_cross_d_overlay_0529_reproduction as runner

        weekly_df = _weekly_frame([202601, 202602, 202603, 202604])
        calendar = _calendar_for(weekly_df["week_id"])

        def fake_overlay(frame: pd.DataFrame) -> pd.DataFrame:
            feature_week = int(frame["week_id"].iloc[-1])
            return pd.DataFrame(
                {
                    "week_id": [feature_week],
                    "cross_d_pred_label": [1 if feature_week % 2 == 0 else -1],
                    "cross_d_prob_up": [0.55 if feature_week % 2 == 0 else 0.45],
                    "cross_d_overlay": [feature_week == 202602],
                    "cross_d_signal_source": ["unit"],
                    "main_pred_label": [-1],
                    "main_prob_up": [0.45],
                    "d5_d_pred_label": [-1],
                    "d5_d_prob_up": [0.45],
                }
            )

        with patch.object(runner, "build_cross_d_overlay", side_effect=fake_overlay):
            rows = runner.build_backtest_rows(
                weekly_df,
                calendar=calendar,
                artifact_path=Path("/tmp/weekly_7y.csv"),
                artifact_source="unit_test",
            )

        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["target_tenor"], "7Y")
        self.assertEqual(rows[0]["horizon"], 6)
        self.assertEqual(rows[0]["predict_date"], "2026-01-02")
        self.assertEqual(rows[0]["predict_date"], rows[0]["feature_date"])
        self.assertEqual(rows[0]["feature_date"], "2026-01-02")
        self.assertEqual(rows[0]["target_date"], "2026-01-09")
        self.assertEqual(rows[0]["extra"]["feature_week_id"], 202601)
        self.assertEqual(rows[0]["extra"]["target_week_id"], 202602)
        self.assertEqual(rows[0]["extra"]["target_date"], "2026-01-09")
        self.assertEqual(rows[0]["extra"]["input_artifact_source"], "unit_test")
        self.assertEqual(rows[0]["label"], 1)

    def test_prediction_rows_choose_target_week_from_calendar_not_adjacent_input_row(self) -> None:
        from backtests import weekly_7y_cross_d_overlay_0529_reproduction as runner

        weekly_df = _weekly_frame([202620, 202621, 202622])
        calendar = _calendar_for(weekly_df["week_id"])
        calendar.next_trading_days = lambda day, count: {
            "2026-05-15": ["2026-05-22"],
            "2026-05-29": [],
        }.get(day, [])
        calendar.week_id_for_date = lambda day: {
            "2026-05-22": 202622,
        }.get(day)

        def fake_overlay(frame: pd.DataFrame) -> pd.DataFrame:
            feature_week = int(frame["week_id"].iloc[-1])
            return pd.DataFrame(
                {
                    "week_id": [feature_week],
                    "cross_d_pred_label": [1],
                    "cross_d_prob_up": [0.55],
                    "cross_d_overlay": [False],
                    "cross_d_signal_source": ["unit"],
                    "main_pred_label": [1],
                    "main_prob_up": [0.55],
                    "d5_d_pred_label": [1],
                    "d5_d_prob_up": [0.55],
                }
            )

        with patch.object(runner, "build_cross_d_overlay", side_effect=fake_overlay):
            rows = runner.build_backtest_rows(
                weekly_df,
                calendar=calendar,
                artifact_path=Path("/tmp/weekly_7y.csv"),
                artifact_source="unit_test",
            )

        self.assertEqual(rows[0]["extra"]["feature_week_id"], 202620)
        self.assertEqual(rows[0]["extra"]["target_week_id"], 202622)
        self.assertEqual(rows[0]["target_date"], "2026-05-22")

    def test_backtest_rows_start_from_predict_date_2025_01_01(self) -> None:
        from backtests import weekly_7y_cross_d_overlay_0529_reproduction as runner

        weekly_df = _weekly_frame([202452, 202501, 202502])
        week_dates = {
            202452: pd.Timestamp("2024-12-27"),
            202501: pd.Timestamp("2025-01-03"),
            202502: pd.Timestamp("2025-01-10"),
        }
        calendar = SimpleNamespace(
            week_id_to_last_trading_day=lambda week_id: week_dates[int(week_id)].strftime("%Y-%m-%d"),
            next_trading_days=lambda day, count: [
                value.strftime("%Y-%m-%d")
                for value in sorted(week_dates.values())
                if value > pd.Timestamp(day)
            ][:count],
            week_id_for_date=lambda day: {
                value.strftime("%Y-%m-%d"): week_id
                for week_id, value in week_dates.items()
            }.get(day),
        )

        def fake_overlay(frame: pd.DataFrame) -> pd.DataFrame:
            feature_week = int(frame["week_id"].iloc[-1])
            return pd.DataFrame(
                {
                    "week_id": [feature_week],
                    "cross_d_pred_label": [1],
                    "cross_d_prob_up": [0.55],
                    "cross_d_overlay": [False],
                    "cross_d_signal_source": ["unit"],
                    "main_pred_label": [1],
                    "main_prob_up": [0.55],
                    "d5_d_pred_label": [1],
                    "d5_d_prob_up": [0.55],
                }
            )

        with patch.object(runner, "build_cross_d_overlay", side_effect=fake_overlay):
            rows = runner.build_backtest_rows(
                weekly_df,
                calendar=calendar,
                artifact_path=Path("/tmp/weekly_7y.csv"),
                artifact_source="unit_test",
            )

        self.assertEqual([row["predict_date"] for row in rows], ["2025-01-03"])
        self.assertTrue(all(row["predict_date"] >= runner.BACKTEST_PREDICT_START_DATE for row in rows))

    def test_backtest_rows_stop_before_live_target_month(self) -> None:
        from backtests import weekly_7y_cross_d_overlay_0529_reproduction as runner

        weekly_df = _weekly_frame([202619, 202620, 202621, 202622, 202623])
        calendar = _calendar_for(weekly_df["week_id"])
        week_dates = {
            202619: pd.Timestamp("2026-05-15"),
            202620: pd.Timestamp("2026-05-22"),
            202621: pd.Timestamp("2026-05-29"),
            202622: pd.Timestamp("2026-06-05"),
            202623: pd.Timestamp("2026-06-12"),
        }
        calendar.week_id_to_last_trading_day = lambda week_id: week_dates[int(week_id)].strftime("%Y-%m-%d")
        calendar.next_trading_days = lambda day, count: [
            value.strftime("%Y-%m-%d")
            for value in sorted(week_dates.values())
            if value > pd.Timestamp(day)
        ][:count]
        calendar.week_id_for_date = lambda day: {
            value.strftime("%Y-%m-%d"): week_id
            for week_id, value in week_dates.items()
        }.get(day)

        def fake_overlay(frame: pd.DataFrame) -> pd.DataFrame:
            feature_week = int(frame["week_id"].iloc[-1])
            return pd.DataFrame(
                {
                    "week_id": [feature_week],
                    "cross_d_pred_label": [-1],
                    "cross_d_prob_up": [0.32],
                    "cross_d_overlay": [False],
                    "cross_d_signal_source": ["unit"],
                    "main_pred_label": [-1],
                    "main_prob_up": [0.32],
                    "d5_d_pred_label": [-1],
                    "d5_d_prob_up": [0.32],
                }
            )

        with patch.object(runner, "build_cross_d_overlay", side_effect=fake_overlay):
            rows = runner.build_backtest_rows(
                weekly_df,
                calendar=calendar,
                artifact_path=Path("/tmp/weekly_7y.csv"),
                artifact_source="unit_test",
            )

        self.assertEqual([row["target_date"] for row in rows], ["2026-05-22", "2026-05-29"])
        self.assertTrue(all(row["target_date"] < runner.LIVE_TARGET_START_DATE for row in rows))

    def test_run_no_persist_returns_sop_payload_without_writes(self) -> None:
        from backtests import weekly_7y_cross_d_overlay_0529_reproduction as runner

        weekly_df = _weekly_frame([202601, 202602, 202603, 202604, 202605, 202606, 202607, 202608])
        artifact = SimpleNamespace(dataframe=weekly_df, path=Path("/tmp/weekly_7y.csv"), source="unit_test")
        engine = MagicMock()
        calendar = _calendar_for(weekly_df["week_id"])

        with patch.object(runner, "create_sqlalchemy_engine", return_value=engine):
            with patch.object(runner, "get_calendar", return_value=calendar):
                with patch.object(runner, "build_weekly_input_artifact", return_value=artifact) as build_artifact:
                    with patch.object(runner, "persist_run_output") as persist:
                        payload = runner.run_weekly_7y_cross_d_overlay_0529_reproduction(persist=False)

        persist.assert_not_called()
        build_artifact.assert_called_once()
        self.assertEqual(build_artifact.call_args.kwargs["schema_columns"], runner.SCHEMA_COLUMNS)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["scheme_id"], "weekly_7y_cross_d_overlay_0529")
        self.assertEqual(payload["data_source"], "framework_db_aligned")
        self.assertEqual(payload["row_count"], len(payload["rows"]))
        self.assertEqual(payload["runs"][0]["rows"], payload["rows"])
        self.assertGreater(payload["monthly_count"], 0)
        engine.dispose.assert_called_once()

    def test_run_persist_writes_full_output_rows(self) -> None:
        from backtests import weekly_7y_cross_d_overlay_0529_reproduction as runner

        weekly_df = _weekly_frame([202601, 202602, 202603, 202604, 202605, 202606, 202607, 202608])
        artifact = SimpleNamespace(dataframe=weekly_df, path=Path("/tmp/weekly_7y.csv"), source="unit_test")
        engine = MagicMock()
        calendar = _calendar_for(weekly_df["week_id"])

        with patch.object(runner, "create_sqlalchemy_engine", return_value=engine):
            with patch.object(runner, "get_calendar", return_value=calendar):
                with patch.object(runner, "build_weekly_input_artifact", return_value=artifact):
                    with patch.object(runner, "persist_run_output", return_value=777) as persist:
                        payload = runner.run_weekly_7y_cross_d_overlay_0529_reproduction(persist=True)

        persist.assert_called_once()
        output = persist.call_args.args[1]
        self.assertEqual(persist.call_args.kwargs["benchmark_id"], runner.BENCHMARK_ID)
        self.assertEqual(payload["run_id"], 777)
        self.assertEqual(len(output.rows), payload["row_count"])
        self.assertEqual(output.rows[0]["benchmark_id"], runner.BENCHMARK_ID)
        self.assertIn("source_row", output.rows[0])


def _weekly_frame(week_ids: list[int]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "week_id": week_ids,
            "TB1YWI3C": [1.80 + index * 0.04 for index in range(len(week_ids))],
            "TB3YWI3C": [1.90 + index * 0.03 for index in range(len(week_ids))],
            "TB5YWI3C": [2.00 + index * 0.05 for index in range(len(week_ids))],
            "TB7YWI3C": [1.70 + index * 0.04 for index in range(len(week_ids))],
            "TB0YWI3C": [2.30 + index * 0.02 for index in range(len(week_ids))],
        }
    )


def _calendar_for(week_ids: pd.Series) -> SimpleNamespace:
    week_dates = {
        int(week_id): pd.Timestamp("2026-01-02") + pd.Timedelta(days=7 * index)
        for index, week_id in enumerate(week_ids)
    }
    week_dates[202620] = pd.Timestamp("2026-05-15")
    week_dates[202621] = pd.Timestamp("2026-05-21")
    week_dates[202622] = pd.Timestamp("2026-05-22")

    def last_trading_day(week_id: int) -> str:
        return week_dates[int(week_id)].strftime("%Y-%m-%d")

    def next_trading_days(day: str, count: int) -> list[str]:
        current = pd.Timestamp(day)
        days = [
            value.strftime("%Y-%m-%d")
            for value in sorted(week_dates.values())
            if value > current
        ]
        return days[:count]

    def week_id_for_date(day: str) -> int | None:
        target = pd.Timestamp(day)
        for week_id, value in week_dates.items():
            if value == target:
                return int(week_id)
        return None

    return SimpleNamespace(
        week_id_to_last_trading_day=last_trading_day,
        next_trading_days=next_trading_days,
        week_id_for_date=week_id_for_date,
    )


if __name__ == "__main__":
    unittest.main()
