from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd


class BlackboxV2InputArtifactTests(unittest.TestCase):
    def test_default_data_bridge_schema_defines_required_baseline_columns(self) -> None:
        from shared.input_artifacts import BLACKBOX_SCHEMA_PATH, _load_blackbox_schema

        version, columns = _load_blackbox_schema(BLACKBOX_SCHEMA_PATH)

        self.assertEqual(version, "data-bridge-v1")
        expected_keys = {
            "daily_output.csv": "date",
            "weekly_output.csv": "week_id",
            "monthly_output.csv": "month_id",
        }
        for filename, key in expected_keys.items():
            self.assertTrue(columns[filename])
            self.assertEqual(columns[filename][0], key)
            self.assertEqual(len(columns[filename]), len(set(columns[filename])))

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

    def test_harness_platform_input_capture_reuses_calendar_snapshot_reader(
        self,
    ) -> None:
        from shared.input_artifacts import (
            capture_blackbox_platform_inputs_from_connection,
        )

        calendar_frames = {
            "api_wind_date.csv": pd.DataFrame(
                {
                    "rdate": ["2026-07-14", "2026-07-15"],
                    "week_id": [202628, 202628],
                }
            ),
            "t_trade_calendar.csv": pd.DataFrame(
                {
                    "rdate": ["2026-07-14", "2026-07-15"],
                    "trade_flag": ["1", "1"],
                }
            ),
        }
        with patch(
            "shared.blackbox_v2.platform_inputs."
            "read_calendar_snapshot_from_connection",
            return_value=calendar_frames,
        ) as read_calendar:
            artifacts = capture_blackbox_platform_inputs_from_connection(
                ("api-wind-date-v1",),
                connection="read-only-connection",
                weekly_cutoff_key="202628",
                captured_at="2026-07-26T00:00:00Z",
            )

        read_calendar.assert_called_once_with("read-only-connection")
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].filename, "api_wind_date.csv")
        self.assertEqual(
            artifacts[0].audit_provenance,
            {
                "source_kind": "harness_database",
                "generation_id": None,
                "manifest_sha256": None,
                "captured_at": "2026-07-26T00:00:00Z",
            },
        )

    def test_scheduled_platform_input_capture_uses_validated_native_frame(
        self,
    ) -> None:
        from unittest.mock import Mock

        from shared.input_artifacts import (
            capture_blackbox_platform_inputs_from_native_generation,
        )
        from shared.native_input_generation import NativeGenerationContext

        frame = pd.DataFrame(
            {
                "rdate": ["2026-07-14", "2026-07-15"],
                "week_id": ["202628.0", "202628"],
            }
        )
        generation = Mock(spec=NativeGenerationContext)
        generation.frame.return_value = frame
        generation.generation_id = "native-generation-1"
        generation.manifest_sha256 = "b" * 64

        artifacts = capture_blackbox_platform_inputs_from_native_generation(
            ("api-wind-date-v1",),
            native_generation=generation,
            weekly_cutoff_key="202628",
            captured_at="2026-07-26T00:01:00Z",
        )

        generation.frame.assert_called_once_with("api_wind_date")
        self.assertEqual(
            artifacts[0].audit_provenance,
            {
                "source_kind": "scheduled_native_generation",
                "generation_id": "native-generation-1",
                "manifest_sha256": "b" * 64,
                "captured_at": "2026-07-26T00:01:00Z",
            },
        )

    def test_db_and_native_platform_input_capture_are_content_identical(
        self,
    ) -> None:
        from unittest.mock import Mock

        from shared.input_artifacts import (
            capture_blackbox_platform_inputs_from_connection,
            capture_blackbox_platform_inputs_from_native_generation,
        )
        from shared.native_input_generation import NativeGenerationContext

        db_frame = pd.DataFrame(
            {
                "rdate": [date(2026, 7, 14), date(2026, 7, 15)],
                "week_id": [202628, 202628],
            }
        )
        native_frame = pd.DataFrame(
            {
                "rdate": ["2026-07-14", "2026-07-15"],
                "week_id": ["202628.0", "202628"],
            }
        )
        generation = Mock(spec=NativeGenerationContext)
        generation.frame.return_value = native_frame
        generation.generation_id = "native-generation-1"
        generation.manifest_sha256 = "c" * 64
        with patch(
            "shared.blackbox_v2.platform_inputs."
            "read_calendar_snapshot_from_connection",
            return_value={
                "api_wind_date.csv": db_frame,
                "t_trade_calendar.csv": pd.DataFrame(),
            },
        ):
            db_artifact = capture_blackbox_platform_inputs_from_connection(
                ("api-wind-date-v1",),
                connection="read-only-connection",
                weekly_cutoff_key="202628",
                captured_at="2026-07-26T00:00:00Z",
            )[0]
        native_artifact = (
            capture_blackbox_platform_inputs_from_native_generation(
                ("api-wind-date-v1",),
                native_generation=generation,
                weekly_cutoff_key="202628",
                captured_at="2026-07-26T00:01:00Z",
            )[0]
        )

        self.assertEqual(db_artifact.content_bytes, native_artifact.content_bytes)
        self.assertEqual(db_artifact.sha256, native_artifact.sha256)
        self.assertEqual(
            db_artifact.identity_manifest,
            native_artifact.identity_manifest,
        )

    def test_empty_platform_input_selection_does_not_capture_calendar(self) -> None:
        from shared.input_artifacts import (
            capture_blackbox_platform_inputs_from_connection,
        )

        with patch(
            "shared.blackbox_v2.platform_inputs."
            "read_calendar_snapshot_from_connection",
        ) as read_calendar:
            artifacts = capture_blackbox_platform_inputs_from_connection(
                (),
                connection="read-only-connection",
                weekly_cutoff_key="202628",
            )

        self.assertEqual(artifacts, ())
        read_calendar.assert_not_called()

    def test_runtime_view_materializes_unique_read_only_regular_files_and_cleans_up(
        self,
    ) -> None:
        import os
        import stat

        from shared.blackbox_v2.platform_inputs import freeze_platform_input
        from shared.blackbox_v2.snapshot import (
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )
        from shared.input_artifacts import open_blackbox_runtime_view

        frames = _frames()
        artifact = freeze_platform_input(
            "api-wind-date-v1",
            pd.DataFrame(
                {
                    "rdate": ["2026-07-14", "2026-07-15"],
                    "week_id": [202628, 202628],
                }
            ),
            weekly_cutoff_key="202628",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=root / "snapshots",
                expected_columns={
                    name: list(frame.columns)
                    for name, frame in frames.items()
                },
                schema_version="data-bridge-v1",
            )
            bundle = compose_blackbox_input_bundle(
                snapshot,
                platform_input_ids=("api-wind-date-v1",),
                platform_input_artifacts=(artifact,),
            )
            with open_blackbox_runtime_view(
                bundle,
                runtime_root=root / "runtime",
            ) as first:
                first_path = first.data_dir
                self.assertEqual(
                    tuple(sorted(path.name for path in first.data_dir.iterdir())),
                    tuple(sorted(bundle.expected_filenames)),
                )
                self.assertEqual(
                    stat.S_IMODE(first.data_dir.stat().st_mode),
                    0o555,
                )
                for path in first.data_dir.iterdir():
                    self.assertTrue(path.is_file())
                    self.assertFalse(path.is_symlink())
                    self.assertEqual(path.stat().st_nlink, 1)
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)
                self.assertNotEqual(
                    os.stat(
                        snapshot.data_dir / "daily_output.csv"
                    ).st_ino,
                    os.stat(
                        first.data_dir / "daily_output.csv"
                    ).st_ino,
                )
                with open_blackbox_runtime_view(
                    bundle,
                    runtime_root=root / "runtime",
                ) as second:
                    self.assertNotEqual(first.data_dir, second.data_dir)
            self.assertFalse(first_path.exists())
            self.assertTrue(snapshot.root_dir.exists())

    def test_runtime_view_cleans_up_after_body_exception_without_masking_it(
        self,
    ) -> None:
        from shared.blackbox_v2.snapshot import (
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )
        from shared.input_artifacts import open_blackbox_runtime_view

        frames = _frames()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=root / "snapshots",
                expected_columns={
                    name: list(frame.columns)
                    for name, frame in frames.items()
                },
                schema_version="data-bridge-v1",
            )
            bundle = compose_blackbox_input_bundle(snapshot)
            with self.assertRaisesRegex(RuntimeError, "body failed"):
                with open_blackbox_runtime_view(
                    bundle,
                    runtime_root=root / "runtime",
                ) as view:
                    runtime_path = view.data_dir
                    raise RuntimeError("body failed")
            self.assertFalse(runtime_path.exists())

    def test_runtime_view_rejects_dataclass_replace_forged_bundle(self) -> None:
        from dataclasses import replace

        from shared.blackbox_v2.snapshot import (
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )
        from shared.input_artifacts import open_blackbox_runtime_view

        frames = _frames()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=root / "snapshots",
                expected_columns={
                    name: list(frame.columns)
                    for name, frame in frames.items()
                },
                schema_version="data-bridge-v1",
            )
            bundle = compose_blackbox_input_bundle(snapshot)
            forged = replace(
                bundle,
                combined_snapshot_id="snapshot-forged",
            )

            with self.assertRaisesRegex(ValueError, "trusted composition"):
                with open_blackbox_runtime_view(
                    forged,
                    runtime_root=root / "runtime",
                ):
                    self.fail("forged bundle must not be yielded")

    def test_runtime_view_uses_trusted_bundle_after_stateful_iterable_validation(
        self,
    ) -> None:
        from dataclasses import replace

        from shared.blackbox_v2.platform_inputs import freeze_platform_input
        from shared.blackbox_v2.snapshot import (
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )
        from shared.input_artifacts import open_blackbox_runtime_view

        frames = _frames()
        first = freeze_platform_input(
            "api-wind-date-v1",
            pd.DataFrame(
                {
                    "rdate": ["2026-07-14", "2026-07-15"],
                    "week_id": [202628, 202628],
                }
            ),
            weekly_cutoff_key="202628",
        )
        second = freeze_platform_input(
            "api-wind-date-v1",
            pd.DataFrame(
                {
                    "rdate": ["2026-07-14", "2026-07-15"],
                    "week_id": [202627, 202628],
                }
            ),
            weekly_cutoff_key="202628",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=root / "snapshots",
                expected_columns={
                    name: list(frame.columns)
                    for name, frame in frames.items()
                },
                schema_version="data-bridge-v1",
            )
            bundle = compose_blackbox_input_bundle(
                snapshot,
                platform_input_ids=("api-wind-date-v1",),
                platform_input_artifacts=(first,),
            )
            stateful = _StatefulArtifactIterable(first, second)
            forged = replace(
                bundle,
                platform_input_artifacts=stateful,
            )

            with open_blackbox_runtime_view(
                forged,
                runtime_root=root / "runtime",
            ) as view:
                self.assertIsNot(view.bundle, forged)
                self.assertEqual(
                    (view.data_dir / "api_wind_date.csv").read_bytes(),
                    first.content_bytes,
                )
            self.assertEqual(stateful.iteration_count, 1)

    def test_runtime_view_preserves_uncertain_process_as_controlled_debris(
        self,
    ) -> None:
        from shared.blackbox_v2.snapshot import (
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )
        from shared.input_artifacts import (
            cleanup_blackbox_runtime_debris,
            open_blackbox_runtime_view,
        )

        frames = _frames()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime_root = root / "runtime"
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=root / "snapshots",
                expected_columns={
                    name: list(frame.columns)
                    for name, frame in frames.items()
                },
                schema_version="data-bridge-v1",
            )
            bundle = compose_blackbox_input_bundle(snapshot)
            with open_blackbox_runtime_view(
                bundle,
                runtime_root=runtime_root,
            ) as view:
                original_path = view.data_dir
                view.mark_termination_uncertain()

            self.assertTrue(original_path.exists())
            self.assertIsNotNone(view.debris_path)
            assert view.debris_path is not None
            self.assertEqual(view.debris_path, original_path)
            self.assertTrue(view.debris_path.is_dir())
            self.assertTrue(
                (view.debris_path / "daily_output.csv").read_bytes()
            )
            self.assertEqual(
                view.debris_path.parent,
                (runtime_root / "active").resolve(),
            )
            markers = list((runtime_root / "debris").glob("*.json"))
            self.assertEqual(len(markers), 1)
            self.assertEqual(
                view.bundle.identity_manifest,
                bundle.identity_manifest,
            )

            cleanup_blackbox_runtime_debris(
                view.debris_path,
                runtime_root=runtime_root,
            )
            self.assertFalse(view.debris_path.exists())
            self.assertFalse(markers[0].exists())

    def test_runtime_view_rejects_parent_file_hash_mismatch_and_removes_staging(
        self,
    ) -> None:
        from shared.blackbox_v2.snapshot import (
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )
        from shared.input_artifacts import open_blackbox_runtime_view

        frames = _frames()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=root / "snapshots",
                expected_columns={
                    name: list(frame.columns)
                    for name, frame in frames.items()
                },
                schema_version="data-bridge-v1",
            )
            source = snapshot.data_dir / "daily_output.csv"
            source.chmod(0o644)
            source.write_text("date,daily_factor\n2026-07-15,999\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "sha256"):
                with open_blackbox_runtime_view(
                    compose_blackbox_input_bundle(snapshot),
                    runtime_root=root / "runtime",
                ):
                    self.fail("corrupt snapshot must not be yielded")
            self.assertEqual(
                list((root / "runtime" / "active").iterdir()),
                [],
            )

    def test_runtime_view_accepts_databridge_dataset_content_manifest(
        self,
    ) -> None:
        from shared.blackbox_v2.snapshot import (
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )
        from shared.input_artifacts import open_blackbox_runtime_view

        frames = _frames()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=root / "snapshots",
                expected_columns={
                    name: list(frame.columns)
                    for name, frame in frames.items()
                },
                schema_version="data-bridge-v1",
            )
            databridge_snapshot = _convert_to_databridge_snapshot(snapshot)
            with open_blackbox_runtime_view(
                compose_blackbox_input_bundle(databridge_snapshot),
                runtime_root=root / "runtime",
            ) as view:
                self.assertEqual(
                    set(path.name for path in view.data_dir.iterdir()),
                    set(_frames()),
                )

    def test_runtime_view_rejects_forged_parent_geometry_schema_and_identity(
        self,
    ) -> None:
        from dataclasses import replace

        from shared.blackbox_v2.snapshot import (
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )
        from shared.input_artifacts import open_blackbox_runtime_view

        def build_snapshot(root: Path):
            frames = _frames()
            return create_snapshot_from_frames(
                frames,
                output_root=root,
                expected_columns={
                    name: list(frame.columns)
                    for name, frame in frames.items()
                },
                schema_version="data-bridge-v1",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot = build_snapshot(root / "geometry")
            for forged in (
                replace(snapshot, data_dir=snapshot.root_dir),
                replace(
                    snapshot,
                    manifest_path=snapshot.data_dir / "daily_output.csv",
                ),
                replace(snapshot, schema_version="forged-schema"),
            ):
                with self.assertRaises(ValueError):
                    with open_blackbox_runtime_view(
                        compose_blackbox_input_bundle(forged),
                        runtime_root=root / "runtime",
                    ):
                        self.fail("forged snapshot must not be yielded")

            snapshot = build_snapshot(root / "identity")
            daily = snapshot.data_dir / "daily_output.csv"
            daily.chmod(0o644)
            daily.write_text(
                "date,daily_factor\n2026-07-15,999\n",
                encoding="utf-8",
            )
            manifest_path = snapshot.manifest_path
            manifest_path.chmod(0o644)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"]["daily_output.csv"] = {
                "sha256": _sha256(daily.read_bytes()),
                "row_count": 1,
                "columns": ["date", "daily_factor"],
            }
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "identity"):
                with open_blackbox_runtime_view(
                    compose_blackbox_input_bundle(snapshot),
                    runtime_root=root / "runtime",
                ):
                    self.fail("self-consistent content cannot impersonate old ID")

    def test_cleanup_rejects_active_view_without_debris_marker(self) -> None:
        from shared.blackbox_v2.snapshot import (
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )
        from shared.input_artifacts import (
            cleanup_blackbox_runtime_debris,
            open_blackbox_runtime_view,
        )

        frames = _frames()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime_root = root / "runtime"
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=root / "snapshots",
                expected_columns={
                    name: list(frame.columns)
                    for name, frame in frames.items()
                },
                schema_version="data-bridge-v1",
            )
            with open_blackbox_runtime_view(
                compose_blackbox_input_bundle(snapshot),
                runtime_root=runtime_root,
            ) as view:
                with self.assertRaisesRegex(ValueError, "marker"):
                    cleanup_blackbox_runtime_debris(
                        view.data_dir,
                        runtime_root=runtime_root,
                    )
                self.assertTrue(view.data_dir.is_dir())

    def test_cleanup_does_not_follow_nested_symlink_or_change_external_file(
        self,
    ) -> None:
        import stat

        from shared.blackbox_v2.snapshot import (
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )
        from shared.input_artifacts import open_blackbox_runtime_view

        frames = _frames()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            external = root / "external.txt"
            external.write_text("outside", encoding="utf-8")
            external.chmod(0o400)
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=root / "snapshots",
                expected_columns={
                    name: list(frame.columns)
                    for name, frame in frames.items()
                },
                schema_version="data-bridge-v1",
            )
            with open_blackbox_runtime_view(
                compose_blackbox_input_bundle(snapshot),
                runtime_root=root / "runtime",
            ) as view:
                runtime_path = view.data_dir
                runtime_path.chmod(0o755)
                (runtime_path / "external-link").symlink_to(external)
                runtime_path.chmod(0o555)

            self.assertFalse(runtime_path.exists())
            self.assertEqual(external.read_text(encoding="utf-8"), "outside")
            self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o400)


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


