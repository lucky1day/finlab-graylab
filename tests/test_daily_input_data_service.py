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
        fake_engine = SimpleNamespace(dispose=lambda: None)
        fake_calendar = SimpleNamespace(previous_trading_day=lambda value: "2026-06-04")
        with patch.object(predict, "build_daily_input_artifact", return_value=artifact) as build:
            with patch.object(predict, "create_input_engine", return_value=fake_engine):
                with patch.object(predict, "get_calendar", return_value=fake_calendar):
                    with patch.object(predict, "TENOR_CONFIGS", {"daily_10y": fake_config}):
                        with patch.object(predict, "_configured_tenors", return_value={"10Y"}):
                            with patch.object(predict, "predict_latest_for_config", return_value=fake_result) as predict_latest:
                                records = predict.run("2026-06-05")

        self.assertEqual(len(records), 1)
        self.assertIs(predict_latest.call_args.args[0], daily_df)
        self.assertEqual(predict_latest.call_args.kwargs["target_date"], "2026-06-05")
        self.assertEqual(records[0].predict_date, "2026-06-05")
        self.assertEqual(records[0].feature_date, "2026-06-04")
        self.assertEqual(records[0].target_date, "2026-06-05")
        kwargs = build.call_args.kwargs
        self.assertEqual(kwargs["end_date"], "2026-06-04")
        self.assertEqual(kwargs["scheme_id"], "t1_daily")
        self.assertEqual(kwargs["predict_date"], "2026-06-05")
        self.assertIs(kwargs["engine"], fake_engine)

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
        fake_engine = SimpleNamespace(dispose=lambda: None)
        fake_calendar = SimpleNamespace(previous_trading_day=lambda value: "2026-06-04")
        with patch.object(predict, "build_daily_input_artifact", return_value=artifact) as build:
            with patch.object(predict, "create_input_engine", return_value=fake_engine):
                with patch.object(predict, "get_calendar", return_value=fake_calendar):
                    with patch.object(predict, "predict_latest_for_module", return_value=fake_result) as predict_latest:
                        with patch.object(predict, "_target_date_from_feature_date", return_value="2026-06-12"):
                            records = predict.run("2026-06-05")

        self.assertEqual(len(records), 4)
        self.assertIs(predict_latest.call_args.args[1], daily_df)
        kwargs = build.call_args.kwargs
        self.assertEqual(kwargs["end_date"], "2026-06-04")
        self.assertEqual(kwargs["scheme_id"], "t5_daily")
        self.assertEqual(kwargs["predict_date"], "2026-06-05")
        self.assertEqual(records[0].feature_date, "2026-06-04")


