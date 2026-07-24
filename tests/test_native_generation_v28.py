from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_native_generation_t1_t5 import (
    _generation_environment,
    _generation_with_target_calendar,
)


class V28NativeGenerationTests(unittest.TestCase):
    def test_daily_5y_v28_consumes_one_frozen_generation(self) -> None:
        self._assert_v28_scheme_uses_frozen_generation(
            module_name="schemes.daily_5y_2_v28.predict",
            expected_scheme_id="daily_5y_2_v28",
            expected_tenor="5Y",
            expected_model_version="5y_2_v28",
        )

    def test_daily_7y_v28_consumes_one_frozen_generation(self) -> None:
        self._assert_v28_scheme_uses_frozen_generation(
            module_name="schemes.daily_7y_1_v28.predict",
            expected_scheme_id="daily_7y_1_v28",
            expected_tenor="7Y",
            expected_model_version="7y_1_v28",
        )

    def _assert_v28_scheme_uses_frozen_generation(
        self,
        *,
        module_name: str,
        expected_scheme_id: str,
        expected_tenor: str,
        expected_model_version: str,
    ) -> None:
        from shared import input_artifacts

        predict = importlib.import_module(module_name)
        context = _generation_with_target_calendar()
        artifacts: dict[str, list] = {
            "daily": [],
            "weekly": [],
            "monthly": [],
        }
        original_builders = {
            "daily": input_artifacts.build_daily_input_artifact,
            "weekly": input_artifacts.build_weekly_input_artifact,
            "monthly": input_artifacts.build_monthly_input_artifact,
        }

        def build_from_generation(frequency: str, **kwargs):
            artifact = original_builders[frequency](
                **kwargs,
                output_root=artifact_root,
            )
            artifacts[frequency].append(artifact)
            return artifact

        result = {
            "anchor_date": "2026-07-23",
            "prediction": -1,
            "confidence": 0.4,
            "vote_score": -0.72,
            "ens_prob": 0.28,
            "true_label": None,
            "model_version": expected_model_version,
        }

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
                ) as daily_db_builder,
                patch.object(
                    input_artifacts._data_service,
                    "build_weekly_output_from_db",
                ) as weekly_db_builder,
                patch.object(
                    input_artifacts._data_service,
                    "build_monthly_output_from_db",
                ) as monthly_db_builder,
                patch.object(
                    predict,
                    "build_daily_input_artifact",
                    side_effect=lambda **kwargs: build_from_generation(
                        "daily",
                        **kwargs,
                    ),
                ),
                patch.object(
                    predict,
                    "build_weekly_input_artifact",
                    side_effect=lambda **kwargs: build_from_generation(
                        "weekly",
                        **kwargs,
                    ),
                ),
                patch.object(
                    predict,
                    "build_monthly_input_artifact",
                    side_effect=lambda **kwargs: build_from_generation(
                        "monthly",
                        **kwargs,
                    ),
                ),
                patch.object(
                    predict,
                    "run_v28_for_feature_date",
                    return_value=result,
                ) as inference,
            ):
                first = predict.run("2026-07-24")
                second = predict.run("2026-07-24")

        db_factory.assert_not_called()
        daily_db_builder.assert_not_called()
        weekly_db_builder.assert_not_called()
        monthly_db_builder.assert_not_called()
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        record = first[0]
        self.assertEqual(record.scheme_id, expected_scheme_id)
        self.assertEqual(record.target_tenor, expected_tenor)
        self.assertEqual(record.feature_date, "2026-07-23")
        self.assertEqual(record.target_date, "2026-07-30")
        self.assertEqual(record.predicted_direction, -1)
        self.assertEqual(record.model_version, expected_model_version)
        self.assertEqual(record.extra["vote_score"], -0.72)
        self.assertEqual(record.extra["ens_prob"], 0.28)
        self.assertEqual(inference.call_count, 2)
        for call in inference.call_args_list:
            kwargs = call.kwargs
            self.assertEqual(
                kwargs["daily_df"]["date"]
                .dt.strftime("%Y-%m-%d")
                .tolist(),
                ["2026-07-22", "2026-07-23"],
            )
            self.assertEqual(
                kwargs["weekly_df"]["week_id"].tolist(),
                [202628, 202629],
            )
            self.assertEqual(
                kwargs["monthly_df"]["month_id"].tolist(),
                ["202606", "202607"],
            )
            self.assertEqual(
                kwargs["date_to_week"],
                {"2026-07-23": 202629},
            )
            self.assertEqual(kwargs["feature_date"], "2026-07-23")
            self.assertFalse(kwargs["require_labels"])

        for frequency in ("daily", "weekly", "monthly"):
            self.assertEqual(len(artifacts[frequency]), 2)
            for artifact in artifacts[frequency]:
                self.assertEqual(
                    artifact.metadata["input_generation_id"],
                    context.generation_id,
                )
                self.assertEqual(
                    artifact.metadata[
                        "input_generation_manifest_sha256"
                    ],
                    context.manifest_sha256,
                )


if __name__ == "__main__":
    unittest.main()
