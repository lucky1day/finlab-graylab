from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from tests.test_native_generation_input_artifacts import (
    _generation_context,
)


def _generation_with_target_calendar():
    context = _generation_context()
    trading_days = (
        "2026-07-23",
        "2026-07-24",
        "2026-07-27",
        "2026-07-28",
        "2026-07-29",
        "2026-07-30",
    )
    frames = dict(context._frames)
    frames["t_trade_calendar.csv"] = pd.DataFrame(
        [
            {"rdate": trading_day, "trade_flag": "1"}
            for trading_day in trading_days
        ]
    )
    frames["api_wind_date.csv"] = pd.DataFrame(
        [
            {
                "rdate": trading_day,
                "week_id": (
                    "202629"
                    if trading_day in {"2026-07-23", "2026-07-24"}
                    else "202630"
                ),
            }
            for trading_day in trading_days
        ]
    )
    return replace(context, _frames=frames)


def _generation_environment(context) -> dict[str, str]:
    from shared import input_artifacts

    return {
        input_artifacts.NATIVE_INPUT_MODE_ENV:
            input_artifacts.NATIVE_INPUT_MODE,
        input_artifacts.NATIVE_MANIFEST_PATH_ENV:
            str(context.manifest_path),
        input_artifacts.NATIVE_GENERATION_ID_ENV:
            context.generation_id,
        input_artifacts.NATIVE_MANIFEST_SHA256_ENV:
            context.manifest_sha256,
        input_artifacts.NATIVE_BUSINESS_DATE_ENV:
            context.business_date,
        input_artifacts.NATIVE_FEATURE_DATE_ENV:
            context.feature_date,
    }


class T1T5NativeGenerationTests(unittest.TestCase):
    def test_t1_daily_consumes_only_frozen_generation_and_is_deterministic(
        self,
    ) -> None:
        from schemes.t1_daily import predict
        from shared import input_artifacts

        context = _generation_with_target_calendar()
        artifacts = []
        original_builder = input_artifacts.build_daily_input_artifact
        configs = {
            "daily_5y": SimpleNamespace(tenor="5Y", window=120),
            "daily_10y": SimpleNamespace(tenor="10Y", window=240),
        }

        def build_from_generation(**kwargs):
            artifact = original_builder(
                **kwargs,
                output_root=artifact_root,
            )
            artifacts.append(artifact)
            return artifact

        def predict_latest(daily_df, config, *, target_date):
            self.assertEqual(
                daily_df["date"].dt.strftime("%Y-%m-%d").tolist(),
                ["2026-07-22", "2026-07-23"],
            )
            return SimpleNamespace(
                target_date=target_date,
                feature_date="2026-07-23",
                pred_label=1 if config.tenor == "10Y" else -1,
                prob_up=0.6 if config.tenor == "10Y" else 0.4,
                base_pred=1,
                base_decision="model",
                vote_sum=1,
                decision="test",
                threshold_used=0.5,
                train_start="2025-01-01",
                train_end="2026-07-22",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir)
            with (
                patch.dict(
                    os.environ,
                    _generation_environment(context),
                    clear=True,
                ),
                patch.object(
                    input_artifacts,
                    "open_native_generation",
                    return_value=context,
                ),
                patch.object(
                    input_artifacts._data_service,
                    "create_sqlalchemy_engine",
                ) as db_factory,
                patch.object(
                    input_artifacts._data_service,
                    "build_daily_output_from_db",
                ) as db_builder,
                patch.object(
                    predict,
                    "build_daily_input_artifact",
                    side_effect=build_from_generation,
                ),
                patch.object(predict, "TENOR_CONFIGS", configs),
                patch.object(
                    predict,
                    "predict_latest_for_config",
                    side_effect=predict_latest,
                ),
            ):
                first = predict.run("2026-07-24")
                second = predict.run("2026-07-24")

        db_factory.assert_not_called()
        db_builder.assert_not_called()
        self.assertEqual(first, second)
        self.assertEqual(
            {
                (record.target_tenor, record.predicted_direction)
                for record in first
            },
            {("5Y", -1), ("10Y", 1)},
        )
        self.assertTrue(
            all(record.feature_date == "2026-07-23" for record in first)
        )
        self.assertTrue(
            all(record.target_date == "2026-07-24" for record in first)
        )
        self.assertTrue(
            all(
                record.extra
                and record.extra["decision"] == "test"
                and record.extra["threshold"] == 0.5
                for record in first
            )
        )
        self.assertEqual(len(artifacts), 2)
        self.assertTrue(
            all(
                artifact.metadata["input_generation_id"]
                == context.generation_id
                for artifact in artifacts
            )
        )

    def test_t5_daily_consumes_only_frozen_generation_and_is_deterministic(
        self,
    ) -> None:
        from schemes.t5_daily import predict
        from shared import input_artifacts

        context = _generation_with_target_calendar()
        artifacts = []
        original_builder = input_artifacts.build_daily_input_artifact

        def build_from_generation(**kwargs):
            artifact = original_builder(
                **kwargs,
                output_root=artifact_root,
            )
            artifacts.append(artifact)
            return artifact

        def predict_latest(_module, daily_df, _predict_date):
            self.assertEqual(
                daily_df["date"].dt.strftime("%Y-%m-%d").tolist(),
                ["2026-07-22", "2026-07-23"],
            )
            return SimpleNamespace(
                feature_date="2026-07-23",
                vote_pred=-1,
                confidence=0.4,
                model_version="frozen-test",
                model_pred=-1,
                vote_sum=-1,
                signal_sum=-2,
                threshold=0.5,
                decision="test",
                vote_signals={"signal": -1},
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir)
            with (
                patch.dict(
                    os.environ,
                    _generation_environment(context),
                    clear=True,
                ),
                patch.object(
                    input_artifacts,
                    "open_native_generation",
                    return_value=context,
                ),
                patch.object(
                    input_artifacts._data_service,
                    "create_sqlalchemy_engine",
                ) as db_factory,
                patch.object(
                    input_artifacts._data_service,
                    "build_daily_output_from_db",
                ) as db_builder,
                patch.object(
                    predict,
                    "build_daily_input_artifact",
                    side_effect=build_from_generation,
                ),
                patch.object(
                    predict,
                    "predict_latest_for_module",
                    side_effect=predict_latest,
                ),
            ):
                first = predict.run("2026-07-24")
                second = predict.run("2026-07-24")

        db_factory.assert_not_called()
        db_builder.assert_not_called()
        self.assertEqual(first, second)
        self.assertEqual(
            {record.target_tenor for record in first},
            {"3Y", "5Y", "7Y", "10Y"},
        )
        self.assertTrue(
            all(record.feature_date == "2026-07-23" for record in first)
        )
        self.assertTrue(
            all(record.target_date == "2026-07-30" for record in first)
        )
        self.assertTrue(
            all(
                record.extra
                and record.extra["decision"] == "test"
                and record.extra["signal_sum"] == -2
                for record in first
            )
        )
        self.assertEqual(len(artifacts), 2)
        self.assertTrue(
            all(
                artifact.metadata["input_generation_manifest_sha256"]
                == context.manifest_sha256
                for artifact in artifacts
            )
        )


if __name__ == "__main__":
    unittest.main()
