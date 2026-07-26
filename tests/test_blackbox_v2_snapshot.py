from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

import pandas as pd


class BlackboxV2SnapshotTests(unittest.TestCase):
    def test_snapshot_filename_contract_remains_exactly_three_business_files(
        self,
    ) -> None:
        from shared.blackbox_v2.snapshot import SNAPSHOT_FILENAMES

        self.assertEqual(
            SNAPSHOT_FILENAMES,
            (
                "daily_output.csv",
                "weekly_output.csv",
                "monthly_output.csv",
            ),
        )

    def test_snapshot_is_content_addressed_and_uses_fixed_filenames(self) -> None:
        from shared.blackbox_v2.snapshot import create_snapshot_from_frames

        frames = _frames()
        expected = {name: list(frame.columns) for name, frame in frames.items()}
        with tempfile.TemporaryDirectory() as tmpdir:
            first = create_snapshot_from_frames(
                frames,
                output_root=Path(tmpdir),
                expected_columns=expected,
                schema_version="data-bridge-v1",
            )
            second = create_snapshot_from_frames(
                frames,
                output_root=Path(tmpdir),
                expected_columns=expected,
                schema_version="data-bridge-v1",
            )

            manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(first.snapshot_id, second.snapshot_id)
            self.assertEqual(
                sorted(path.name for path in first.data_dir.iterdir()),
                ["daily_output.csv", "monthly_output.csv", "weekly_output.csv"],
            )
            self.assertEqual(manifest["data_snapshot_id"], first.snapshot_id)
            self.assertEqual(manifest["files"]["daily_output.csv"]["row_count"], 3)
            self.assertFalse(first.data_dir.stat().st_mode & stat.S_IWUSR)
            self.assertTrue(
                all(not path.stat().st_mode & stat.S_IWUSR for path in first.data_dir.iterdir())
            )

    def test_snapshot_rejects_schema_that_does_not_start_with_time_key(self) -> None:
        from shared.blackbox_v2.snapshot import create_snapshot_from_frames

        frames = _frames()
        expected = {name: list(frame.columns) for name, frame in frames.items()}
        expected["weekly_output.csv"] = ["week_factor", "week_id"]
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "baseline must start with week_id"):
                create_snapshot_from_frames(
                    frames,
                    output_root=Path(tmpdir),
                    expected_columns=expected,
                    schema_version="data-bridge-v1",
                )

    def test_snapshot_accepts_added_columns_and_tracks_them_in_identity(self) -> None:
        from shared.blackbox_v2.snapshot import create_snapshot_from_frames

        baseline_frames = _frames()
        baseline_columns = {
            name: list(frame.columns)
            for name, frame in baseline_frames.items()
        }
        added_frames = {
            name: frame.assign(new_factor=range(1, len(frame) + 1))
            for name, frame in baseline_frames.items()
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir)
            baseline = create_snapshot_from_frames(
                baseline_frames,
                output_root=output_root,
                expected_columns=baseline_columns,
                schema_version="data-bridge-v1",
            )
            added = create_snapshot_from_frames(
                added_frames,
                output_root=output_root,
                expected_columns=baseline_columns,
                schema_version="data-bridge-v1",
            )
            manifest = json.loads(added.manifest_path.read_text(encoding="utf-8"))

        self.assertNotEqual(baseline.snapshot_id, added.snapshot_id)
        self.assertEqual(
            manifest["files"]["daily_output.csv"]["columns"],
            ["date", "daily_factor", "new_factor"],
        )

    def test_snapshot_rejects_duplicate_or_unsorted_time_keys(self) -> None:
        from shared.blackbox_v2.snapshot import create_snapshot_from_frames

        frames = _frames()
        expected = {name: list(frame.columns) for name, frame in frames.items()}
        frames["weekly_output.csv"]["week_id"] = ["202628", "202628"]
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "week_id must be unique"):
                create_snapshot_from_frames(
                    frames,
                    output_root=Path(tmpdir),
                    expected_columns=expected,
                    schema_version="data-bridge-v1",
                )

        frames = _frames()
        frames["monthly_output.csv"] = frames["monthly_output.csv"].iloc[::-1].reset_index(drop=True)
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "month_id must be strictly ascending"):
                create_snapshot_from_frames(
                    frames,
                    output_root=Path(tmpdir),
                    expected_columns=expected,
                    schema_version="data-bridge-v1",
                )

    def test_snapshot_rejects_invalid_keys_and_non_numeric_factor_values(self) -> None:
        from shared.blackbox_v2.snapshot import create_snapshot_from_frames

        frames = _frames()
        expected = {name: list(frame.columns) for name, frame in frames.items()}
        frames["daily_output.csv"].loc[1, "date"] = "not-a-date"
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "date must use YYYY-MM-DD"):
                create_snapshot_from_frames(
                    frames,
                    output_root=Path(tmpdir),
                    expected_columns=expected,
                    schema_version="data-bridge-v1",
                )

        frames = _frames()
        frames["monthly_output.csv"]["month_factor"] = frames["monthly_output.csv"]["month_factor"].astype(object)
        frames["monthly_output.csv"].loc[1, "month_factor"] = "not-a-number"
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "month_factor must contain only numeric or null values"):
                create_snapshot_from_frames(
                    frames,
                    output_root=Path(tmpdir),
                    expected_columns=expected,
                    schema_version="data-bridge-v1",
                )

    def test_resolves_all_cutoffs_without_iso_or_month_inference(self) -> None:
        from shared.blackbox_v2.snapshot import create_snapshot_from_frames, resolve_cutoffs

        frames = _frames()
        expected = {name: list(frame.columns) for name, frame in frames.items()}
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=Path(tmpdir),
                expected_columns=expected,
                schema_version="data-bridge-v1",
            )

            cutoffs = resolve_cutoffs(
                snapshot,
                "2026-07-15",
                weekly_as_of=pd.DataFrame({"week_id": ["202627"]}),
                monthly_as_of=pd.DataFrame({"month_id": ["202606"]}),
            )

        self.assertEqual(cutoffs.daily_cutoff_key, "2026-07-15")
        self.assertEqual(cutoffs.weekly_cutoff_key, "202627")
        self.assertEqual(cutoffs.monthly_cutoff_key, "202606")

    def test_snapshot_accepts_databridge_timestamp_date_format(self) -> None:
        from shared.blackbox_v2.snapshot import create_snapshot_from_frames

        frames = _frames()
        frames["daily_output.csv"]["date"] = [
            "2026/07/14 00:00",
            "2026/07/15 00:00",
            "2026/07/16 00:00",
        ]
        expected = {name: list(frame.columns) for name, frame in frames.items()}
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=Path(tmpdir),
                expected_columns=expected,
                schema_version="data-bridge-v1",
            )
            self.assertTrue(snapshot.data_dir.is_dir())

    def test_bundle_without_platform_inputs_preserves_parent_identity(self) -> None:
        from shared.blackbox_v2.snapshot import (
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )

        frames = _frames()
        expected = {name: list(frame.columns) for name, frame in frames.items()}
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=Path(tmpdir),
                expected_columns=expected,
                schema_version="data-bridge-v1",
            )
            bundle = compose_blackbox_input_bundle(snapshot)

        self.assertEqual(bundle.combined_snapshot_id, snapshot.snapshot_id)
        self.assertEqual(bundle.parent_snapshot_id, snapshot.snapshot_id)
        self.assertIs(bundle.base_snapshot, snapshot)
        self.assertEqual(bundle.platform_input_ids, ())
        self.assertEqual(bundle.platform_input_artifacts, ())
        self.assertEqual(bundle.expected_filenames, tuple(_frames()))

    def test_bundle_identity_is_stable_across_source_provenance(self) -> None:
        from shared.blackbox_v2.platform_inputs import freeze_platform_input
        from shared.blackbox_v2.snapshot import (
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )

        frames = _frames()
        expected = {name: list(frame.columns) for name, frame in frames.items()}
        calendar = pd.DataFrame(
            {
                "rdate": ["2026-07-14", "2026-07-15"],
                "week_id": [202628, 202628],
            }
        )
        harness_artifact = freeze_platform_input(
            "api-wind-date-v1",
            calendar,
            weekly_cutoff_key="202628",
            audit_provenance={
                "source_kind": "harness_database",
                "captured_at": "2026-07-26T00:00:00Z",
                "generation_id": None,
                "temp_path": "/tmp/harness-a",
            },
        )
        native_artifact = freeze_platform_input(
            "api-wind-date-v1",
            calendar,
            weekly_cutoff_key="202628",
            audit_provenance={
                "source_kind": "scheduled_native_generation",
                "captured_at": "2026-07-27T00:00:00Z",
                "generation_id": "native-1",
                "temp_path": "/tmp/native-b",
            },
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=Path(tmpdir),
                expected_columns=expected,
                schema_version="data-bridge-v1",
            )
            harness_bundle = compose_blackbox_input_bundle(
                snapshot,
                platform_input_ids=("api-wind-date-v1",),
                platform_input_artifacts=(harness_artifact,),
            )
            native_bundle = compose_blackbox_input_bundle(
                snapshot,
                platform_input_ids=("api-wind-date-v1",),
                platform_input_artifacts=(native_artifact,),
            )

        self.assertEqual(
            harness_bundle.combined_snapshot_id,
            native_bundle.combined_snapshot_id,
        )
        self.assertEqual(
            harness_bundle.identity_manifest,
            native_bundle.identity_manifest,
        )
        json.dumps(harness_bundle.identity_manifest, sort_keys=True)
        json.dumps(harness_bundle.audit_manifest, sort_keys=True)
        mutated_identity = harness_bundle.identity_manifest
        mutated_identity["parent_snapshot_id"] = "forged-parent"
        mutated_identity["platform_inputs"][0]["sha256"] = "0" * 64
        mutated_audit = harness_bundle.audit_manifest
        mutated_audit["base_snapshot"]["generation_id"] = "forged-generation"
        self.assertEqual(
            harness_bundle.identity_manifest,
            native_bundle.identity_manifest,
        )
        self.assertNotEqual(
            harness_bundle.identity_manifest["parent_snapshot_id"],
            "forged-parent",
        )
        self.assertNotEqual(
            harness_bundle.audit_manifest["base_snapshot"]["generation_id"],
            "forged-generation",
        )
        self.assertNotEqual(
            harness_bundle.audit_manifest,
            native_bundle.audit_manifest,
        )
        self.assertEqual(
            harness_bundle.expected_filenames,
            (
                "daily_output.csv",
                "weekly_output.csv",
                "monthly_output.csv",
                "api_wind_date.csv",
            ),
        )

    def test_bundle_rejects_duplicate_or_mismatched_platform_artifacts(
        self,
    ) -> None:
        from shared.blackbox_v2.platform_inputs import freeze_platform_input
        from shared.blackbox_v2.snapshot import (
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )

        frames = _frames()
        expected = {name: list(frame.columns) for name, frame in frames.items()}
        artifact = freeze_platform_input(
            "api-wind-date-v1",
            pd.DataFrame(
                {
                    "rdate": ["2026-07-15"],
                    "week_id": ["202628"],
                }
            ),
            weekly_cutoff_key="202628",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=Path(tmpdir),
                expected_columns=expected,
                schema_version="data-bridge-v1",
            )
            with self.assertRaisesRegex(ValueError, "duplicate"):
                compose_blackbox_input_bundle(
                    snapshot,
                    platform_input_ids=(
                        "api-wind-date-v1",
                        "api-wind-date-v1",
                    ),
                    platform_input_artifacts=(artifact, artifact),
                )
            with self.assertRaisesRegex(ValueError, "must match"):
                compose_blackbox_input_bundle(
                    snapshot,
                    platform_input_ids=("api-wind-date-v1",),
                    platform_input_artifacts=(),
                )


def _frames() -> dict[str, pd.DataFrame]:
    return {
        "daily_output.csv": pd.DataFrame(
            {"date": ["2026-07-14", "2026-07-15", "2026-07-16"], "daily_factor": [1.0, 2.0, 3.0]}
        ),
        "weekly_output.csv": pd.DataFrame(
            {"week_id": ["202627", "202628"], "week_factor": [10.0, 11.0]}
        ),
        "monthly_output.csv": pd.DataFrame(
            {"month_id": ["202606", "202607"], "month_factor": [20.0, 21.0]}
        ),
    }


if __name__ == "__main__":
    unittest.main()
