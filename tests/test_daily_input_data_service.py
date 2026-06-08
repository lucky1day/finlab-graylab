from __future__ import annotations

import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import pandas as pd


class DailyInputDataServiceTests(unittest.TestCase):
    def test_daily_data_service_target_columns_match_upstream_daily_output_anchor(self) -> None:
        from shared.data_service import DAILY_TARGETS

        self.assertEqual(DAILY_TARGETS, ("TB1YWI0C", "TB5YWI0C", "TB0YWI0C"))


class DailyPredictAdapterDataServiceTests(unittest.TestCase):
    def test_t1_daily_predict_uses_common_daily_input_artifact(self) -> None:
        from schemes.t1_daily import predict

        daily_df = pd.DataFrame({"date": pd.to_datetime(["2026-06-04"]), "TB0YWI0C": [2.0]})
        fake_config = SimpleNamespace(tenor="10Y", window=240)
        fake_result = SimpleNamespace(
            rdate="2026-06-05",
            target_date="2026-06-05",
            feature_date="2026-06-04",
            pred_label=1,
            prob_up=0.6,
            base_pred=1,
            base_decision="model",
            vote_sum=1,
            decision="test",
            threshold_used=0.5,
            train_start="2025-01-01",
            train_end="2026-06-03",
        )

        artifact = SimpleNamespace(dataframe=daily_df, path=Path("/tmp/t1_daily_output.csv"), source="test")
        with patch.object(predict, "build_daily_input_artifact", return_value=artifact) as build:
            with patch.object(predict, "TENOR_CONFIGS", {"daily_10y": fake_config}):
                with patch.object(predict, "_configured_tenors", return_value={"10Y"}):
                    with patch.object(predict, "predict_latest_for_config", return_value=fake_result) as predict_latest:
                        records = predict.run("2026-06-05")

        self.assertEqual(len(records), 1)
        self.assertIs(predict_latest.call_args.args[0], daily_df)
        kwargs = build.call_args.kwargs
        self.assertEqual(kwargs["end_date"], "2026-06-05")
        self.assertEqual(kwargs["scheme_id"], "t1_daily")
        self.assertEqual(kwargs["predict_date"], "2026-06-05")

    def test_t5_daily_predict_uses_common_daily_input_artifact(self) -> None:
        from schemes.t5_daily import predict

        daily_df = pd.DataFrame({"date": pd.to_datetime(["2026-06-04"]), "TB0YWI0C": [2.0]})
        fake_result = SimpleNamespace(
            feature_date="2026-06-04",
            vote_pred=-1,
            confidence=0.4,
            model_version="test",
            model_pred=-1,
            vote_sum=-1,
            signal_sum=0,
            threshold=0.5,
            decision="test",
            vote_signals={},
        )

        artifact = SimpleNamespace(dataframe=daily_df, path=Path("/tmp/t5_daily_output.csv"), source="test")
        with patch.object(predict, "build_daily_input_artifact", return_value=artifact) as build:
            with patch.object(predict, "predict_latest_for_module", return_value=fake_result) as predict_latest:
                with patch.object(predict, "_target_date_from_feature_date", return_value="2026-06-12"):
                    records = predict.run("2026-06-05")

        self.assertEqual(len(records), 4)
        self.assertIs(predict_latest.call_args.args[1], daily_df)
        kwargs = build.call_args.kwargs
        self.assertEqual(kwargs["end_date"], "2026-06-05")
        self.assertEqual(kwargs["scheme_id"], "t5_daily")
        self.assertEqual(kwargs["predict_date"], "2026-06-05")


class ReproductionDailyDataServiceTests(unittest.TestCase):
    def test_db_aligned_daily_uses_common_input_artifact_when_upstream_mode(self) -> None:
        from backtests import daily_0529_reproduction as daily_reproduction

        canonical = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-01-02"]),
                "TB0YWI0C": [2.2345],
            }
        )
        generated = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-01-02"]),
                "TB0YWI0C": [2.2345],
            }
        )
        engine = object()

        artifact = SimpleNamespace(dataframe=generated, path=Path("/tmp/reproduction_daily_output.csv"), source="test")
        with patch.object(daily_reproduction, "build_daily_input_artifact", return_value=artifact) as build:
            _, aligned = daily_reproduction.build_db_aligned_daily(csv_df=canonical, engine=engine, upstream_mode=True)

        self.assertEqual(aligned["TB0YWI0C"].tolist(), [2.2345])
        kwargs = build.call_args.kwargs
        self.assertEqual(kwargs["start_date"], "2026-01-02")
        self.assertEqual(kwargs["end_date"], "2026-01-02")
        self.assertIs(kwargs["engine"], engine)
        self.assertEqual(kwargs["scheme_id"], "daily_common")


if __name__ == "__main__":
    unittest.main()
