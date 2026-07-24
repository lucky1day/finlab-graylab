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


LIWEI_SCHEMES = (
    (
        "liwei_0616_10y01_cons_say_k3_div_k10",
        "run_10y01_for_feature_date",
        "10Y",
    ),
    (
        "liwei_0616_10y01_full_oos_k3_div_k10",
        "run_for_feature_date",
        "10Y",
    ),
    (
        "liwei_0616_10y02_cons_say_k3_div_k5",
        "run_10y02_for_feature_date",
        "10Y",
    ),
    (
        "liwei_0616_5y01_full_oos_k3_div_k10",
        "run_for_feature_date",
        "5Y",
    ),
    (
        "liwei_0616_5y_auc_static_all_k3_div_k10",
        "run_for_feature_date",
        "5Y",
    ),
    (
        "liwei_0616_5y_auc_yearly_all_k3_div_k10",
        "run_for_feature_date",
        "5Y",
    ),
    (
        "liwei_0616_5y_ic_yearly_all_k3_div_k10",
        "run_for_feature_date",
        "5Y",
    ),
    (
        "liwei_0616_7y01_cons_say_k3_div_k10",
        "run_7y01_for_feature_date",
        "7Y",
    ),
    (
        "liwei_0616_7y03_cons_all_k3_div_k8",
        "run_7y03_for_feature_date",
        "7Y",
    ),
    (
        "liwei_0616_cons_sda_k3_div_k10",
        "run_5y01_for_feature_date",
        "5Y",
    ),
)


class LiweiNativeGenerationMatrixTests(unittest.TestCase):
    def test_all_liwei_daily_schemes_consume_one_frozen_generation(
        self,
    ) -> None:
        for scheme_id, inference_name, target_tenor in LIWEI_SCHEMES:
            with self.subTest(scheme_id=scheme_id):
                self._assert_scheme_uses_frozen_generation(
                    scheme_id=scheme_id,
                    inference_name=inference_name,
                    target_tenor=target_tenor,
                )

    def _assert_scheme_uses_frozen_generation(
        self,
        *,
        scheme_id: str,
        inference_name: str,
        target_tenor: str,
    ) -> None:
        from shared import input_artifacts

        predict = importlib.import_module(f"schemes.{scheme_id}.predict")
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

        native_binding = {
            "generation_id": context.generation_id,
            "manifest_sha256": context.manifest_sha256,
            "dataset_content_id": context.dataset_content_id,
            "business_date": context.business_date,
            "feature_date": context.feature_date,
            "schema_version": context.schema_version,
            "exporter_version": context.exporter_version,
        }
        result = {
            "prediction": -1,
            "confidence": 0.4,
            "vote_score": -0.72,
            "baseline_signs": {"STD": -1},
            "baseline_scores": {"STD": -0.35},
            "true_label": None,
            "source_model_version": "source-test",
            "model_version": f"{scheme_id}-frozen-test",
            "phase_a_cache_audit": {
                "status": "hit",
                "native_generation": native_binding,
            },
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
                    inference_name,
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
        self.assertEqual(record.scheme_id, scheme_id)
        self.assertEqual(record.target_tenor, target_tenor)
        self.assertEqual(record.feature_date, "2026-07-23")
        self.assertEqual(record.target_date, "2026-07-30")
        self.assertEqual(record.predicted_direction, -1)
        self.assertEqual(record.extra["vote_score"], -0.72)
        self.assertEqual(
            record.extra["phase_a_cache"]["native_generation"],
            native_binding,
        )
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
            self.assertTrue(kwargs["use_incremental_cache"])

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
