from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

import pandas as pd


class BlackboxV2SnapshotTests(unittest.TestCase):
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

    def test_snapshot_rejects_schema_order_change(self) -> None:
        from shared.blackbox_v2.snapshot import create_snapshot_from_frames

        frames = _frames()
        expected = {name: list(frame.columns) for name, frame in frames.items()}
        expected["weekly_output.csv"] = ["week_factor", "week_id"]
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "column order"):
                create_snapshot_from_frames(
                    frames,
                    output_root=Path(tmpdir),
                    expected_columns=expected,
                    schema_version="data-bridge-v1",
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
