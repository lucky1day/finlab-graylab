from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class WeeklyAverageLGBMPredictAdapterTests(unittest.TestCase):
    def test_platform_input_and_source_runner_share_one_database_binding(
        self,
    ) -> None:
        from shared.weekly_average_lgbm_predict_adapter import (
            run_weekly_average_lgbm_prediction,
        )

        evidence = SimpleNamespace(
            scheme_id="weekly_avg_1y_lgbm_0529",
            source_package_hash="a" * 64,
            frequency="W1Y",
            target_tenor="1Y",
            target_column="TB1YWI1C",
            model_id="WEEKLY-1Y-LGBM-01",
        )
        source_rows = [
            {
                "frequency": "W1Y",
                "effective_week_id": 202625,
                "pred_label": 1,
                "prob_up": 0.61,
                "source_output_date": "2026-06-22",
            }
        ]
        artifact = SimpleNamespace(
            path=Path("/tmp/weekly.csv"),
            source="shared_data_service_weekly",
        )
        engine = SimpleNamespace(dispose=lambda: None)
        database_config = object()

        with (
            patch(
                "shared.weekly_average_lgbm_predict_adapter."
                "load_source_runtime_database_config",
                return_value=database_config,
            ),
            patch(
                "shared.weekly_average_lgbm_predict_adapter."
                "require_weekly_average_source_evidence",
                return_value=evidence,
            ),
            patch(
                "shared.weekly_average_lgbm_predict_adapter."
                "run_source_weekly_live",
                return_value=source_rows,
            ) as source_runner,
            patch(
                "shared.weekly_average_lgbm_predict_adapter."
                "create_input_engine",
                return_value=engine,
            ) as create_engine,
            patch(
                "shared.weekly_average_lgbm_predict_adapter."
                "get_calendar",
                return_value=_WeeklyCalendar(),
            ),
            patch(
                "shared.weekly_average_lgbm_predict_adapter."
                "build_weekly_input_artifact",
                return_value=artifact,
            ),
        ):
            records = run_weekly_average_lgbm_prediction(
                "weekly_avg_1y_lgbm_0529",
                "2026-06-22",
            )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].feature_date, "2026-06-19")
        self.assertEqual(records[0].target_date, "2026-06-26")
        self.assertIs(
            source_runner.call_args.kwargs["database_config"],
            database_config,
        )
        self.assertIs(
            create_engine.call_args.kwargs["database_config"],
            database_config,
        )


class _WeeklyCalendar:
    def week_id_to_last_trading_day(self, week_id: int) -> str:
        return {
            202625: "2026-06-19",
            202626: "2026-06-26",
        }[week_id]

    def next_trading_days(
        self,
        feature_date: str,
        count: int,
    ) -> list[str]:
        del feature_date
        return ["2026-06-22"][:count]

    def week_id_for_date(self, day: str) -> int:
        del day
        return 202626


if __name__ == "__main__":
    unittest.main()