class ReproductionDailyDataServiceTests(unittest.TestCase):
    def test_daily_reproduction_does_not_import_raw_daily_builder(self) -> None:
        from backtests import daily_0529_reproduction as daily_reproduction

        self.assertFalse(hasattr(daily_reproduction, "build_daily_output_from_db"))

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

    def test_framework_db_aligned_daily_uses_common_input_artifact(self) -> None:
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

        artifact = SimpleNamespace(dataframe=generated, path=Path("/tmp/framework_daily_output.csv"), source="test")
        with patch.object(daily_reproduction, "build_daily_input_artifact", return_value=artifact) as build:
            _, aligned = daily_reproduction.build_framework_db_aligned_daily(csv_df=canonical, engine=engine)

        self.assertEqual(aligned["TB0YWI0C"].tolist(), [2.2345])
        kwargs = build.call_args.kwargs
        self.assertEqual(kwargs["start_date"], "2026-01-02")
        self.assertEqual(kwargs["end_date"], "2026-01-02")
        self.assertIs(kwargs["engine"], engine)
        self.assertEqual(kwargs["scheme_id"], "daily_framework")

    def test_t1_backtest_rows_start_from_predict_date_2025_01_01(self) -> None:
        from backtests import daily_0529_reproduction as daily_reproduction
        from schemes.t1_daily.core import config as t1_config
        from schemes.t1_daily.core import lgbm_predictor

        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2024-12-31", "2025-01-01", "2025-01-02"]),
                "close": [1.0, 1.1, 1.2],
            }
        )
        fake_configs = {
            "D1Y": SimpleNamespace(frequency="D1Y", tenor="3Y", close_col="close", threshold=0.0),
            "D5Y": SimpleNamespace(frequency="D5Y", tenor="5Y", close_col="close", threshold=0.0),
            "D10Y": SimpleNamespace(frequency="D10Y", tenor="10Y", close_col="close", threshold=0.0),
        }

        def fake_predict_latest_for_config(_daily: pd.DataFrame, cfg: SimpleNamespace, *, target_date: str) -> SimpleNamespace:
            feature_date = "2024-12-31" if target_date == "2025-01-01" else "2025-01-01"
            return SimpleNamespace(
                config=cfg,
                tenor=cfg.tenor,
                frequency=cfg.frequency,
                feature_date=feature_date,
                target_date=target_date,
                pred_label=1,
                prob_up=0.6,
                base_pred=1,
                threshold_used=0.0,
                base_decision="model",
                vote_sum=1,
                decision="test",
                train_start="2020-01-01",
                train_end=feature_date,
                feature_columns=["close"],
            )

        with patch.object(t1_config, "TENOR_CONFIGS", fake_configs):
            with patch.object(lgbm_predictor, "predict_latest_for_config", side_effect=fake_predict_latest_for_config):
                rows = daily_reproduction.run_t1_framework_backtest(daily_df)

        self.assertEqual(len(rows), 2)
        self.assertEqual({row["predict_date"] for row in rows}, {"2025-01-01"})
        self.assertEqual({row["target_tenor"] for row in rows}, {"5Y", "10Y"})
        self.assertTrue(all(row["predict_date"] >= "2025-01-01" for row in rows))

    def test_t1_backtest_includes_final_may29_target_date(self) -> None:
        from backtests import daily_0529_reproduction as daily_reproduction
        from schemes.t1_daily.core import config as t1_config
        from schemes.t1_daily.core import lgbm_predictor

        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-05-28", "2026-05-29"]),
                "close": [1.0, 0.9],
            }
        )
        fake_configs = {
            "D1Y": SimpleNamespace(frequency="D1Y", tenor="3Y", close_col="close", threshold=0.0),
            "D5Y": SimpleNamespace(frequency="D5Y", tenor="5Y", close_col="close", threshold=0.0),
            "D10Y": SimpleNamespace(frequency="D10Y", tenor="10Y", close_col="close", threshold=0.0),
        }
        seen_target_dates: list[str] = []

        def fake_predict_latest_for_config(_daily: pd.DataFrame, cfg: SimpleNamespace, *, target_date: str) -> SimpleNamespace:
            seen_target_dates.append(target_date)
            return SimpleNamespace(
                config=cfg,
                tenor=cfg.tenor,
                frequency=cfg.frequency,
                feature_date="2026-05-28",
                target_date=target_date,
                pred_label=-1,
                prob_up=0.4,
                base_pred=-1,
                threshold_used=0.0,
                base_decision="model",
                vote_sum=-1,
                decision="test",
                train_start="2020-01-01",
                train_end="2026-05-28",
                feature_columns=["close"],
            )

        with patch.object(t1_config, "TENOR_CONFIGS", fake_configs):
            with patch.object(lgbm_predictor, "predict_latest_for_config", side_effect=fake_predict_latest_for_config):
                rows = daily_reproduction.run_t1_framework_backtest(daily_df)

        self.assertIn("2026-05-29", seen_target_dates)
        final_rows = [row for row in rows if row["target_date"] == "2026-05-29"]
        self.assertEqual({row["target_tenor"] for row in final_rows}, {"5Y", "10Y"})
        self.assertEqual({row["predict_date"] for row in final_rows}, {"2026-05-28"})

    def test_t5_backtest_rows_stop_before_live_target_start(self) -> None:
        from backtests import daily_0529_reproduction as daily_reproduction
        from schemes.t5_daily import latest_prediction
        from schemes.t5_daily.core import common_utils

        daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-05-22", "2026-05-25"]),
                "close": [1.0, 1.1],
            }
        )
        fake_module = SimpleNamespace(
            CLOSE_COL="close",
            HORIZON=5,
            build_custom_vote_signals=lambda _daily, _recipe: pd.DataFrame(index=range(len(daily_df))),
            RECIPE=[],
        )
        fake_modules = {tenor: fake_module for tenor in ("3Y", "5Y", "7Y", "10Y")}
        target_dates = {
            "2026-05-22": "2026-05-29",
            "2026-05-25": "2026-06-01",
        }

        def fake_row(module, daily, labels, close, features, fallback_signal, vote_df, idx, n_jobs):
            feature_date = daily.loc[idx, "date"].strftime("%Y-%m-%d")
            return {
                "scheme_id": "t5_daily",
                "target_tenor": "5Y",
                "horizon": module.HORIZON,
                "predict_date": feature_date,
                "feature_date": feature_date,
                "target_date": target_dates[feature_date],
                "predicted_direction": 1,
                "label": 1,
            }

        with patch.object(latest_prediction, "TENOR_MODULES", fake_modules):
            with patch.object(latest_prediction, "_build_features", return_value=pd.DataFrame({"x": [0.0, 0.0]})):
                with patch.object(common_utils, "make_labels", return_value=(None, [1, 1])):
                    with patch.object(common_utils, "build_fallback_signal", return_value=pd.Series([1, 1])):
                        with patch.object(daily_reproduction, "_run_t5_single_index", side_effect=fake_row):
                            rows = daily_reproduction.run_t5_framework_backtest(daily_df, n_jobs=1)

        self.assertTrue(rows)
        self.assertTrue(all(row["target_date"] < daily_reproduction.LIVE_TARGET_START_DATE for row in rows))
        self.assertNotIn("2026-06-01", {row["target_date"] for row in rows})


if __name__ == "__main__":
    unittest.main()
