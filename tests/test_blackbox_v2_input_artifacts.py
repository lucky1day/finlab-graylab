from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd


class BlackboxV2InputArtifactTests(unittest.TestCase):
    def test_default_data_bridge_schema_matches_received_samples(self) -> None:
        from shared.input_artifacts import BLACKBOX_SCHEMA_PATH, _load_blackbox_schema

        version, columns = _load_blackbox_schema(BLACKBOX_SCHEMA_PATH)

        self.assertEqual(version, "data-bridge-v1")
        self.assertEqual(len(columns["daily_output.csv"]), 774)
        self.assertEqual(len(columns["weekly_output.csv"]), 575)
        self.assertEqual(len(columns["monthly_output.csv"]), 123)
        self.assertEqual(columns["daily_output.csv"][0], "date")
        self.assertEqual(columns["weekly_output.csv"][0], "week_id")
        self.assertEqual(columns["monthly_output.csv"][0], "month_id")

    def test_builds_three_frequency_snapshot_from_current_files_without_db_export(self) -> None:
        from shared.input_artifacts import build_blackbox_input_snapshot

        frames = _frames()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema_path = _write_schema(root, frames)
            data_root, runtime_root = _publish_current(root, frames, schema_path, refresh_date="2026-07-16")
            with patch("shared.input_artifacts._data_service") as data_service:
                snapshot = build_blackbox_input_snapshot(
                    snapshot_date="2026-07-16",
                    output_root=root / "snapshots",
                    schema_path=schema_path,
                    data_root=data_root,
                    refresh_runtime_root=runtime_root,
                    require_fresh=True,
                )

        self.assertTrue(snapshot.snapshot_id.startswith("snapshot-"))
        data_service.build_daily_output_from_db.assert_not_called()
        data_service.build_weekly_output_from_db.assert_not_called()
        data_service.build_monthly_output_from_db.assert_not_called()

    def test_open_snapshot_removes_runtime_copy_after_use(self) -> None:
        from shared.input_artifacts import open_blackbox_input_snapshot

        frames = _frames()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema_path = _write_schema(root, frames)
            data_root, runtime_root = _publish_current(root, frames, schema_path, refresh_date="2026-07-16")
            with open_blackbox_input_snapshot(
                snapshot_date="2026-07-16",
                schema_path=schema_path,
                data_root=data_root,
                refresh_runtime_root=runtime_root,
                snapshot_runtime_root=root / "runtime-snapshots",
                require_fresh=True,
            ) as snapshot:
                snapshot_root = snapshot.root_dir
                self.assertTrue(snapshot.data_dir.is_dir())
            self.assertFalse(snapshot_root.exists())

    def test_rejects_stale_current_files_for_scheduled_run(self) -> None:
        from shared.data_bridge.refresh import DataBridgeRefreshError
        from shared.input_artifacts import build_blackbox_input_snapshot

        frames = _frames()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema_path = _write_schema(root, frames)
            data_root, runtime_root = _publish_current(root, frames, schema_path, refresh_date="2026-07-15")
            with self.assertRaisesRegex(DataBridgeRefreshError, "refresh_date"):
                build_blackbox_input_snapshot(
                    snapshot_date="2026-07-16",
                    output_root=root / "snapshots",
                    schema_path=schema_path,
                    data_root=data_root,
                    refresh_runtime_root=runtime_root,
                    require_fresh=True,
                )

    def test_resolves_cutoffs_from_platform_as_of_outputs(self) -> None:
        from shared.input_artifacts import (
            build_blackbox_input_snapshot,
            resolve_blackbox_input_cutoffs,
        )

        frames = _frames()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema_path = _write_schema(root, frames)
            with patch("shared.input_artifacts._data_service") as data_service:
                data_service.build_daily_output_from_db.return_value = frames["daily_output.csv"]
                data_service.build_weekly_output_from_db.return_value = frames["weekly_output.csv"]
                data_service.build_monthly_output_from_db.return_value = frames["monthly_output.csv"]
                snapshot = build_blackbox_input_snapshot(
                    snapshot_date="2026-07-16",
                    output_root=root / "snapshots",
                    schema_path=schema_path,
                    data_root=_publish_current(root, frames, schema_path, refresh_date="2026-07-16")[0],
                    refresh_runtime_root=root / "runtime",
                    require_fresh=True,
                )

                data_service.build_weekly_output_from_db.return_value = frames["weekly_output.csv"].iloc[:1]
                data_service.build_monthly_output_from_db.return_value = frames["monthly_output.csv"].iloc[:1]
                cutoffs = resolve_blackbox_input_cutoffs(
                    snapshot,
                    feature_date="2026-07-15",
                    engine="engine",
                    schema_path=schema_path,
                )

        self.assertEqual(cutoffs.daily_cutoff_key, "2026-07-15")
        self.assertEqual(cutoffs.weekly_cutoff_key, "202627")
        self.assertEqual(cutoffs.monthly_cutoff_key, "202606")
        data_service.build_weekly_output_from_db.assert_called_with(
            schema_columns=["week_id", "week_factor"],
            as_of_date="2026-07-15",
            engine="engine",
        )
        data_service.build_monthly_output_from_db.assert_called_with(
            end_date="2026-07-15", engine="engine"
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


def _write_schema(root: Path, frames: dict[str, pd.DataFrame]) -> Path:
    path = root / "schema.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "data-bridge-v1",
                "files": {name: {"columns": list(frame.columns)} for name, frame in frames.items()},
            }
        ),
        encoding="utf-8",
    )
    return path


def _publish_current(
    root: Path,
    frames: dict[str, pd.DataFrame],
    schema_path: Path,
    *,
    refresh_date: str,
) -> tuple[Path, Path]:
    from shared.data_bridge.refresh import DataBridgeStore
    from shared.data_bridge.validation import validate_dataset, write_validated_dataset

    data_root = root / "data-root"
    runtime_root = root / "runtime"
    dataset = validate_dataset(frames, schema_path=schema_path)
    candidate = root / f"candidate-{refresh_date}"
    if candidate.exists():
        import shutil

        shutil.rmtree(candidate)
    write_validated_dataset(dataset, candidate)
    state = {
        "schema_version": dataset.schema_version,
        "generation_id": f"test-{refresh_date}",
        "refresh_date": refresh_date,
        "business_digest": dataset.business_digest,
        "files": {
            name: {
                "sha256": profile.sha256,
                "business_hash": profile.business_hash,
                "rows": profile.rows,
                "columns": profile.columns,
                "min_key": profile.min_key,
                "max_key": profile.max_key,
            }
            for name, profile in dataset.files.items()
        },
    }
    DataBridgeStore(data_root=data_root, runtime_root=runtime_root).publish(candidate, state)
    return data_root, runtime_root


if __name__ == "__main__":
    unittest.main()
