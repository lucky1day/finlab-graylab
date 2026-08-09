from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd


def _generation_context():
    from shared.native_input_generation import NativeGenerationContext

    metadata = pd.DataFrame(
        [
            {
                "indicators_code": "TB1YWI0C",
                "frequency": "daily",
                "status": 1,
                "pre_forecast_flag": 1,
                "lag_length": 0,
                "indicators_source": "raw",
            },
            {
                "indicators_code": "WEEKLY_A",
                "frequency": "weekly",
                "status": 1,
                "pre_forecast_flag": 1,
                "lag_length": 0,
                "indicators_source": "raw",
            },
            {
                "indicators_code": "MONTHLY_A",
                "frequency": "monthly",
                "status": 1,
                "pre_forecast_flag": 1,
                "lag_length": 0,
                "indicators_source": "raw",
            },
        ]
    )
    frames = {
        "metadata.csv": metadata,
        "api_wind_daily.csv": pd.DataFrame(
            [
                {
                    "rdate": "2026-07-22",
                    "indicators_code": "TB1YWI0C",
                    "indicators_value": 1.1,
                },
                {
                    "rdate": "2026-07-23",
                    "indicators_code": "TB1YWI0C",
                    "indicators_value": 1.2,
                },
            ]
        ),
        "api_wind_derivative_daily.csv": pd.DataFrame(
            columns=["rdate", "indicators_code", "indicators_value"]
        ),
        "api_wind_weekly.csv": pd.DataFrame(
            [
                {
                    "rdate": "2026-07-17",
                    "week_id": "202628",
                    "indicators_code": "WEEKLY_A",
                    "indicators_value": 2.1,
                },
                {
                    "rdate": "2026-07-23",
                    "week_id": "202629",
                    "indicators_code": "WEEKLY_A",
                    "indicators_value": 2.2,
                },
            ]
        ),
        "api_wind_derivative_weekly.csv": pd.DataFrame(
            columns=["rdate", "week_id", "indicators_code", "indicators_value"]
        ),
        "api_wind_monthly.csv": pd.DataFrame(
            [
                {
                    "rdate": "2026-06-15",
                    "indicators_code": "MONTHLY_A",
                    "indicators_value": 3.1,
                },
                {
                    "rdate": "2026-07-15",
                    "indicators_code": "MONTHLY_A",
                    "indicators_value": 3.2,
                },
            ]
        ),
        "api_wind_derivative_monthly.csv": pd.DataFrame(
            columns=[
                "rdate",
                "month_id",
                "indicators_code",
                "indicators_value",
            ]
        ),
        "api_wind_date.csv": pd.DataFrame(
            [{"rdate": "2026-07-23", "week_id": "202629"}]
        ),
        "t_trade_calendar.csv": pd.DataFrame(
            [{"rdate": "2026-07-23", "trade_flag": "1"}]
        ),
        "weekly_cutoff_index.csv": pd.DataFrame(
            [{"week_id": "202629", "available_date": "2026-07-23"}]
        ),
        "monthly_cutoff_index.csv": pd.DataFrame(
            [{"month_id": "202607", "available_date": "2026-07-15"}]
        ),
    }
    return NativeGenerationContext(
        generation_id="native-0123456789abcdef01234567",
        generation_type="native_source",
        dataset_content_id="d" * 64,
        root_dir=Path("/tmp/native-generation"),
        manifest_path=Path("/tmp/native-generation/manifest.json"),
        manifest_sha256="a" * 64,
        business_date="2026-07-24",
        feature_date="2026-07-23",
        source_commit_token="s" * 64,
        readiness_basis="CLOCK_CONTRACT",
        schema_version="native-generation-v1",
        exporter_version="native-generation-exporter-v1",
        created_at="2026-07-23T22:30:00.000000Z",
        sealed_at="2026-07-23T22:31:00.000000Z",
        _cutoffs_json=(
            '{"daily_cutoff_key":"2026-07-23",'
            '"monthly_cutoff_key":"202607",'
            '"weekly_cutoff_key":"202629"}'
        ),
        _manifest_json="{}",
        _frames=frames,
    )