def _sha256(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


class _StatefulArtifactIterable:
    def __init__(self, first, second) -> None:
        self._first = first
        self._second = second
        self.iteration_count = 0

    def __iter__(self):
        self.iteration_count += 1
        if self.iteration_count == 1:
            return iter((self._first,))
        return iter((self._second,))


def _convert_to_databridge_snapshot(snapshot):
    from dataclasses import replace

    manifest = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))
    files = {
        filename: {
            "path": f"data/{filename}",
            "sha256": entry["sha256"],
            "size_bytes": (snapshot.data_dir / filename).stat().st_size,
            "row_count": entry["row_count"],
            "columns": entry["columns"],
        }
        for filename, entry in manifest["files"].items()
    }
    identity = {
        "schema_version": manifest["schema_version"],
        "files": files,
    }
    dataset_content_id = _sha256(
        json.dumps(
            identity,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    databridge_manifest = {
        "schema_version": manifest["schema_version"],
        "dataset_content_id": dataset_content_id,
        "files": files,
    }
    snapshot.manifest_path.chmod(0o644)
    snapshot.manifest_path.write_text(
        json.dumps(databridge_manifest, sort_keys=True),
        encoding="utf-8",
    )
    snapshot.manifest_path.chmod(0o444)
    return replace(
        snapshot,
        snapshot_id=f"snapshot-{dataset_content_id[:24]}",
    )


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
