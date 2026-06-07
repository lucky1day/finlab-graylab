from __future__ import annotations

import importlib
import json
import unittest
from pathlib import Path

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

    def test_weekly_output_builder_pivots_long_weekly_frames(self) -> None:
        module = importlib.import_module("schemes.weekly_10y_d_overlay.core.weekly_data_service")
        schema = ["week_id", "TB0YWI3C", "TB1YWI3C", "X_FACTOR"]
        raw = pd.DataFrame(
            [
                {"week_id": "202601", "indicators_code": "TB0YWI3C", "indicators_value": "2.10"},
                {"week_id": "202601", "indicators_code": "TB1YWI3C", "indicators_value": "1.20"},
                {"week_id": "202602", "indicators_code": "TB0YWI3C", "indicators_value": "2.12"},
            ]
        )
        derivative = pd.DataFrame(
            [
                {"week_id": "202601", "indicators_code": "X_FACTOR", "indicators_value": "8"},
                {"week_id": "202602", "indicators_code": "X_FACTOR", "indicators_value": "9"},
            ]
        )

        wide = module.build_weekly_output_from_frames(schema, raw, derivative)

        self.assertEqual(wide.columns.tolist(), schema)
        self.assertEqual(wide["week_id"].tolist(), [202601, 202602])
        self.assertEqual(float(wide.loc[0, "TB0YWI3C"]), 2.10)
        self.assertTrue(pd.isna(wide.loc[1, "TB1YWI3C"]))
        self.assertEqual(float(wide.loc[1, "X_FACTOR"]), 9.0)

    def test_weekly_output_builder_uses_daily_close_fallback_for_missing_weekly_close_codes(self) -> None:
        module = importlib.import_module("schemes.weekly_10y_d_overlay.core.weekly_data_service")
        schema = ["week_id", "TB0YWI3C", "TB1YWI3C", "TB5YWI3C", "X_FACTOR"]
        raw = pd.DataFrame(
            [
                {"week_id": 202622, "indicators_code": "X_FACTOR", "indicators_value": 8.0},
            ]
        )
        derivative = pd.DataFrame(
            [
                {"week_id": 202621, "indicators_code": "TB0YWI3C", "indicators_value": 1.70},
                {"week_id": 202621, "indicators_code": "TB1YWI3C", "indicators_value": 1.10},
                {"week_id": 202621, "indicators_code": "TB5YWI3C", "indicators_value": 1.40},
            ]
        )
        daily_fallback = pd.DataFrame(
            [
                {"week_id": 202622, "indicators_code": "TB0YWI3C", "indicators_value": 1.72},
                {"week_id": 202622, "indicators_code": "TB1YWI3C", "indicators_value": 1.11},
                {"week_id": 202622, "indicators_code": "TB5YWI3C", "indicators_value": 1.41},
            ]
        )

        wide = module.build_weekly_output_from_frames(
            schema,
            raw,
            derivative,
            daily_weekly_close_fallback=daily_fallback,
            end_week=202622,
        )

        row = wide[wide["week_id"].eq(202622)].iloc[0]
        self.assertEqual(float(row["TB0YWI3C"]), 1.72)
        self.assertEqual(float(row["TB1YWI3C"]), 1.11)
        self.assertEqual(float(row["TB5YWI3C"]), 1.41)
        self.assertEqual(float(row["X_FACTOR"]), 8.0)

    def test_wind_export_weekly_metadata_filters_active_pre_forecast_factors(self) -> None:
        module = importlib.import_module("schemes.weekly_10y_d_overlay.core.weekly_data_service")
        metadata = pd.DataFrame(
            [
                {
                    "indicators_code": "KEEP_CN",
                    "frequency": "周",
                    "lag_length": "1",
                    "status": "1",
                    "pre_forecast_flag": "1",
                },
                {
                    "indicators_code": "KEEP_ALIAS",
                    "frequency": "weekly",
                    "lag_length": "",
                    "status": "1",
                    "pre_forecast_flag": "1",
                },
                {
                    "indicators_code": "DROP_INACTIVE",
                    "frequency": "周",
                    "lag_length": "0",
                    "status": "0",
                    "pre_forecast_flag": "1",
                },
                {
                    "indicators_code": "DROP_NOT_PRE_FORECAST",
                    "frequency": "周",
                    "lag_length": "0",
                    "status": "1",
                    "pre_forecast_flag": "0",
                },
                {
                    "indicators_code": "DROP_DAILY",
                    "frequency": "日",
                    "lag_length": "0",
                    "status": "1",
                    "pre_forecast_flag": "1",
                },
            ]
        )

        selected = module.select_weekly_factor_metadata(metadata)

        self.assertEqual(selected["indicators_code"].tolist(), ["KEEP_CN", "KEEP_ALIAS"])
        self.assertEqual(selected["lag_length_num"].tolist(), [1, 0])

    def test_wind_export_weekly_builder_applies_lag_and_keeps_weekend_updates(self) -> None:
        module = importlib.import_module("schemes.weekly_10y_d_overlay.core.weekly_data_service")
        metadata = pd.DataFrame(
            [
                {
                    "indicators_code": "LAGGED",
                    "frequency": "周",
                    "lag_length": "1",
                    "status": "1",
                    "pre_forecast_flag": "1",
                },
                {
                    "indicators_code": "WEEKEND",
                    "frequency": "周",
                    "lag_length": "0",
                    "status": "1",
                    "pre_forecast_flag": "1",
                },
                {
                    "indicators_code": "FILTERED",
                    "frequency": "周",
                    "lag_length": "0",
                    "status": "0",
                    "pre_forecast_flag": "1",
                },
            ]
        )
        raw = pd.DataFrame(
            [
                {
                    "rdate": "2026-06-05",
                    "week_id": "202621",
                    "indicators_code": "LAGGED",
                    "indicators_value": "1.0",
                },
                {
                    "rdate": "2026-06-12",
                    "week_id": "202622",
                    "indicators_code": "LAGGED",
                    "indicators_value": "2.0",
                },
                {
                    "rdate": "2026-06-05",
                    "week_id": "202621",
                    "indicators_code": "WEEKEND",
                    "indicators_value": "9.0",
                },
                {
                    "rdate": "2026-06-06",
                    "week_id": "202621",
                    "indicators_code": "WEEKEND",
                    "indicators_value": "10.0",
                },
                {
                    "rdate": "2026-06-06",
                    "week_id": "202621",
                    "indicators_code": "FILTERED",
                    "indicators_value": "999.0",
                },
            ]
        )

        wide = module.build_wind_export_weekly_output_from_frames(metadata, raw)

        self.assertEqual(wide.columns.tolist(), ["week_id", "LAGGED", "WEEKEND"])
        self.assertEqual(wide["week_id"].tolist(), [202621, 202622])
        self.assertTrue(pd.isna(wide.loc[0, "LAGGED"]))
        self.assertEqual(float(wide.loc[1, "LAGGED"]), 1.0)
        self.assertEqual(float(wide.loc[0, "WEEKEND"]), 10.0)

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

    def test_weekly_schema_json_is_available(self) -> None:
        schema_path = PROJECT_ROOT / "schemes" / "weekly_10y_d_overlay" / "core" / "weekly_output_0529_columns.json"
        self.assertTrue(schema_path.exists(), "weekly schema JSON must be copied into the scheme core")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema[0], "week_id")
        self.assertIn("TB0YWI3C", schema)

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
