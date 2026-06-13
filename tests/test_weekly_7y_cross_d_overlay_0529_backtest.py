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
        self.assertTrue(backtest["benchmark_required"])
        self.assertLess(backtest["start_week"], backtest["end_week"])

    def test_prediction_rows_use_target_date_and_db_calendar(self) -> None:
        from backtests import weekly_7y_cross_d_overlay_0529_reproduction as runner

        weekly_df = _weekly_frame([202601, 202602, 202603, 202604])
        calendar = _calendar_for(weekly_df["week_id"])

        def fake_overlay(frame: pd.DataFrame) -> pd.DataFrame:
            weeks = frame["week_id"].astype(int).tolist()
            return pd.DataFrame(
                {
                    "week_id": weeks,
                    "cross_d_pred_label": [1 if week % 2 == 0 else -1 for week in weeks],
                    "cross_d_prob_up": [0.55 if week % 2 == 0 else 0.45 for week in weeks],
                    "cross_d_overlay": [week == 202602 for week in weeks],
                    "cross_d_signal_source": ["unit" for _ in weeks],
                    "main_pred_label": [-1 for _ in weeks],
                    "main_prob_up": [0.45 for _ in weeks],
                    "d5_d_pred_label": [-1 for _ in weeks],
                    "d5_d_prob_up": [0.45 for _ in weeks],
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
            weeks = frame["week_id"].astype(int).tolist()
            return pd.DataFrame(
                {
                    "week_id": weeks,
                    "cross_d_pred_label": [1 for _ in weeks],
                    "cross_d_prob_up": [0.55 for _ in weeks],
                    "cross_d_overlay": [False for _ in weeks],
                    "cross_d_signal_source": ["unit" for _ in weeks],
                    "main_pred_label": [1 for _ in weeks],
                    "main_prob_up": [0.55 for _ in weeks],
                    "d5_d_pred_label": [1 for _ in weeks],
                    "d5_d_prob_up": [0.55 for _ in weeks],
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
            weeks = frame["week_id"].astype(int).tolist()
            return pd.DataFrame(
                {
                    "week_id": weeks,
                    "cross_d_pred_label": [1 for _ in weeks],
                    "cross_d_prob_up": [0.55 for _ in weeks],
                    "cross_d_overlay": [False for _ in weeks],
                    "cross_d_signal_source": ["unit" for _ in weeks],
                    "main_pred_label": [1 for _ in weeks],
                    "main_prob_up": [0.55 for _ in weeks],
                    "d5_d_pred_label": [1 for _ in weeks],
                    "d5_d_prob_up": [0.55 for _ in weeks],
                }
            )

        with patch.object(runner, "build_cross_d_overlay", side_effect=fake_overlay) as build_overlay:
            rows = runner.build_backtest_rows(
                weekly_df,
                calendar=calendar,
                artifact_path=Path("/tmp/weekly_7y.csv"),
                artifact_source="unit_test",
            )

        self.assertEqual([row["predict_date"] for row in rows], ["2025-01-03"])
        self.assertTrue(all(row["predict_date"] >= runner.BACKTEST_PREDICT_START_DATE for row in rows))
        self.assertEqual(build_overlay.call_count, 1)
        passed_history = build_overlay.call_args.args[0]
        self.assertEqual(passed_history["week_id"].astype(int).tolist(), [202452, 202501, 202502])

    def test_backtest_uses_single_original_batch_overlay_not_pit_slices(self) -> None:
        from backtests import weekly_7y_cross_d_overlay_0529_reproduction as runner

        weekly_df = _weekly_frame([202452, 202501, 202502, 202503])
        week_dates = {
            202452: pd.Timestamp("2024-12-27"),
            202501: pd.Timestamp("2025-01-03"),
            202502: pd.Timestamp("2025-01-10"),
            202503: pd.Timestamp("2025-01-17"),
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
            weeks = frame["week_id"].astype(int).tolist()
            return pd.DataFrame(
                {
                    "week_id": weeks,
                    "cross_d_pred_label": [1 for _ in weeks],
                    "cross_d_prob_up": [0.55 for _ in weeks],
                    "cross_d_overlay": [False for _ in weeks],
                    "cross_d_signal_source": ["unit" for _ in weeks],
                    "main_pred_label": [1 for _ in weeks],
                    "main_prob_up": [0.55 for _ in weeks],
                    "d5_d_pred_label": [1 for _ in weeks],
                    "d5_d_prob_up": [0.55 for _ in weeks],
                }
            )

        with patch.object(runner, "build_cross_d_overlay", side_effect=fake_overlay) as build_overlay:
            rows = runner.build_backtest_rows(
                weekly_df,
                calendar=calendar,
                artifact_path=Path("/tmp/weekly_7y.csv"),
                artifact_source="unit_test",
            )

        self.assertEqual(build_overlay.call_count, 1)
        passed_history = build_overlay.call_args.args[0]
        self.assertEqual(passed_history["week_id"].astype(int).tolist(), [202452, 202501, 202502, 202503])
        self.assertEqual([row["extra"]["feature_week_id"] for row in rows], [202501, 202502])

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
            weeks = frame["week_id"].astype(int).tolist()
            return pd.DataFrame(
                {
                    "week_id": weeks,
                    "cross_d_pred_label": [-1 for _ in weeks],
                    "cross_d_prob_up": [0.32 for _ in weeks],
                    "cross_d_overlay": [False for _ in weeks],
                    "cross_d_signal_source": ["unit" for _ in weeks],
                    "main_pred_label": [-1 for _ in weeks],
                    "main_prob_up": [0.32 for _ in weeks],
                    "d5_d_pred_label": [-1 for _ in weeks],
                    "d5_d_prob_up": [0.32 for _ in weeks],
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
                    with patch.object(
                        runner,
                        "validate_original_benchmark_rows",
                        return_value={"benchmark_rows": 0, "matched_rows": 0},
                    ):
                        with patch.object(runner, "persist_run_output") as persist:
                            payload = runner.run_weekly_7y_cross_d_overlay_0529_reproduction(persist=False)

        persist.assert_not_called()
        self.assertEqual(build_artifact.call_count, 1)
        seed_kwargs = build_artifact.call_args_list[0].kwargs
        self.assertEqual(seed_kwargs["schema_columns"], runner.SCHEMA_COLUMNS)
        self.assertEqual(seed_kwargs["start_week"], runner.BACKTEST_START_WEEK)
        self.assertEqual(seed_kwargs["end_week"], runner.BACKTEST_END_WEEK)
        self.assertEqual(seed_kwargs["as_of_date"], runner.BACKTEST_MAX_AS_OF_DATE)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["scheme_id"], "weekly_7y_cross_d_overlay_0529")
        self.assertEqual(payload["data_source"], "framework_db_aligned")
        self.assertEqual(payload["row_count"], len(payload["rows"]))
        self.assertEqual(payload["runs"][0]["rows"], payload["rows"])
        self.assertGreater(payload["monthly_count"], 0)
        self.assertEqual(payload["summary"]["backtest_mode"], "original_batch_reproduction")
        self.assertFalse(payload["summary"]["backtest_point_in_time"])
        self.assertTrue(payload["summary"]["historical_backtest_exception"])
        self.assertEqual(payload["summary"]["point_in_time_artifact_count"], 0)
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
                    with patch.object(
                        runner,
                        "validate_original_benchmark_rows",
                        return_value={"benchmark_rows": 0, "matched_rows": 0},
                    ):
                        with patch.object(runner, "persist_run_output", return_value=777) as persist:
                            payload = runner.run_weekly_7y_cross_d_overlay_0529_reproduction(persist=True)

        persist.assert_called_once()
        output = persist.call_args.args[1]
        self.assertEqual(persist.call_args.kwargs["benchmark_id"], runner.BENCHMARK_ID)
        self.assertEqual(payload["run_id"], 777)
        self.assertEqual(len(output.rows), payload["row_count"])
        self.assertEqual(output.rows[0]["benchmark_id"], runner.BENCHMARK_ID)
        self.assertIn("source_row", output.rows[0])

    def test_original_benchmark_validation_rejects_direction_mismatch(self) -> None:
        from backtests import weekly_7y_cross_d_overlay_0529_reproduction as runner

        rows = [
            {
                "predict_date": "2025-07-04",
                "target_date": "2025-07-11",
                "target_tenor": "7Y",
                "predicted_direction": 1,
                "confidence": 0.62,
                "label": 1,
                "extra": {"feature_week_id": 202527},
            }
        ]
        benchmark = pd.DataFrame(
            {
                "predict_date": ["2025-07-04"],
                "tenor": ["7Y"],
                "direction": [-1],
                "confidence": [0.62],
                "feature_week_id": [202527],
                "target_date": ["2025-07-11"],
                "label": [1],
                "is_correct": [False],
            }
        )

        with self.assertRaisesRegex(AssertionError, "direction mismatch"):
            runner.validate_original_benchmark_rows(rows, benchmark)

    def test_original_benchmark_validation_can_match_date_key_without_week_id(self) -> None:
        from backtests import weekly_7y_cross_d_overlay_0529_reproduction as runner

        rows = [
            {
                "predict_date": "2025-07-04",
                "target_date": "2025-07-11",
                "target_tenor": "7Y",
                "predicted_direction": -1,
                "confidence": 0.62,
                "label": 1,
                "extra": {"feature_week_id": 202527},
            }
        ]
        benchmark = pd.DataFrame(
            {
                "predict_date": ["2025-07-04"],
                "tenor": ["7Y"],
                "predicted_direction": [-1],
                "confidence": [0.62],
                "framework_target_date": ["2025-07-11"],
                "label": [1],
                "is_correct": [False],
            }
        )

        result = runner.validate_original_benchmark_rows(rows, benchmark)

        self.assertEqual(result, {"benchmark_rows": 1, "matched_rows": 1})


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
