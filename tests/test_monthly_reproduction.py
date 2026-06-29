from __future__ import annotations

import unittest
from unittest.mock import patch

from shared.models import PredictionRecord
from shared.prediction_context import MONTHLY_TARGET_RULE


class MonthlyReproductionTests(unittest.TestCase):
    def test_no_persist_reproduction_joins_monthly_actual_label(self) -> None:
        from backtests.monthly_0629_reproduction import run_monthly_0629_reproduction

        record = PredictionRecord(
            scheme_id="monthly_10y_rf_top5_0629",
            target_tenor="10Y",
            horizon=30,
            predict_date="2026-04-15",
            feature_date="2026-04-15",
            target_date="2026-05-15",
            predicted_direction=-1,
            confidence=0.69,
            model_version="monthly_10y_rf_top5",
            extra={
                "feature_month_id": "2026-04",
                "target_month_id": "2026-05",
                "target_rule": MONTHLY_TARGET_RULE,
                "frequency": "M10Y",
                "final_select_id": "10Y-01",
                "candidate_id": "10Y::Random Forest::top5",
                "model_name": "Random Forest",
                "top_n": "top5",
                "y_pred": 0,
                "pred_proba_up": 0.31,
                "pred_proba_down": 0.69,
                "param_index": 7,
                "params_json": '{"n_estimators": 100}',
                "training_rows": 71,
                "validation_rows": 24,
            },
        )

        with (
            patch("backtests.monthly_0629_reproduction.run_monthly_prediction", return_value=[record]),
            patch(
                "backtests.monthly_0629_reproduction._monthly_actual_lookup",
                return_value={("10Y", "2026-04-15", "2026-05-15", MONTHLY_TARGET_RULE): -1},
            ),
        ):
            payload = run_monthly_0629_reproduction(
                "monthly_10y_rf_top5_0629",
                predict_dates=["2026-04-15"],
                persist=False,
            )

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["row_count"], 1)
        row = payload["rows"][0]
        self.assertEqual(row["label"], -1)
        self.assertEqual(row["is_correct"], True)
        self.assertEqual(row["direction"], -1)
        self.assertEqual(row["target_rule"], MONTHLY_TARGET_RULE)


if __name__ == "__main__":
    unittest.main()