class NativeGenerationEnvironmentTests(unittest.TestCase):
    def test_create_input_engine_opens_exact_generation_from_complete_environment(
        self,
    ) -> None:
        from shared import input_artifacts

        context = _generation_context()
        environment = {
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
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(
                input_artifacts,
                "open_native_generation",
                return_value=context,
            ) as opener,
            patch.object(
                input_artifacts._data_service,
                "create_sqlalchemy_engine",
            ) as db_factory,
        ):
            actual = input_artifacts.create_input_engine()

        self.assertIs(actual, context)
        db_factory.assert_not_called()
        opener.assert_called_once_with(
            context.manifest_path,
            expected_generation_id=context.generation_id,
            expected_manifest_sha256=context.manifest_sha256,
            expected_business_date=context.business_date,
            expected_feature_date=context.feature_date,
        )

    def test_partial_generation_environment_fails_closed_without_db_fallback(
        self,
    ) -> None:
        from shared import input_artifacts

        environment = {
            input_artifacts.NATIVE_MANIFEST_PATH_ENV:
                "/tmp/native/manifest.json",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(
                input_artifacts._data_service,
                "create_sqlalchemy_engine",
            ) as db_factory,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "partial Native input generation environment",
            ):
                input_artifacts.create_input_engine()

        db_factory.assert_not_called()

    def test_no_generation_environment_preserves_explicit_legacy_db_mode(
        self,
    ) -> None:
        from shared import input_artifacts

        sentinel = object()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                input_artifacts._data_service,
                "create_sqlalchemy_engine",
                return_value=sentinel,
            ) as db_factory,
        ):
            actual = input_artifacts.create_input_engine()

        self.assertIs(actual, sentinel)
        db_factory.assert_called_once_with()


