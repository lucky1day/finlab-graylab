"""weekly_10y_d_overlay_0529 回测 runner 测试。"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd


class Weekly10YDOverlay0529BacktestTests(unittest.TestCase):
    """周频 10Y D-overlay 历史回测 runner 测试。"""

    def test_config_declares_backtest_runner_and_benchmark_required(self) -> None:
        from harness.config_loader import load_config_raw

        project_root = Path(__file__).resolve().parents[1]
        config = load_config_raw(project_root / "schemes" / "weekly_10y_d_overlay_0529" / "config.yaml")

        self.assertEqual(config["scheme_id"], "weekly_10y_d_overlay_0529")
        self.assertEqual(config["horizon"], 6)
        self.assertEqual(config["tenors"], ["10Y"])
        self.assertEqual(config["frequency"], "weekly")
        self.assertEqual(config["schedule"]["cron"], "30 11 * * 6")
        backtest = config.get("backtest")
        self.assertIsInstance(backtest, dict)
        self.assertEqual(backtest["runner"], "backtests.weekly_10y_d_overlay_0529_reproduction")
        self.assertTrue(backtest["benchmark_required"])
        self.assertLess(backtest["start_week"], backtest["end_week"])

    def test_prediction_rows_use_target_date_and_db_calendar(self) -> None:
        from backtests import weekly_10y_d_overlay_0529_reproduction as runner

        weekly_df = _weekly_frame([202601, 202602, 202603, 202604])
        calendar = _calendar_for(weekly_df["week_id"])

        def fake_overlay(frame: pd.DataFrame) -> pd.DataFrame:
            weeks = [int(value) for value in frame["week_id"].tolist()]
            return pd.DataFrame(
                {
                    "week_id": weeks,
                    "d_pred_label": [1 if week % 2 == 0 else -1 for week in weeks],
                    "d_prob_up": [0.56 if week % 2 == 0 else 0.28 for week in weeks],
                    "d_model2_overlay": [week == 202602 for week in weeks],
                    "d_signal_source": ["unit" for _ in weeks],
                    "score_pred_label": [-1 for _ in weeks],
                    "score_prob_up": [0.28 for _ in weeks],
                    "model2_prob_up": [0.56 for _ in weeks],
                    "d_model2_pred_label": [1 for _ in weeks],
                }
            )

        with patch.object(runner, "build_d_overlay", side_effect=fake_overlay):
            rows = runner.build_backtest_rows(
                weekly_df,
                calendar=calendar,
                artifact_path=Path("/tmp/weekly_10y.csv"),
                artifact_source="unit_test",
            )

        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["target_tenor"], "10Y")
        self.assertEqual(rows[0]["horizon"], 6)
        self.assertEqual(rows[0]["predict_date"], "2026-01-03")
        self.assertEqual(rows[0]["feature_date"], "2026-01-02")
        self.assertEqual(rows[0]["target_date"], "2026-01-09")
        self.assertEqual(rows[0]["extra"]["feature_week_id"], 202601)
        self.assertEqual(rows[0]["extra"]["target_week_id"], 202602)
        self.assertEqual(rows[0]["extra"]["target_date"], "2026-01-09")
        self.assertEqual(rows[0]["extra"]["input_artifact_source"], "unit_test")
        self.assertEqual(rows[0]["label"], 1)

    def test_prediction_rows_choose_target_week_from_calendar_not_adjacent_input_row(self) -> None:
        from backtests import weekly_10y_d_overlay_0529_reproduction as runner

        weekly_df = _weekly_frame([202620, 202621, 202622])
        calendar = _calendar_for(weekly_df["week_id"])
        calendar.next_trading_days = lambda day, count: {
            "2026-05-15": ["2026-05-22"],
            "2026-05-29": [],
        }.get(day, [])
        def week_id_for_date(day: str) -> int | None:
            target = pd.Timestamp(day)
            for week_id, value in {202620: "2026-05-15", 202621: "2026-05-21", 202622: "2026-05-22"}.items():
                last = pd.Timestamp(value)
                if last - pd.Timedelta(days=4) <= target <= last:
                    return week_id
            return None

        calendar.week_id_for_date = week_id_for_date

        def fake_overlay(frame: pd.DataFrame) -> pd.DataFrame:
            weeks = [int(value) for value in frame["week_id"].tolist()]
            return pd.DataFrame(
                {
                    "week_id": weeks,
                    "d_pred_label": [1 for _ in weeks],
                    "d_prob_up": [0.56 for _ in weeks],
                    "d_model2_overlay": [False for _ in weeks],
                    "d_signal_source": ["unit" for _ in weeks],
                    "score_pred_label": [1 for _ in weeks],
                    "score_prob_up": [0.56 for _ in weeks],
                    "model2_prob_up": [0.56 for _ in weeks],
                    "d_model2_pred_label": [1 for _ in weeks],
                }
            )

        with patch.object(runner, "build_d_overlay", side_effect=fake_overlay):
            rows = runner.build_backtest_rows(
                weekly_df,
                calendar=calendar,
                artifact_path=Path("/tmp/weekly_10y.csv"),
                artifact_source="unit_test",
            )

        self.assertEqual(rows[0]["extra"]["feature_week_id"], 202620)
        self.assertEqual(rows[0]["extra"]["target_week_id"], 202622)
        self.assertEqual(rows[0]["target_date"], "2026-05-22")

    def test_backtest_rows_stop_before_live_target_month(self) -> None:
        from backtests import weekly_10y_d_overlay_0529_reproduction as runner

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

        def week_id_for_date(day: str) -> int | None:
            target = pd.Timestamp(day)
            for week_id, value in week_dates.items():
                if value - pd.Timedelta(days=4) <= target <= value:
                    return int(week_id)
            return None

        calendar.week_id_for_date = week_id_for_date

        def fake_overlay(frame: pd.DataFrame) -> pd.DataFrame:
            weeks = [int(value) for value in frame["week_id"].tolist()]
            return pd.DataFrame(
                {
                    "week_id": weeks,
                    "d_pred_label": [-1 for _ in weeks],
                    "d_prob_up": [0.32 for _ in weeks],
                    "d_model2_overlay": [False for _ in weeks],
                    "d_signal_source": ["unit" for _ in weeks],
                    "score_pred_label": [-1 for _ in weeks],
                    "score_prob_up": [0.32 for _ in weeks],
                    "model2_prob_up": [0.32 for _ in weeks],
                    "d_model2_pred_label": [-1 for _ in weeks],
                }
            )

        with patch.object(runner, "build_d_overlay", side_effect=fake_overlay):
            rows = runner.build_backtest_rows(
                weekly_df,
                calendar=calendar,
                artifact_path=Path("/tmp/weekly_10y.csv"),
                artifact_source="unit_test",
            )

        self.assertEqual([row["target_date"] for row in rows], ["2026-05-22", "2026-05-29"])
        self.assertTrue(all(row["target_date"] < runner.LIVE_TARGET_START_DATE for row in rows))

    def test_run_no_persist_returns_sop_payload_without_writes(self) -> None:
        from backtests import weekly_10y_d_overlay_0529_reproduction as runner

        weekly_df = _weekly_frame([202601, 202602, 202603, 202604, 202605, 202606, 202607, 202608])
        artifact = SimpleNamespace(dataframe=weekly_df, path=Path("/tmp/weekly_10y.csv"), source="unit_test")
        engine = MagicMock()
        calendar = _calendar_for(weekly_df["week_id"])

        def fake_overlay(frame: pd.DataFrame) -> pd.DataFrame:
            weeks = [int(value) for value in frame["week_id"].tolist()]
            return pd.DataFrame(
                {
                    "week_id": weeks,
                    "d_pred_label": [1 for _ in weeks],
                    "d_prob_up": [0.56 for _ in weeks],
                    "d_model2_overlay": [False for _ in weeks],
                    "d_signal_source": ["unit" for _ in weeks],
                    "score_pred_label": [1 for _ in weeks],
                    "score_prob_up": [0.56 for _ in weeks],
                    "model2_prob_up": [0.56 for _ in weeks],
                    "d_model2_pred_label": [1 for _ in weeks],
                }
            )

        with patch.object(runner, "create_sqlalchemy_engine", return_value=engine):
            with patch.object(runner, "get_calendar", return_value=calendar):
                with patch.object(runner, "build_weekly_input_artifact", return_value=artifact) as build_artifact:
                    with patch.object(runner, "build_d_overlay", side_effect=fake_overlay):
                        with patch.object(runner, "persist_run_output") as persist:
                            payload = runner.run_weekly_10y_d_overlay_0529_reproduction(persist=False)

        persist.assert_not_called()
        build_artifact.assert_called_once()
        self.assertIsNone(build_artifact.call_args.kwargs["schema_columns"])
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["scheme_id"], "weekly_10y_d_overlay_0529")
        self.assertEqual(payload["data_source"], "framework_db_aligned")
        self.assertEqual(payload["row_count"], len(payload["rows"]))
        self.assertEqual(payload["runs"][0]["rows"], payload["rows"])
        self.assertGreater(payload["monthly_count"], 0)
        engine.dispose.assert_called_once()

    def test_run_persist_writes_full_output_rows(self) -> None:
        from backtests import weekly_10y_d_overlay_0529_reproduction as runner

        weekly_df = _weekly_frame([202601, 202602, 202603, 202604, 202605, 202606, 202607, 202608])
        artifact = SimpleNamespace(dataframe=weekly_df, path=Path("/tmp/weekly_10y.csv"), source="unit_test")
        engine = MagicMock()
        calendar = _calendar_for(weekly_df["week_id"])

        def fake_overlay(frame: pd.DataFrame) -> pd.DataFrame:
            weeks = [int(value) for value in frame["week_id"].tolist()]
            return pd.DataFrame(
                {
                    "week_id": weeks,
                    "d_pred_label": [1 for _ in weeks],
                    "d_prob_up": [0.56 for _ in weeks],
                    "d_model2_overlay": [False for _ in weeks],
                    "d_signal_source": ["unit" for _ in weeks],
                    "score_pred_label": [1 for _ in weeks],
                    "score_prob_up": [0.56 for _ in weeks],
                    "model2_prob_up": [0.56 for _ in weeks],
                    "d_model2_pred_label": [1 for _ in weeks],
                }
            )

        with patch.object(runner, "create_sqlalchemy_engine", return_value=engine):
            with patch.object(runner, "get_calendar", return_value=calendar):
                with patch.object(runner, "build_weekly_input_artifact", return_value=artifact):
                    with patch.object(runner, "build_d_overlay", side_effect=fake_overlay):
                        with patch.object(runner, "persist_run_output", return_value=777) as persist:
                            payload = runner.run_weekly_10y_d_overlay_0529_reproduction(persist=True)

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
            "TB0YWI3C": [2.30 + index * 0.02 for index in range(len(week_ids))],
            "TB1YWI3C": [1.60 + index * 0.01 for index in range(len(week_ids))],
            "TB5YWI3C": [2.00 + index * 0.01 for index in range(len(week_ids))],
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
            if value - pd.Timedelta(days=4) <= target <= value:
                return int(week_id)
        return None

    return SimpleNamespace(
        week_id_to_last_trading_day=last_trading_day,
        next_trading_days=next_trading_days,
        week_id_for_date=week_id_for_date,
    )


if __name__ == "__main__":
    unittest.main()
