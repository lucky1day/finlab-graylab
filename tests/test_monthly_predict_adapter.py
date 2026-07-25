from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class MonthlyPredictAdapterTests(unittest.TestCase):
    def test_monthly_adapter_maps_source_row_to_prediction_record(self) -> None:
        from shared.monthly_predict_adapter import run_monthly_prediction
        from shared.prediction_context import MONTHLY_TARGET_RULE

        evidence = SimpleNamespace(
            scheme_id="monthly_10y_rf_top5_0629",
            source_role="source_original_monthly_algorithm",
            source_package_hash="abc123",
            frequency="M10Y",
            target_tenor="10Y",
            final_select_id="10Y-01",
            model_id="monthly_10y_rf_top5",
            candidate_id="10Y::Random Forest::top5",
        )
        source_rows = [
            {
                "tenor": "10Y",
                "frequency": "M10Y",
                "final_select_id": "10Y-01",
                "candidate_id": "10Y::Random Forest::top5",
                "model_name": "Random Forest",
                "top_n": "top5",
                "feature_month_id": "2026-04",
                "feature_observation_date": "2026-04-15",
                "target_month_id": "2026-05",
                "y_pred": "0",
                "pred_proba_up": "0.31",
                "pred_proba_down": "0.69",
                "param_index": "7",
                "params_json": '{"n_estimators": 100}',
                "training_rows": "71",
                "validation_rows": "24",
                "source_output_date": "2026-04-15",
            }
        ]
        artifact = SimpleNamespace(
            path=Path("/tmp/monthly.csv"),
            source="shared_data_service_monthly",
        )
        engine = SimpleNamespace(dispose=lambda: None)
        database_config = object()

        with (
            patch(
                "shared.monthly_predict_adapter."
                "load_source_runtime_database_config",
                return_value=database_config,
            ),
            patch("shared.monthly_predict_adapter.require_monthly_source_evidence", return_value=evidence),
            patch("shared.monthly_predict_adapter.run_source_monthly_live", return_value=source_rows) as source_runner,
            patch("shared.monthly_predict_adapter.create_input_engine", return_value=engine) as create_engine,
            patch("shared.monthly_predict_adapter.get_calendar", return_value=_MonthlyCalendar()),
            patch("shared.monthly_predict_adapter.build_monthly_input_artifact", return_value=artifact) as build_monthly,
        ):
            records = run_monthly_prediction("monthly_10y_rf_top5_0629", "2026-04-15")

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.scheme_id, "monthly_10y_rf_top5_0629")
        self.assertEqual(record.target_tenor, "10Y")
        self.assertEqual(record.horizon, 30)
        self.assertEqual(record.predict_date, "2026-04-15")
        self.assertEqual(record.feature_date, "2026-04-15")
        self.assertEqual(record.target_date, "2026-05-15")
        self.assertEqual(record.predicted_direction, -1)
        self.assertEqual(record.confidence, 0.69)
        self.assertEqual(record.model_version, "monthly_10y_rf_top5")
        self.assertEqual(record.extra["target_rule"], MONTHLY_TARGET_RULE)
        self.assertEqual(record.extra["feature_month_id"], "2026-04")
        self.assertEqual(record.extra["target_month_id"], "2026-05")
        self.assertEqual(record.extra["y_pred"], 0)
        self.assertEqual(record.extra["pred_proba_down"], 0.69)
        self.assertEqual(record.extra["param_index"], 7)
        self.assertEqual(build_monthly.call_args.kwargs["end_date"], "2026-04-15")
        self.assertIs(
            source_runner.call_args.kwargs["database_config"],
            database_config,
        )
        self.assertIs(
            create_engine.call_args.kwargs["database_config"],
            database_config,
        )


class _MonthlyCalendar:
    def is_trading_day(self, day: str) -> bool:
        return day in {"2026-04-15", "2026-05-15"}

    def previous_trading_day(self, day: str) -> str:
        return {"2026-04-16": "2026-04-15", "2026-05-16": "2026-05-15"}[day]

    def next_trading_days(self, day: str, count: int) -> list[str]:
        return {
            "2026-04-14": ["2026-04-15"],
            "2026-05-14": ["2026-05-15"],
        }.get(day, [])[:count]


if __name__ == "__main__":
    unittest.main()
