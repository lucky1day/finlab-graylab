from __future__ import annotations

import importlib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Weekly10YIntegrationTests(unittest.TestCase):
    def test_scheme_files_exist_and_config_declares_weekly_10y(self) -> None:
        scheme_dir = PROJECT_ROOT / "schemes" / "weekly_10y_d_overlay"
        config_path = scheme_dir / "config.yaml"
        predict_path = scheme_dir / "predict.py"

        self.assertTrue(config_path.exists(), "weekly_10y_d_overlay config.yaml must exist")
        self.assertTrue(predict_path.exists(), "weekly_10y_d_overlay predict.py must exist")

        text = config_path.read_text(encoding="utf-8")
        self.assertIn("scheme_id: weekly_10y_d_overlay", text)
        self.assertIn("frequency: weekly", text)
        self.assertIn('tenors: ["10Y"]', text)
        self.assertIn('cron: "30 11 * * 6"', text)
        self.assertIn("status: active", text)

    def test_saturday_prediction_dates_use_source_feature_week_id_and_calendar_target_date(self) -> None:
        module = importlib.import_module("schemes.weekly_10y_d_overlay.predict")

        context = module.resolve_weekly_prediction_context("2026-06-06", source_feature_week_id=202621)

        self.assertEqual(context.predict_date, "2026-06-06")
        self.assertEqual(context.feature_week_id, 202621)
        self.assertEqual(context.feature_date, "2026-06-05")
        self.assertEqual(context.target_week_id, 202622)
        self.assertEqual(context.target_date, "2026-06-12")

    def test_week_id_helpers_follow_production_first_monday_rule(self) -> None:
        module = importlib.import_module("schemes.weekly_10y_d_overlay.core.predictors")

        self.assertEqual(module.date_to_week_id("2026-05-09"), 202618)
        self.assertEqual(module.date_to_week_id("2026-05-23"), 202620)
        self.assertEqual(module.date_to_week_id("2026-06-06"), 202622)
        self.assertEqual(module.week_id_to_friday(202620).strftime("%Y-%m-%d"), "2026-05-22")
        self.assertEqual(module.next_week_id(202622), 202623)

    def test_weekly_predictor_reports_missing_feature_week_explicitly(self) -> None:
        module = importlib.import_module("schemes.weekly_10y_d_overlay.predict")
        context = module.resolve_weekly_prediction_context("2026-06-06")
        weekly_df = pd.DataFrame(
            [
                {"week_id": 202620, "TB0YWI3C": 1.707, "TB1YWI3C": 1.1425, "TB5YWI3C": 1.410},
                {"week_id": 202621, "TB0YWI3C": None, "TB1YWI3C": None, "TB5YWI3C": None},
            ]
        )

        with self.assertRaisesRegex(ValueError, "Latest supported Saturday predict_date is 2026-05-23"):
            module.validate_weekly_data_coverage(weekly_df, context)

    def test_weekly_predictor_rejects_feature_week_with_missing_required_values(self) -> None:
        module = importlib.import_module("schemes.weekly_10y_d_overlay.predict")
        context = module.resolve_weekly_prediction_context("2026-05-30")
        weekly_df = pd.DataFrame(
            [
                {"week_id": 202620, "TB0YWI3C": 1.707, "TB1YWI3C": 1.1425, "TB5YWI3C": 1.410},
                {"week_id": 202621, "TB0YWI3C": None, "TB1YWI3C": None, "TB5YWI3C": None},
            ]
        )

        with self.assertRaisesRegex(
            ValueError,
            "week_id 202621 .*missing required weekly values.*Latest supported Saturday predict_date is 2026-05-23",
        ):
            module.validate_weekly_data_coverage(weekly_df, context)

    def test_weekly_predictor_reports_latest_model_output_week(self) -> None:
        module = importlib.import_module("schemes.weekly_10y_d_overlay.core.predictors")
        predictions = pd.DataFrame([{"week_id": 202619, "d_pred_label": -1, "d_prob_up": 0.32}])

        with self.assertRaisesRegex(ValueError, "latest model output week_id is 202619"):
            module._latest_result(predictions, "dry_run", target_week_id=202621)

    def test_frontend_maps_next_monday_horizon_to_weekly_column(self) -> None:
        js = (PROJECT_ROOT / "frontend" / "aifin-shell.js").read_text(encoding="utf-8")
        marker = 'String(horizon) === "NEXT_MONDAY"'
        self.assertIn(marker, js)

    def test_frontend_groups_weekly_detail_rows_by_feature_month(self) -> None:
        js = (PROJECT_ROOT / "frontend" / "aifin-shell.js").read_text(encoding="utf-8")

        self.assertIn("function detailGroupMonth", js)
        self.assertIn("row.feature_date || row.predict_date", js)
        self.assertIn("dailyRowsByMonth(scheme.daily_rows || [], scheme.frequency, scheme.horizon)", js)
        self.assertIn("dailyRowsByMonth(metrics.daily_rows || [], scheme.frequency, scheme.horizon)", js)

    def test_frontend_positions_detail_panel_next_to_trigger_without_scrolling(self) -> None:
        js = (PROJECT_ROOT / "frontend" / "aifin-shell.js").read_text(encoding="utf-8")

        self.assertIn("function positionFactorCalendarPanel", js)
        self.assertIn("trigger.getBoundingClientRect()", js)
        self.assertIn("panel.style.top =", js)
        self.assertNotIn("scrollIntoView", js)
        self.assertIn(
            'openFactorCalendar(calendarButton.getAttribute("data-factor-calendar-month"), calendarButton)',
            js,
        )

    def test_weekly_backtest_row_dates_follow_saturday_prediction_rule(self) -> None:
        module = importlib.import_module("backtests.weekly_10y_d_overlay_reproduction")
        predictions = pd.DataFrame(
            [
                {
                    "week_id": 202622,
                    "actual_label": -1,
                    "d_pred_label": 1,
                    "score_pred_label": -1,
                    "d_prob_up": 0.62,
                    "future_return": -0.001,
                    "d_model2_pred_label": 1,
                    "d_model_disagree": True,
                    "d_overlay": True,
                }
            ]
        )

        rows = module.prediction_frame_to_backtest_rows(predictions)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["predict_date"], "2026-06-06")
        self.assertEqual(rows[0]["feature_date"], "2026-06-05")
        self.assertEqual(rows[0]["target_date"], "2026-06-12")
        self.assertEqual(rows[0]["horizon"], 6)
        self.assertEqual(rows[0]["extra"]["target_week_id"], 202623)

    def test_weekly_backtest_rows_use_source_month_date_for_2025_week_mapping(self) -> None:
        module = importlib.import_module("backtests.weekly_10y_d_overlay_reproduction")
        predictions = pd.DataFrame(
            [
                {
                    "week_id": 202527,
                    "month_date": pd.Timestamp("2025-07-04"),
                    "actual_label": 1,
                    "d_pred_label": -1,
                    "score_pred_label": -1,
                    "d_prob_up": 0.32,
                    "future_return": 0.015,
                    "d_model2_pred_label": -1,
                    "d_model_disagree": False,
                    "d_overlay": False,
                }
            ]
        )

        rows = module.prediction_frame_to_backtest_rows(predictions)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["feature_date"], "2025-07-04")
        self.assertEqual(rows[0]["predict_date"], "2025-07-05")
        self.assertEqual(rows[0]["target_date"], "2025-07-11")
        self.assertEqual(rows[0]["extra"]["feature_week_id"], 202527)
        self.assertEqual(rows[0]["extra"]["target_week_id"], 202528)

    def test_weekly_monthly_metrics_bucket_by_feature_month(self) -> None:
        module = importlib.import_module("backtests.weekly_10y_d_overlay_reproduction")
        base = {
            "benchmark_id": "model_muti_0529",
            "scheme_id": "weekly_10y_d_overlay",
            "target_tenor": "10Y",
            "horizon": 6,
            "model_pred": -1,
            "confidence": 0.32,
            "source_row": {},
            "extra": {"frequency": "weekly"},
        }
        rows = [
            {
                **base,
                "predict_date": "2025-10-25",
                "feature_date": "2025-10-24",
                "target_date": "2025-10-31",
                "label": -1,
                "predicted_direction": 1,
            },
            {
                **base,
                "predict_date": "2025-11-01",
                "feature_date": "2025-10-31",
                "target_date": "2025-11-07",
                "label": 1,
                "predicted_direction": -1,
            },
        ]

        output = module.make_weekly_run_output("2025-10-25", "2025-11-01", rows)
        metrics = {row["month"]: row for row in output.monthly_metrics}

        self.assertEqual(metrics["2025-10"]["sample_count"], 2)
        self.assertNotIn("2025-11", metrics)

    def test_weekly_10y_backtest_uses_common_weekly_input_artifact(self) -> None:
        module = importlib.import_module("backtests.weekly_10y_d_overlay_reproduction")
        weekly_df = pd.DataFrame([{"week_id": 202622, "TB0YWI3C": 1.80, "TB1YWI3C": 1.20, "TB5YWI3C": 1.50}])
        predictions = pd.DataFrame(
            [
                {
                    "week_id": 202622,
                    "actual_label": -1,
                    "d_pred_label": -1,
                    "score_pred_label": -1,
                    "d_prob_up": 0.28,
                    "future_return": -0.001,
                    "d_model2_pred_label": -1,
                    "d_model_disagree": False,
                    "d_overlay": False,
                }
            ]
        )
        artifact = SimpleNamespace(
            dataframe=weekly_df,
            path=Path("/tmp/weekly_output_historical_backtest.csv"),
            source="test_common_weekly_input_artifact",
        )
        fake_engine = SimpleNamespace()

        with patch.object(module, "build_weekly_input_artifact", return_value=artifact) as build:
            with patch.object(module, "predict_w10y", return_value=SimpleNamespace(predictions=predictions)) as predict:
                payload = module.run_weekly_10y_d_overlay_reproduction(engine=fake_engine, persist=False)

        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["summary"]["weekly_input_artifact_source"], "test_common_weekly_input_artifact")
        self.assertEqual(payload["summary"]["weekly_input_artifact_path"], str(artifact.path))
        self.assertIs(predict.call_args.args[0], weekly_df)
        self.assertEqual(build.call_args.kwargs["scheme_id"], "weekly_10y_d_overlay")
        self.assertEqual(build.call_args.kwargs["predict_date"], "historical_backtest")

    def test_weekly_monthly_metrics_keep_legacy_cross_year_week(self) -> None:
        module = importlib.import_module("backtests.weekly_10y_d_overlay_reproduction")
        common = {
            "actual_label": -1,
            "d_pred_label": -1,
            "score_pred_label": -1,
            "d_prob_up": 0.32,
            "d_model2_pred_label": -1,
            "d_model_disagree": False,
            "d_overlay": False,
        }
        predictions = pd.DataFrame(
            [
                {"week_id": 202601, "month_date": pd.Timestamp("2026-01-02"), "TB0YWI3C": 1.8860, **common},
                {"week_id": 202553, "month_date": pd.Timestamp("2026-01-04"), "TB0YWI3C": 1.8500, **common},
                {"week_id": 202602, "month_date": pd.Timestamp("2026-01-09"), "TB0YWI3C": 1.8325, **common},
                {"week_id": 202603, "month_date": pd.Timestamp("2026-01-16"), "TB0YWI3C": 1.8210, **common},
                {"week_id": 202604, "month_date": pd.Timestamp("2026-01-23"), "TB0YWI3C": 1.8010, **common},
                {"week_id": 202605, "month_date": pd.Timestamp("2026-01-30"), "TB0YWI3C": 1.8010, **common},
            ]
        )

        rows = module.prediction_frame_to_backtest_rows(predictions)
        output = module.make_weekly_run_output("2026-01-03", "2026-01-31", rows)
        metrics = {row["month"]: row for row in output.monthly_metrics}
        rows_by_week = {row["extra"]["feature_week_id"]: row for row in rows}

        self.assertIn(202553, {row["extra"]["feature_week_id"] for row in rows})
        self.assertEqual(rows_by_week[202601]["extra"]["target_week_id"], 202553)
        self.assertEqual(rows_by_week[202553]["extra"]["target_week_id"], 202602)
        self.assertEqual(rows_by_week[202553]["target_date"], "2026-01-09")
        self.assertEqual(rows_by_week[202553]["label"], -1)
        for row in rows:
            self.assertLess(row["feature_date"], row["target_date"])
        self.assertEqual(metrics["2026-01"]["sample_count"], 6)

    def test_weekly_backtest_rows_keep_canonical_week_id_for_duplicate_cross_year_dates(self) -> None:
        module = importlib.import_module("backtests.weekly_10y_d_overlay_reproduction")
        common = {
            "actual_label": -1,
            "d_pred_label": -1,
            "score_pred_label": -1,
            "d_prob_up": 0.32,
            "future_return": -0.001,
            "d_model2_pred_label": -1,
            "d_model_disagree": False,
            "d_overlay": False,
        }
        predictions = pd.DataFrame(
            [
                {"week_id": 202553, **common},
                {"week_id": 202601, **common},
            ]
        )

        rows = module.prediction_frame_to_backtest_rows(predictions)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["predict_date"], "2026-01-10")
        self.assertEqual(rows[0]["extra"]["feature_week_id"], 202601)


if __name__ == "__main__":
    unittest.main()
