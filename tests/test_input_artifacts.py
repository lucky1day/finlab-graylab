from __future__ import annotations

import inspect
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd


class InputArtifactTests(unittest.TestCase):
    def test_input_artifacts_does_not_reexport_data_service(self) -> None:
        import shared.input_artifacts as input_artifacts

        self.assertFalse(hasattr(input_artifacts, "data_service"))

    def test_input_artifact_path_sanitizes_scheme_and_predict_date(self) -> None:
        from shared.input_artifacts import input_artifact_path

        with tempfile.TemporaryDirectory() as tmpdir:
            path = input_artifact_path(
                scheme_id="daily/scheme:bad",
                frequency="daily",
                predict_date="2026/06/05",
                output_root=Path(tmpdir),
            )

        self.assertEqual(path.name, "daily_output_2026-06-05.csv")
        self.assertEqual(path.parent.name, "daily_scheme_bad")

    def test_input_artifact_path_prefix_mapping_per_frequency(self) -> None:
        from shared.input_artifacts import input_artifact_path

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            daily = input_artifact_path(
                scheme_id="demo",
                frequency="daily",
                predict_date="2026-06-05",
                output_root=root,
            )
            weekly = input_artifact_path(
                scheme_id="demo",
                frequency="weekly",
                predict_date="2026-06-05",
                output_root=root,
            )
            monthly = input_artifact_path(
                scheme_id="demo",
                frequency="monthly",
                predict_date="2026-06-05",
                output_root=root,
            )

        self.assertEqual(daily.name, "daily_output_2026-06-05.csv")
        self.assertEqual(weekly.name, "weekly_output_2026-06-05.csv")
        self.assertEqual(monthly.name, "monthly_output_2026-06-05.csv")
        with self.assertRaises(ValueError):
            input_artifact_path(
                scheme_id="demo",
                frequency="quarterly",
                predict_date="2026-06-05",
                output_root=Path("/tmp"),
            )

    def test_scheduled_live_source_artifact_path_uses_frozen_fence(
        self,
    ) -> None:
        from shared import input_artifacts

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(
                os.environ,
                {
                    input_artifacts.SCHEDULE_EXECUTION_TOKEN_ENV:
                        "compat-attempt",
                    input_artifacts.LIVE_SOURCE_FENCE_GENERATION_ID_ENV:
                        "native-20260724",
                },
                clear=True,
            ),
        ):
            path = input_artifacts.input_artifact_path(
                scheme_id="daily_1y_xgb_1y13_0629",
                frequency="daily",
                predict_date="2026-07-24",
                output_root=Path(tmpdir),
            )

        self.assertEqual(path.parent.name, "compat-attempt")
        self.assertEqual(path.parent.parent.name, "native-20260724")
        self.assertEqual(path.parent.parent.parent.name, "_scheduled")

    def test_daily_input_artifact_delegates_to_unified_data_service_file(self) -> None:
        from shared.input_artifacts import build_daily_input_artifact

        daily_df = pd.DataFrame({"date": pd.to_datetime(["2026-06-05"]), "TB0YWI0C": [2.1]})
        engine = object()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("shared.input_artifacts._data_service") as daily_service:
                daily_service.build_daily_output_from_db.return_value = daily_df
                daily_service.save_daily_output.side_effect = lambda df, path: df.to_csv(path, index=False)
                artifact = build_daily_input_artifact(
                    scheme_id="t1_daily",
                    predict_date="2026-06-05",
                    start_date="2019-06-10",
                    end_date="2026-06-05",
                    engine=engine,
                    output_root=root,
                )
                artifact_exists = artifact.path.exists()
                expected_content_hash = hashlib.sha256(artifact.path.read_bytes()).hexdigest()

        self.assertEqual(artifact.scheme_id, "t1_daily")
        self.assertEqual(artifact.frequency, "daily")
        self.assertEqual(artifact.source, "shared_data_service_daily")
        self.assertEqual(artifact.data_version, "shared_data_service_daily.v1")
        self.assertEqual(artifact.content_hash, expected_content_hash)
        self.assertEqual(artifact.source_watermark, "2026-06-05")
        self.assertEqual(len(artifact.schema_hash), 64)
        self.assertEqual(len(artifact.artifact_id), 64)
        self.assertEqual(artifact.row_count, 1)
        self.assertEqual(artifact.column_count, 2)
        self.assertEqual(artifact.columns, ["date", "TB0YWI0C"])
        self.assertEqual(
            artifact.date_coverage,
            {"field": "date", "start": "2026-06-05", "end": "2026-06-05"},
        )
        self.assertEqual(
            artifact.quality_flags,
            {
                "missing_required_columns": [],
                "empty_frame": False,
                "null_coverage_rows": 0,
                "duplicate_coverage_values": 0,
            },
        )
        self.assertTrue(str(artifact.path).endswith("t1_daily/daily_output_2026-06-05.csv"))
        self.assertEqual(artifact.dataframe["date"].dt.strftime("%Y-%m-%d").tolist(), ["2026-06-05"])
        self.assertEqual(artifact.dataframe["TB0YWI0C"].tolist(), [2.1])
        kwargs = daily_service.build_daily_output_from_db.call_args.kwargs
        self.assertEqual(kwargs["start_date"], "2019-06-10")
        self.assertEqual(kwargs["end_date"], "2026-06-05")
        self.assertIs(kwargs["engine"], engine)
        self.assertIs(daily_service.save_daily_output.call_args.args[0], daily_df)
        saved_path = Path(
            daily_service.save_daily_output.call_args.args[1]
        )
        self.assertEqual(saved_path.parent, artifact.path.parent)
        self.assertTrue(saved_path.name.startswith(f".{artifact.path.name}."))
        self.assertEqual(saved_path.suffix, ".tmp")
        self.assertTrue(artifact_exists)

    def test_weekly_input_artifact_delegates_to_unified_data_service_file(self) -> None:
        from shared.input_artifacts import build_weekly_input_artifact

        weekly_df = pd.DataFrame({"week_id": [202621], "TB0YWI3C": [1.7]})
        engine = object()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("shared.input_artifacts._data_service") as data_service:
                data_service.build_weekly_output_from_db.return_value = weekly_df
                data_service.save_weekly_output.side_effect = lambda df, path: df.to_csv(path, index=False)
                artifact = build_weekly_input_artifact(
                    scheme_id="demo_weekly_scheme",
                    predict_date="2026-06-06",
                    end_week=202621,
                    as_of_date="2026-05-29",
                    engine=engine,
                    output_root=root,
                )
                self.assertTrue(artifact.path.exists())
                expected_content_hash = hashlib.sha256(artifact.path.read_bytes()).hexdigest()

        self.assertEqual(artifact.scheme_id, "demo_weekly_scheme")
        self.assertEqual(artifact.frequency, "weekly")
        self.assertEqual(artifact.source, "shared_data_service_weekly")
        self.assertEqual(artifact.data_version, "shared_data_service_weekly.v1")
        self.assertEqual(artifact.content_hash, expected_content_hash)
        self.assertEqual(artifact.source_watermark, "202621")
        self.assertEqual(len(artifact.schema_hash), 64)
        self.assertEqual(len(artifact.artifact_id), 64)
        self.assertEqual(artifact.row_count, 1)
        self.assertEqual(artifact.column_count, 2)
        self.assertEqual(artifact.columns, ["week_id", "TB0YWI3C"])
        self.assertEqual(
            artifact.date_coverage,
            {"field": "week_id", "start": 202621, "end": 202621},
        )
        self.assertEqual(
            artifact.quality_flags,
            {
                "missing_required_columns": [],
                "empty_frame": False,
                "null_coverage_rows": 0,
                "duplicate_coverage_values": 0,
            },
        )
        self.assertTrue(str(artifact.path).endswith("demo_weekly_scheme/weekly_output_2026-06-06.csv"))
        self.assertEqual(artifact.dataframe["week_id"].tolist(), [202621])
        self.assertEqual(artifact.dataframe["TB0YWI3C"].tolist(), [1.7])
        kwargs = data_service.build_weekly_output_from_db.call_args.kwargs
        self.assertEqual(kwargs["end_week"], 202621)
        self.assertEqual(kwargs["as_of_date"], "2026-05-29")
        self.assertIs(kwargs["engine"], engine)
        self.assertIs(data_service.save_weekly_output.call_args.args[0], weekly_df)
        saved_path = Path(
            data_service.save_weekly_output.call_args.args[1]
        )
        self.assertEqual(saved_path.parent, artifact.path.parent)
        self.assertTrue(saved_path.name.startswith(f".{artifact.path.name}."))
        self.assertEqual(saved_path.suffix, ".tmp")

    def test_weekly_output_filters_raw_rows_by_as_of_date_before_dedup(self) -> None:
        from shared.data_service import build_weekly_output_from_frames

        raw = pd.DataFrame(
            {
                "rdate": ["2026-05-29", "2026-06-02", "2026-05-22"],
                "week_id": [202621, 202621, 202620],
                "indicators_code": ["TB0YWI3C", "TB0YWI3C", "TB0YWI3C"],
                "indicators_value": [1.1, 9.9, 1.0],
            }
        )

        result = build_weekly_output_from_frames(
            ["week_id", "TB0YWI3C"],
            raw,
            as_of_date="2026-05-29",
        )

        self.assertEqual(result["week_id"].tolist(), [202620, 202621])
        self.assertEqual(result["TB0YWI3C"].tolist(), [1.0, 1.1])

    def test_weekly_output_as_of_handles_mixed_rdate_formats(self) -> None:
        from shared.data_service import build_weekly_output_from_frames

        raw = pd.DataFrame(
            {
                "rdate": ["2025-01-02", "2025/1/3", "2025/1/10"],
                "week_id": [202501, 202501, 202502],
                "indicators_code": ["N0000001", "TB0YWI3C", "TB0YWI3C"],
                "indicators_value": [8.8, 1.6, 9.9],
            }
        )

        result = build_weekly_output_from_frames(
            ["week_id", "N0000001", "TB0YWI3C"],
            raw,
            start_week=202501,
            end_week=202502,
            as_of_date="2025-01-03",
        )

        self.assertEqual(result["week_id"].tolist(), [202501])
        self.assertEqual(result["N0000001"].tolist(), [8.8])
        self.assertEqual(result["TB0YWI3C"].tolist(), [1.6])

    def test_monthly_input_artifact_delegates_to_unified_data_service_file(self) -> None:
        from shared.input_artifacts import build_monthly_input_artifact

        monthly_df = pd.DataFrame(
            {
                "month_id": ["202504", "202505", "bad", "2025", "202506.0"],
                "M0000001": ["1.1", "1.2", "9.9", "8.8", "1.3"],
            }
        )
        engine = object()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("shared.input_artifacts._data_service") as data_service:
                data_service.build_monthly_output_from_db.return_value = monthly_df
                data_service.save_monthly_output.side_effect = lambda df, path: df.to_csv(path, index=False)
                artifact = build_monthly_input_artifact(
                    scheme_id="demo_monthly_scheme",
                    predict_date="2026-06-05",
                    start_date="2019-06-10",
                    end_date="2026-06-05",
                    engine=engine,
                    output_root=root,
                )
                self.assertTrue(artifact.path.exists())
                expected_content_hash = hashlib.sha256(artifact.path.read_bytes()).hexdigest()

        self.assertEqual(artifact.scheme_id, "demo_monthly_scheme")
        self.assertEqual(artifact.frequency, "monthly")
        self.assertEqual(artifact.source, "shared_data_service_monthly")
        self.assertEqual(artifact.data_version, "shared_data_service_monthly.v1")
        self.assertEqual(artifact.content_hash, expected_content_hash)
        self.assertEqual(artifact.source_watermark, "202506")
        self.assertEqual(len(artifact.schema_hash), 64)
        self.assertEqual(len(artifact.artifact_id), 64)
        self.assertEqual(artifact.row_count, 3)
        self.assertEqual(artifact.column_count, 2)
        self.assertEqual(artifact.columns, ["month_id", "M0000001"])
        self.assertEqual(
            artifact.date_coverage,
            {"field": "month_id", "start": "202504", "end": "202506"},
        )
        self.assertEqual(
            artifact.quality_flags,
            {
                "missing_required_columns": [],
                "empty_frame": False,
                "null_coverage_rows": 0,
                "duplicate_coverage_values": 0,
            },
        )
        self.assertTrue(str(artifact.path).endswith("demo_monthly_scheme/monthly_output_2026-06-05.csv"))
        self.assertEqual(artifact.dataframe["month_id"].tolist(), ["202504", "202505", "202506"])
        self.assertEqual(artifact.dataframe["M0000001"].tolist(), [1.1, 1.2, 1.3])
        kwargs = data_service.build_monthly_output_from_db.call_args.kwargs
        self.assertEqual(kwargs["start_date"], "2019-06-10")
        self.assertEqual(kwargs["end_date"], "2026-06-05")
        self.assertIs(kwargs["engine"], engine)
        self.assertIs(data_service.save_monthly_output.call_args.args[0], monthly_df)
        saved_path = Path(
            data_service.save_monthly_output.call_args.args[1]
        )
        self.assertEqual(saved_path.parent, artifact.path.parent)
        self.assertTrue(saved_path.name.startswith(f".{artifact.path.name}."))
        self.assertEqual(saved_path.suffix, ".tmp")

    def test_weekly_input_artifact_signature_removes_legacy_flags(self) -> None:
        from shared.input_artifacts import build_weekly_input_artifact

        signature = inspect.signature(build_weekly_input_artifact)
        self.assertNotIn("end_date", signature.parameters)
        self.assertNotIn("include_daily_weekly_close_fallback", signature.parameters)

    def test_content_hash_is_stable_for_same_csv_and_changes_when_data_changes(self) -> None:
        from shared.input_artifacts import build_daily_input_artifact

        first_df = pd.DataFrame({"date": pd.to_datetime(["2026-06-05"]), "TB0YWI0C": [2.1]})
        changed_df = pd.DataFrame({"date": pd.to_datetime(["2026-06-05"]), "TB0YWI0C": [2.2]})

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("shared.input_artifacts._data_service") as daily_service:
                daily_service.save_daily_output.side_effect = lambda df, path: df.to_csv(path, index=False)
                daily_service.build_daily_output_from_db.return_value = first_df
                first = build_daily_input_artifact(
                    scheme_id="t1_daily",
                    predict_date="2026-06-05",
                    start_date="2019-06-10",
                    end_date="2026-06-05",
                    output_root=root,
                )
                second = build_daily_input_artifact(
                    scheme_id="t1_daily",
                    predict_date="2026-06-05",
                    start_date="2019-06-10",
                    end_date="2026-06-05",
                    output_root=root,
                )
                daily_service.build_daily_output_from_db.return_value = changed_df
                changed = build_daily_input_artifact(
                    scheme_id="t1_daily",
                    predict_date="2026-06-05",
                    start_date="2019-06-10",
                    end_date="2026-06-05",
                    output_root=root,
                )

        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.schema_hash, second.schema_hash)
        self.assertEqual(first.artifact_id, second.artifact_id)
        self.assertNotEqual(first.content_hash, changed.content_hash)
        self.assertNotEqual(first.artifact_id, changed.artifact_id)


if __name__ == "__main__":
    unittest.main()
