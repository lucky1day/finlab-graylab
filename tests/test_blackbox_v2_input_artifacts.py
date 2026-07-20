from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
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
        self.assertEqual(snapshot.generation_id, "test-2026-07-16")
        self.assertEqual(snapshot.refresh_date, "2026-07-16")
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

    def test_bulk_cutoffs_read_sources_and_snapshot_once_for_100_unique_dates(self) -> None:
        from shared import data_service
        from shared.blackbox_v2.snapshot import create_snapshot_from_frames, resolve_cutoffs
        from shared.input_artifacts import resolve_blackbox_input_cutoffs_bulk

        feature_dates = [
            (date(2025, 1, 1) + timedelta(days=index)).isoformat()
            for index in range(100)
        ]
        daily_dates = [
            (date(2024, 12, 20) + timedelta(days=index)).isoformat()
            for index in range(112)
        ]
        weekly_raw = _weekly_source_rows()
        weekly_derivative = weekly_raw.iloc[0:0].copy()
        monthly_raw = _monthly_source_rows()
        monthly_derivative = pd.DataFrame(
            columns=["rdate", "month_id", "indicators_code", "indicators_value"]
        )
        metadata = _factor_metadata()
        weekly_keys = sorted({str(value) for value in weekly_raw["week_id"]})
        monthly_all = data_service.build_monthly_output_from_frames(
            metadata,
            monthly_raw,
            monthly_derivative,
            end_date=max(feature_dates),
        )
        frames = {
            "daily_output.csv": pd.DataFrame(
                {"date": daily_dates, "daily_factor": range(len(daily_dates))}
            ),
            "weekly_output.csv": pd.DataFrame(
                {"week_id": weekly_keys, "week_factor": range(len(weekly_keys))}
            ),
            "monthly_output.csv": pd.DataFrame(
                {
                    "month_id": monthly_all["month_id"].astype(str),
                    "month_factor": range(len(monthly_all)),
                }
            ),
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema_path = _write_schema(root, frames)
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=root / "snapshots",
                expected_columns={name: list(frame.columns) for name, frame in frames.items()},
                schema_version="data-bridge-v1",
            )
            real_read_csv = pd.read_csv
            with (
                patch(
                    "shared.input_artifacts._data_service.read_factor_metadata_from_db",
                    return_value=metadata,
                ) as read_metadata,
                patch(
                    "shared.input_artifacts._data_service.read_weekly_long_from_db",
                    side_effect=[weekly_raw, weekly_derivative],
                ) as read_weekly,
                patch(
                    "shared.input_artifacts._data_service.read_monthly_long_from_db",
                    side_effect=[monthly_raw, monthly_derivative],
                ) as read_monthly,
                patch("shared.input_artifacts.pd.read_csv", wraps=real_read_csv) as read_snapshot,
            ):
                resolved = resolve_blackbox_input_cutoffs_bulk(
                    snapshot,
                    feature_dates=feature_dates,
                    engine="engine",
                    schema_path=schema_path,
                )

            self.assertEqual(len(resolved), 100)
            self.assertEqual(read_metadata.call_count, 1)
            self.assertEqual(read_weekly.call_count, 2)
            self.assertEqual(read_monthly.call_count, 2)
            self.assertEqual(read_snapshot.call_count, 3)

            for boundary in ("2025-01-14", "2025-01-16", "2025-02-17"):
                weekly_as_of = data_service.build_weekly_output_from_frames(
                    ["week_id", "week_factor"],
                    weekly_raw,
                    weekly_derivative,
                    as_of_date=boundary,
                )
                monthly_as_of = data_service.build_monthly_output_from_frames(
                    metadata,
                    monthly_raw,
                    monthly_derivative,
                    end_date=boundary,
                )
                expected = resolve_cutoffs(
                    snapshot,
                    boundary,
                    weekly_as_of=weekly_as_of,
                    monthly_as_of=monthly_as_of,
                )
                self.assertEqual(resolved[boundary], expected)

            self.assertEqual(resolved["2025-01-14"].monthly_cutoff_key, "202501")
            self.assertEqual(resolved["2025-01-16"].monthly_cutoff_key, "202502")

    def test_bulk_cutoffs_cache_duplicate_feature_dates(self) -> None:
        from shared.blackbox_v2.snapshot import create_snapshot_from_frames
        from shared.input_artifacts import resolve_blackbox_input_cutoffs_bulk

        frames = _frames()
        metadata = _factor_metadata()
        weekly = pd.DataFrame(
            {
                "rdate": ["2026-07-10", "2026-07-15"],
                "week_id": ["202627", "202628"],
                "indicators_code": ["week_factor", "week_factor"],
                "indicators_value": [10.0, 11.0],
            }
        )
        monthly = pd.DataFrame(
            {
                "rdate": ["2026-06-15", "2026-07-15"],
                "indicators_code": ["month_factor", "month_factor"],
                "indicators_value": [20.0, 21.0],
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema_path = _write_schema(root, frames)
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=root / "snapshots",
                expected_columns={name: list(frame.columns) for name, frame in frames.items()},
                schema_version="data-bridge-v1",
            )
            with (
                patch(
                    "shared.input_artifacts._data_service.read_factor_metadata_from_db",
                    return_value=metadata,
                ),
                patch(
                    "shared.input_artifacts._data_service.read_weekly_long_from_db",
                    side_effect=[weekly, weekly.iloc[0:0]],
                ),
                patch(
                    "shared.input_artifacts._data_service.read_monthly_long_from_db",
                    side_effect=[monthly, monthly.assign(month_id="").iloc[0:0]],
                ),
            ):
                resolved = resolve_blackbox_input_cutoffs_bulk(
                    snapshot,
                    feature_dates=["2026-07-15", "2026-07-15"],
                    engine="engine",
                    schema_path=schema_path,
                )

        self.assertEqual(list(resolved), ["2026-07-15"])


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


def _factor_metadata() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "indicators_code": "month_factor",
                "frequency": "monthly",
                "status": "1",
                "pre_forecast_flag": "1",
                "lag_length": 0,
                "indicators_source": "raw",
            }
        ]
    )


def _weekly_source_rows() -> pd.DataFrame:
    rows = []
    cursor = date(2024, 12, 23)
    while cursor <= date(2025, 4, 14):
        iso = cursor.isocalendar()
        rows.append(
            {
                "rdate": cursor.isoformat(),
                "week_id": f"{iso.year}{iso.week:02d}",
                "indicators_code": "week_factor",
                "indicators_value": float(len(rows) + 1),
            }
        )
        cursor += timedelta(days=7)
    return pd.DataFrame(rows)


def _monthly_source_rows() -> pd.DataFrame:
    dates = (
        "2024-12-10",
        "2025-01-10",
        "2025-01-16",
        "2025-02-14",
        "2025-02-16",
        "2025-03-14",
        "2025-03-16",
        "2025-04-10",
    )
    return pd.DataFrame(
        {
            "rdate": dates,
            "indicators_code": ["month_factor"] * len(dates),
            "indicators_value": [float(index) for index in range(len(dates))],
        }
    )


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