class NativeGenerationArtifactBuilderTests(unittest.TestCase):
    def test_ephemeral_input_root_overrides_persistent_output_root(self) -> None:
        from shared import input_artifacts

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            with patch.dict(
                os.environ,
                {input_artifacts.EPHEMERAL_NATIVE_INPUT_ROOT_ENV: str(root)},
                clear=True,
            ):
                path = input_artifacts.input_artifact_path(
                    scheme_id="daily_demo",
                    frequency="daily",
                    predict_date="2026-07-24",
                    output_root=Path("/persistent/inputs"),
                )

        self.assertEqual(
            path,
            root / "daily_demo" / "daily_output_2026-07-24.csv",
        )

        with patch.dict(
            os.environ,
            {input_artifacts.EPHEMERAL_NATIVE_INPUT_ROOT_ENV: "relative"},
            clear=True,
        ), self.assertRaisesRegex(ValueError, "absolute"):
            input_artifacts.input_artifact_path(
                scheme_id="daily_demo",
                frequency="daily",
                predict_date="2026-07-24",
            )

    def test_scheduled_attempt_uses_generation_and_attempt_scoped_path(
        self,
    ) -> None:
        from shared import input_artifacts

        context = _generation_context()
        environment = {
            input_artifacts.NATIVE_GENERATION_ID_ENV:
                context.generation_id,
            input_artifacts.SCHEDULE_EXECUTION_TOKEN_ENV:
                "attempt-1",
        }
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, environment, clear=True),
        ):
            path = input_artifacts.input_artifact_path(
                scheme_id="daily_demo",
                frequency="daily",
                predict_date="2026-07-24",
                output_root=Path(tmpdir),
            )

        self.assertEqual(
            path.parts[-5:],
            (
                "daily_demo",
                "_scheduled",
                context.generation_id,
                "attempt-1",
                "daily_output_2026-07-24.csv",
            ),
        )

    def test_atomic_artifact_failure_preserves_previous_complete_file(
        self,
    ) -> None:
        from shared import input_artifacts

        frame = pd.DataFrame(
            [{"date": "2026-07-23", "TB1YWI0C": 1.2}]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            final_path = input_artifacts.input_artifact_path(
                scheme_id="daily_demo",
                frequency="daily",
                predict_date="2026-07-24",
                output_root=root,
            )
            final_path.parent.mkdir(parents=True)
            final_path.write_bytes(b"previous-complete\n")

            def fail_after_partial_write(_frame, path) -> None:
                Path(path).write_bytes(b"partial\n")
                raise OSError("disk full")

            with (
                patch.object(
                    input_artifacts._data_service,
                    "build_daily_output_from_db",
                    return_value=frame,
                ),
                patch.object(
                    input_artifacts._data_service,
                    "save_daily_output",
                    side_effect=fail_after_partial_write,
                ),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                input_artifacts.build_daily_input_artifact(
                    scheme_id="daily_demo",
                    predict_date="2026-07-24",
                    start_date="2026-07-23",
                    end_date="2026-07-23",
                    engine=object(),
                    output_root=root,
                )

            self.assertEqual(
                final_path.read_bytes(),
                b"previous-complete\n",
            )
            self.assertEqual(
                list(final_path.parent.glob(f".{final_path.name}.*.tmp")),
                [],
            )

    def test_all_frequency_builders_use_frozen_frames_and_embed_provenance(
        self,
    ) -> None:
        from shared.input_artifacts import (
            build_daily_input_artifact,
            build_monthly_input_artifact,
            build_weekly_input_artifact,
        )

        context = _generation_context()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with (
                patch(
                    "shared.input_artifacts._data_service."
                    "build_daily_output_from_db",
                ) as daily_db,
                patch(
                    "shared.input_artifacts._data_service."
                    "build_weekly_output_from_db",
                ) as weekly_db,
                patch(
                    "shared.input_artifacts._data_service."
                    "build_monthly_output_from_db",
                ) as monthly_db,
            ):
                daily = build_daily_input_artifact(
                    scheme_id="daily_demo",
                    predict_date="2026-07-24",
                    start_date="2026-07-22",
                    end_date="2026-07-23",
                    engine=context,
                    output_root=root,
                )
                weekly = build_weekly_input_artifact(
                    scheme_id="weekly_demo",
                    predict_date="2026-07-24",
                    start_week=202628,
                    end_week=202629,
                    as_of_date="2026-07-23",
                    engine=context,
                    output_root=root,
                )
                monthly = build_monthly_input_artifact(
                    scheme_id="monthly_demo",
                    predict_date="2026-07-24",
                    start_date="2026-06-01",
                    end_date="2026-07-23",
                    engine=context,
                    output_root=root,
                )

        daily_db.assert_not_called()
        weekly_db.assert_not_called()
        monthly_db.assert_not_called()
        self.assertEqual(
            daily.dataframe["date"].dt.strftime("%Y-%m-%d").tolist(),
            ["2026-07-22", "2026-07-23"],
        )
        self.assertEqual(weekly.dataframe["week_id"].tolist(), [202628, 202629])
        self.assertEqual(
            monthly.dataframe["month_id"].tolist(),
            ["202606", "202607"],
        )
        for artifact in (daily, weekly, monthly):
            self.assertEqual(
                artifact.metadata["input_generation_id"],
                context.generation_id,
            )
            self.assertEqual(
                artifact.metadata["input_generation_manifest_sha256"],
                context.manifest_sha256,
            )
            self.assertEqual(
                artifact.metadata["input_generation_dataset_content_id"],
                context.dataset_content_id,
            )
            self.assertEqual(
                artifact.metadata["input_generation_feature_date"],
                context.feature_date,
            )

    def test_builder_rejects_requested_cutoff_after_frozen_feature_date(
        self,
    ) -> None:
        from shared.input_artifacts import (
            build_daily_input_artifact,
            build_monthly_input_artifact,
            build_weekly_input_artifact,
        )

        context = _generation_context()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            calls = [
                lambda: build_daily_input_artifact(
                    scheme_id="daily_demo",
                    predict_date="2026-07-24",
                    start_date="2026-07-22",
                    end_date="2026-07-24",
                    engine=context,
                    output_root=root,
                ),
                lambda: build_weekly_input_artifact(
                    scheme_id="weekly_demo",
                    predict_date="2026-07-24",
                    as_of_date="2026-07-24",
                    engine=context,
                    output_root=root,
                ),
                lambda: build_monthly_input_artifact(
                    scheme_id="monthly_demo",
                    predict_date="2026-07-24",
                    start_date="2026-06-01",
                    end_date="2026-07-24",
                    engine=context,
                    output_root=root,
                ),
            ]
            for call in calls:
                with self.subTest(call=call):
                    with self.assertRaisesRegex(
                        ValueError,
                        "after frozen feature_date",
                    ):
                        call()


if __name__ == "__main__":
    unittest.main()
