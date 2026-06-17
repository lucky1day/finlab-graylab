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
        self.assertTrue(backtest["benchmark_required"])
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
            return pd.DataFrame(
                {
                    "week_id": weeks,
                    "final_pred_label": [1 if week % 2 == 0 else -1 for week in weeks],
                    "final_prob_up": [0.55 if week % 2 == 0 else 0.45 for week in weeks],
                    "rule_vote": [1.0 if week % 2 == 0 else -1.0 for week in weeks],
                    "source_spec": ["demo_rule" for _ in weeks],
                    "score_spec": ["demo_rule:1.0000" for _ in weeks],
                }
            )

        with patch.object(runner, "build_rule_vote", side_effect=fake_vote):
            rows = runner.build_backtest_rows(
                weekly_df,
                calendar=calendar,
                artifact_path=Path("/tmp/weekly.csv"),
                artifact_source="unit_test",
            )

        self.assertEqual(vote_calls, [[202601, 202602, 202603, 202604]])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["predict_date"], "2026-01-02")
        self.assertEqual(rows[0]["predict_date"], rows[0]["feature_date"])
        self.assertEqual(rows[0]["target_date"], "2026-01-09")
        self.assertEqual(rows[0]["extra"]["feature_week_id"], 202601)
        self.assertEqual(rows[0]["extra"]["target_week_id"], 202602)
        self.assertEqual(rows[0]["extra"]["input_artifact_source"], "unit_test")
        self.assertEqual(rows[0]["label"], 1)

        compact = runner.compact_prediction_rows(rows)
        self.assertEqual(
            compact[0],
            {
                "feature_date": "2026-01-02",
                "target_date": "2026-01-09",
                "target_tenor": "5Y",
                "horizon": 6,
                "direction": -1,
                "confidence": 0.45,
                "label": 1,
                "is_correct": False,
                "feature_week_id": 202601,
                "target_week_id": 202602,
            },
        )

    def test_backtest_rows_start_from_predict_date_2025_01_01(self) -> None:
        from backtests import weekly_5y_direct_0529_reproduction as runner

        weekly_df = _weekly_frame([202452, 202501, 202502])
        week_dates = {
            202452: pd.Timestamp("2024-12-27"),
            202501: pd.Timestamp("2025-01-03"),
            202502: pd.Timestamp("2025-01-10"),
        }
        calendar = SimpleNamespace(
            week_id_to_last_trading_day=lambda week_id: {
                202452: "2024-12-27",
                202501: "2025-01-03",
                202502: "2025-01-10",
            }[int(week_id)],
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

        def fake_vote(frame: pd.DataFrame) -> pd.DataFrame:
            weeks = frame["week_id"].astype(int).tolist()
            return pd.DataFrame(
                {
                    "week_id": weeks,
                    "final_pred_label": [1 for _ in weeks],
                    "final_prob_up": [0.55 for _ in weeks],
                    "rule_vote": [1.0 for _ in weeks],
                    "source_spec": ["unit" for _ in weeks],
                    "score_spec": ["unit:1.0000" for _ in weeks],
                }
            )

        with patch.object(runner, "build_rule_vote", side_effect=fake_vote) as build_vote:
            rows = runner.build_backtest_rows(
                weekly_df,
                calendar=calendar,
                artifact_path=Path("/tmp/weekly.csv"),
                artifact_source="unit_test",
            )

        self.assertEqual([row["predict_date"] for row in rows], ["2025-01-03"])
        self.assertTrue(all(row["predict_date"] >= runner.BACKTEST_PREDICT_START_DATE for row in rows))
        self.assertEqual(build_vote.call_count, 1)
        passed_history = build_vote.call_args.args[0]
        self.assertEqual(passed_history["week_id"].astype(int).tolist(), [202452, 202501, 202502])

    def test_backtest_uses_single_original_batch_vote_not_pit_slices(self) -> None:
        from backtests import weekly_5y_direct_0529_reproduction as runner

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

        def fake_vote(frame: pd.DataFrame) -> pd.DataFrame:
            weeks = frame["week_id"].astype(int).tolist()
            return pd.DataFrame(
                {
                    "week_id": weeks,
                    "final_pred_label": [1 for _ in weeks],
                    "final_prob_up": [0.55 for _ in weeks],
                    "rule_vote": [1.0 for _ in weeks],
                    "source_spec": ["unit" for _ in weeks],
                    "score_spec": ["unit:1.0000" for _ in weeks],
                }
            )

        with patch.object(runner, "build_rule_vote", side_effect=fake_vote) as build_vote:
            rows = runner.build_backtest_rows(
                weekly_df,
                calendar=calendar,
                artifact_path=Path("/tmp/weekly.csv"),
                artifact_source="unit_test",
            )

        self.assertEqual(build_vote.call_count, 1)
        passed_history = build_vote.call_args.args[0]
        self.assertEqual(passed_history["week_id"].astype(int).tolist(), [202452, 202501, 202502, 202503])
        self.assertEqual([row["extra"]["feature_week_id"] for row in rows], [202501, 202502])

    def test_backtest_rows_stop_before_live_target_month(self) -> None:
        from backtests import weekly_5y_direct_0529_reproduction as runner

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

        def fake_vote(frame: pd.DataFrame) -> pd.DataFrame:
            weeks = frame["week_id"].astype(int).tolist()
            return pd.DataFrame(
                {
                    "week_id": weeks,
                    "final_pred_label": [-1 for _ in weeks],
                    "final_prob_up": [0.32 for _ in weeks],
                    "rule_vote": [-1.0 for _ in weeks],
                    "source_spec": ["unit" for _ in weeks],
                    "score_spec": ["unit:-1.0000" for _ in weeks],
                }
            )

        with patch.object(runner, "build_rule_vote", side_effect=fake_vote):
            rows = runner.build_backtest_rows(
                weekly_df,
                calendar=calendar,
                artifact_path=Path("/tmp/weekly.csv"),
                artifact_source="unit_test",
            )

        self.assertEqual([row["target_date"] for row in rows], ["2026-05-22", "2026-05-29"])
        self.assertTrue(all(row["target_date"] < runner.LIVE_TARGET_START_DATE for row in rows))

    def test_run_no_persist_returns_backtest_gate_and_sop_payload(self) -> None:
        from backtests import weekly_5y_direct_0529_reproduction as runner

        weekly_df = _weekly_frame([202601, 202602, 202603, 202604, 202605, 202606, 202607])
        artifact = SimpleNamespace(dataframe=weekly_df, path=Path("/tmp/weekly.csv"), source="unit_test")
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
                            payload = runner.run_weekly_5y_direct_0529_reproduction(persist=False)

        persist.assert_not_called()
        self.assertEqual(build_artifact.call_count, 1)
        seed_kwargs = build_artifact.call_args_list[0].kwargs
        self.assertEqual(seed_kwargs["schema_columns"], runner.SCHEMA_COLUMNS)
        self.assertEqual(seed_kwargs["start_week"], runner.BACKTEST_START_WEEK)
        self.assertEqual(seed_kwargs["end_week"], runner.BACKTEST_END_WEEK)
        self.assertEqual(seed_kwargs["as_of_date"], runner.BACKTEST_MAX_AS_OF_DATE)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["scheme_id"], "weekly_5y_direct_0529")
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
        from backtests import weekly_5y_direct_0529_reproduction as runner

        weekly_df = _weekly_frame([202601, 202602, 202603, 202604, 202605, 202606, 202607])
        artifact = SimpleNamespace(dataframe=weekly_df, path=Path("/tmp/weekly.csv"), source="unit_test")
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

    def test_original_benchmark_validation_rejects_direction_mismatch(self) -> None:
        from backtests import weekly_5y_direct_0529_reproduction as runner

        rows = [
            {
                "predict_date": "2025-07-04",
                "target_date": "2025-07-11",
                "target_tenor": "5Y",
                "predicted_direction": 1,
                "confidence": 0.5166666666666667,
                "label": 1,
                "extra": {"feature_week_id": 202527},
            }
        ]
        benchmark = pd.DataFrame(
            {
                "predict_date": ["2025-07-04"],
                "tenor": ["5Y"],
                "direction": [-1],
                "confidence": [0.5166666666666667],
                "feature_week_id": [202527],
                "target_date": ["2025-07-11"],
                "label": [1],
                "is_correct": [False],
            }
        )

        with self.assertRaisesRegex(AssertionError, "direction mismatch"):
            runner.validate_original_benchmark_rows(rows, benchmark)

    def test_original_benchmark_validation_can_match_date_key_without_week_id(self) -> None:
        from backtests import weekly_5y_direct_0529_reproduction as runner

        rows = [
            {
                "predict_date": "2025-07-04",
                "target_date": "2025-07-11",
                "target_tenor": "5Y",
                "predicted_direction": -1,
                "confidence": 0.5166666666666667,
                "label": 1,
                "extra": {"feature_week_id": 202527},
            }
        ]
        benchmark = pd.DataFrame(
            {
                "predict_date": ["2025-07-04"],
                "tenor": ["5Y"],
                "predicted_direction": [-1],
                "confidence": [0.5166666666666667],
                "target_date": ["2025-07-11"],
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

    def next_trading_days(day: str, count: int) -> list[str]:
        return [
            value.strftime("%Y-%m-%d")
            for value in sorted(week_dates.values())
            if value > pd.Timestamp(day)
        ][:count]

    def week_id_for_date(day: str) -> int | None:
        return {
            value.strftime("%Y-%m-%d"): int(week_id)
            for week_id, value in week_dates.items()
        }.get(day)

    return SimpleNamespace(
        week_id_to_last_trading_day=last_trading_day,
        next_trading_days=next_trading_days,
        week_id_for_date=week_id_for_date,
    )
