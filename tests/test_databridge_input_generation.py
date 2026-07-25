from __future__ import annotations

import inspect
import json
import os
import shutil
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import pandas as pd

from tests.test_native_generation_input_artifacts import _generation_context


SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "shared"
    / "blackbox_v2"
    / "data_bridge_v1_schema.json"
)


def _frame(filename: str, keys: list[str]) -> pd.DataFrame:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    columns = list(schema["files"][filename]["columns"])
    return pd.DataFrame(
        [
            {
                column: (
                    key
                    if column == columns[0]
                    else str(index + 1)
                )
                for index, column in enumerate(columns)
            }
            for key in keys
        ],
        columns=columns,
    )


def _current_dataset(
    *,
    upstream_generation_id: str = "full-20260724-065000-abcdef123456",
):
    from shared.data_bridge.refresh import CurrentDataset
    from shared.data_bridge.validation import validate_dataset

    frames = {
        "daily_output.csv": _frame(
            "daily_output.csv",
            ["2026-07-22", "2026-07-23", "2026-07-24"],
        ),
        "weekly_output.csv": _frame(
            "weekly_output.csv",
            ["202628", "202629", "202630"],
        ),
        "monthly_output.csv": _frame(
            "monthly_output.csv",
            ["202606", "202607", "202608"],
        ),
    }
    dataset = validate_dataset(
        frames,
        schema_path=SCHEMA_PATH,
        expected_daily_date="2026-07-23",
    )
    state = {
        "generation_id": upstream_generation_id,
        "refresh_date": "2026-07-24",
        "refresh_started_at": "2026-07-24T06:30:00+08:00",
        "refreshed_at": "2026-07-24T06:50:00+08:00",
        "published_at": "2026-07-24T06:50:01+08:00",
        "source_mode": "full_export",
        "stability_rounds": 2,
        "last_attempt": {
            "status": "success",
            "refresh_date": "2026-07-24",
            "started_at": "2026-07-24T06:30:00+08:00",
            "finished_at": "2026-07-24T06:50:00+08:00",
            "duration_sec": 1200.0,
            "error": None,
        },
        "schema_version": dataset.schema_version,
        "business_digest": dataset.business_digest,
        "files": {
            filename: {
                "sha256": profile.sha256,
                "rows": profile.rows,
                "columns": profile.columns,
            }
            for filename, profile in dataset.files.items()
        },
    }
    return CurrentDataset(
        state=MappingProxyType(state),
        dataset=dataset,
    )


_NATIVE_TEST_DIRECTORIES: list[tempfile.TemporaryDirectory[str]] = []


def _native_cutoff_context(*, engine: object | None = None):
    """生成真实可重开的 Native generation，供 DataBridge 契约测试使用。"""
    import hashlib
    from datetime import datetime, timezone

    from shared import native_input_generation as native_module
    from tests.test_native_input_generation import _Engine

    source = _generation_context()
    temporary = tempfile.TemporaryDirectory()
    _NATIVE_TEST_DIRECTORIES.append(temporary)
    factor_tables = set(native_module._data_contract.FACTOR_SOURCE_TABLES)
    calendar_tables = set(native_module._data_contract.CALENDAR_SOURCE_TABLES)
    table_names = sorted(
        factor_tables
        | calendar_tables
        | {native_module._data_contract.METADATA_SOURCE_TABLE}
    )
    tables = tuple(
        native_module._data_contract.SourceTableEvidence(
            table_name=table_name,
            row_count=1,
            latest_create_time=(
                None
                if table_name in calendar_tables
                else "2026-07-24T06:29:00.000000"
            ),
            latest_update_time=(
                "2026-07-24T06:29:00.000000"
                if table_name == native_module._data_contract.METADATA_SOURCE_TABLE
                else None
            ),
            latest_business_key=(
                "2026-07-23" if table_name in calendar_tables else None
            ),
        )
        for table_name in table_names
    )
    provisional_evidence = native_module._data_contract.SourceCommitEvidence(
        feature_date="2026-07-23",
        source_commit_token="0" * 64,
        tables=tables,
    )
    evidence_payload = native_module._source_evidence_payload(
        provisional_evidence,
        expected_feature_date="2026-07-23",
    )
    source_commit_token = hashlib.sha256(
        native_module._canonical_json_bytes(evidence_payload)
    ).hexdigest()
    source_evidence = native_module._data_contract.SourceCommitEvidence(
        feature_date="2026-07-23",
        source_commit_token=source_commit_token,
        tables=tables,
    )

    def factor_reader(
        _codes: object,
        table_name: str,
        _engine: object,
        include_month_id: bool = False,
    ) -> pd.DataFrame:
        del include_month_id
        frame = source.frame(f"{table_name}.csv")
        if table_name == "api_wind_daily":
            present = set(frame["indicators_code"].astype(str))
            missing = [
                code
                for code in (
                    native_module._data_contract
                    .NATIVE_READINESS_DAILY_ANCHORS
                )
                if code not in present
            ]
            frame = pd.concat(
                [
                    frame,
                    pd.DataFrame(
                        [
                            {
                                "rdate": "2026-07-23",
                                "indicators_code": code,
                                "indicators_value": 1.0,
                            }
                            for code in missing
                        ],
                        columns=frame.columns,
                    ),
                ],
                ignore_index=True,
            )
        return frame

    with (
        patch.object(
            native_module._data_service,
            "read_factor_metadata_from_db",
            return_value=source.frame("metadata.csv"),
        ),
        patch.object(
            native_module._data_service,
            "read_daily_long_from_db",
            side_effect=factor_reader,
        ),
        patch.object(
            native_module._data_service,
            "read_weekly_long_from_db",
            side_effect=factor_reader,
        ),
        patch.object(
            native_module._data_service,
            "read_monthly_long_from_db",
            side_effect=factor_reader,
        ),
        patch.object(
            native_module._data_contract,
            "capture_source_commit_evidence_from_connection",
            return_value=source_evidence,
        ),
    ):
        return native_module.create_native_generation(
            _Engine() if engine is None else engine,
            business_date="2026-07-24",
            feature_date="2026-07-23",
            source_contract_cutoff=datetime(
                2026,
                7,
                23,
                22,
                30,
                tzinfo=timezone.utc,
            ),
            capture_not_after=datetime(
                2026,
                7,
                23,
                22,
                31,
                tzinfo=timezone.utc,
            ),
            output_root=temporary.name,
            _snapshot_clock=lambda: datetime(
                2026,
                7,
                23,
                22,
                30,
                15,
                tzinfo=timezone.utc,
            ),
        )


class DataBridgeInputGenerationTests(unittest.TestCase):
    def test_generation_freezes_three_files_and_authoritative_cutoffs(
        self,
    ) -> None:
        from shared.databridge_input_generation import (
            create_databridge_generation,
            open_databridge_generation,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            created = create_databridge_generation(
                _current_dataset(),
                native_generation=_native_cutoff_context(),
                business_date="2026-07-24",
                feature_date="2026-07-23",
                output_root=Path(tmpdir),
                schema_path=SCHEMA_PATH,
            )
            reopened = open_databridge_generation(
                created.manifest_path,
                expected_generation_id=created.generation_id,
                expected_manifest_sha256=created.manifest_sha256,
                expected_business_date="2026-07-24",
                expected_feature_date="2026-07-23",
                schema_path=SCHEMA_PATH,
            )

            self.assertEqual(
                reopened.cutoffs.daily_cutoff_key,
                "2026-07-23",
            )
            self.assertEqual(
                reopened.cutoffs.weekly_cutoff_key,
                "202629",
            )
            self.assertEqual(
                reopened.cutoffs.monthly_cutoff_key,
                "202607",
            )
            self.assertEqual(
                reopened.upstream_generation_id,
                "full-20260724-065000-abcdef123456",
            )
            self.assertEqual(
                reopened.native_generation_id,
                _native_cutoff_context().generation_id,
            )
            self.assertEqual(reopened.source_mode, "full_export")
            self.assertEqual(reopened.stability_rounds, 2)
            self.assertEqual(
                reopened.refresh_started_at,
                "2026-07-23T22:30:00.000000Z",
            )
            self.assertTrue(reopened.snapshot.data_dir.is_dir())
            self.assertEqual(
                set(path.name for path in reopened.snapshot.data_dir.iterdir()),
                {
                    "daily_output.csv",
                    "weekly_output.csv",
                    "monthly_output.csv",
                },
            )

    def test_open_rehashes_every_file_and_rejects_tampering(self) -> None:
        from shared.databridge_input_generation import (
            create_databridge_generation,
            open_databridge_generation,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            created = create_databridge_generation(
                _current_dataset(),
                native_generation=_native_cutoff_context(),
                business_date="2026-07-24",
                feature_date="2026-07-23",
                output_root=Path(tmpdir),
                schema_path=SCHEMA_PATH,
            )
            path = created.data_dir / "weekly_output.csv"
            path.chmod(0o644)
            path.write_bytes(path.read_bytes() + b"\n")

            with self.assertRaisesRegex(
                ValueError,
                "hash mismatch",
            ):
                open_databridge_generation(
                    created.manifest_path,
                    schema_path=SCHEMA_PATH,
                )

    def test_same_dataset_with_new_upstream_generation_has_new_identity(
        self,
    ) -> None:
        from shared.databridge_input_generation import (
            create_databridge_generation,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            first = create_databridge_generation(
                _current_dataset(upstream_generation_id="upstream-a"),
                native_generation=_native_cutoff_context(),
                business_date="2026-07-24",
                feature_date="2026-07-23",
                output_root=Path(tmpdir),
                schema_path=SCHEMA_PATH,
            )
            second = create_databridge_generation(
                _current_dataset(upstream_generation_id="upstream-b"),
                native_generation=_native_cutoff_context(),
                business_date="2026-07-24",
                feature_date="2026-07-23",
                output_root=Path(tmpdir),
                schema_path=SCHEMA_PATH,
            )

        self.assertNotEqual(first.generation_id, second.generation_id)
        self.assertEqual(first.dataset_content_id, second.dataset_content_id)

    def test_rejects_old_current_or_mismatched_native_context(self) -> None:
        from shared.databridge_input_generation import (
            create_databridge_generation,
        )

        old = _current_dataset()
        old_state = dict(old.state)
        old_state["refresh_date"] = "2026-07-23"
        old = replace(old, state=MappingProxyType(old_state))
        mismatched_native = replace(
            _native_cutoff_context(),
            business_date="2026-07-23",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(
                ValueError,
                "refresh_date",
            ):
                create_databridge_generation(
                    old,
                    native_generation=_native_cutoff_context(),
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    output_root=Path(tmpdir),
                    schema_path=SCHEMA_PATH,
                )
            with self.assertRaisesRegex(
                ValueError,
                "Native generation business_date",
            ):
                create_databridge_generation(
                    _current_dataset(),
                    native_generation=mismatched_native,
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    output_root=Path(tmpdir),
                    schema_path=SCHEMA_PATH,
                )

    def test_rejects_refresh_started_before_0630_contract(
        self,
    ) -> None:
        from shared.databridge_input_generation import (
            create_databridge_generation,
        )

        stale = _current_dataset()
        stale_state = dict(stale.state)
        stale_state["refresh_started_at"] = (
            "2026-07-24T06:29:59+08:00"
        )
        stale = replace(stale, state=MappingProxyType(stale_state))
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(
                ValueError,
                "after 06:30",
            ):
                create_databridge_generation(
                    stale,
                    native_generation=_native_cutoff_context(),
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    output_root=Path(tmpdir),
                    schema_path=SCHEMA_PATH,
                )

    def test_rejects_non_full_or_unstable_refresh_evidence(self) -> None:
        from shared.databridge_input_generation import (
            create_databridge_generation,
        )

        for field, value, message in (
            ("source_mode", "incremental", "full_export"),
            ("stability_rounds", 1, "stability_rounds"),
            ("published_at", None, "published_at"),
        ):
            with self.subTest(field=field):
                current = _current_dataset()
                state = dict(current.state)
                state[field] = value
                current = replace(
                    current,
                    state=MappingProxyType(state),
                )
                with tempfile.TemporaryDirectory() as tmpdir:
                    with self.assertRaisesRegex(ValueError, message):
                        create_databridge_generation(
                            current,
                            native_generation=_native_cutoff_context(),
                            business_date="2026-07-24",
                            feature_date="2026-07-23",
                            output_root=Path(tmpdir),
                            schema_path=SCHEMA_PATH,
                        )

    def test_rejects_daily_cutoff_before_feature_date(self) -> None:
        from shared.data_bridge.refresh import CurrentDataset
        from shared.data_bridge.validation import validate_dataset
        from shared.databridge_input_generation import (
            create_databridge_generation,
        )

        current = _current_dataset()
        stale_frames = {
            name: frame.copy(deep=True)
            for name, frame in current.dataset.frames.items()
        }
        stale_frames["daily_output.csv"] = stale_frames[
            "daily_output.csv"
        ][
            stale_frames["daily_output.csv"]["date"]
            != "2026-07-23"
        ]
        stale_dataset = validate_dataset(
            stale_frames,
            schema_path=SCHEMA_PATH,
        )
        state = dict(current.state)
        state["business_digest"] = stale_dataset.business_digest
        state["files"] = {
            filename: {
                "sha256": profile.sha256,
                "rows": profile.rows,
                "columns": profile.columns,
            }
            for filename, profile in stale_dataset.files.items()
        }
        stale = CurrentDataset(
            state=MappingProxyType(state),
            dataset=stale_dataset,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(
                ValueError,
                "daily cutoff must equal feature_date",
            ):
                create_databridge_generation(
                    stale,
                    native_generation=_native_cutoff_context(),
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    output_root=Path(tmpdir),
                    schema_path=SCHEMA_PATH,
                )


class DataBridgeGenerationDurabilityAndRetentionTests(unittest.TestCase):
    def test_create_reopens_and_rehashes_native_generation_before_use(
        self,
    ) -> None:
        from shared.databridge_input_generation import (
            create_databridge_generation,
        )

        native = _native_cutoff_context()
        native.root_dir.chmod(0o755)
        target = native.root_dir / "weekly_cutoff_index.csv"
        target.chmod(0o644)
        target.write_bytes(target.read_bytes() + b"\n")

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "file hash mismatch"):
                create_databridge_generation(
                    _current_dataset(),
                    native_generation=native,
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    output_root=Path(tmpdir),
                    schema_path=SCHEMA_PATH,
                )

    def test_create_rejects_cached_native_manifest_identity_drift(
        self,
    ) -> None:
        from shared.databridge_input_generation import (
            create_databridge_generation,
        )

        native = replace(
            _native_cutoff_context(),
            manifest_sha256="b" * 64,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "manifest sha256 mismatch"):
                create_databridge_generation(
                    _current_dataset(),
                    native_generation=native,
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    output_root=Path(tmpdir),
                    schema_path=SCHEMA_PATH,
                )

    def test_create_uses_reopened_native_frames_not_cached_frames(
        self,
    ) -> None:
        from shared.databridge_input_generation import (
            create_databridge_generation,
        )

        native = _native_cutoff_context()
        forged_frames = dict(native._frames)
        forged_frames["weekly_cutoff_index.csv"] = pd.DataFrame(
            [
                {
                    "week_id": "209999",
                    "available_date": "2026-07-23",
                }
            ]
        )
        cached_drift = replace(native, _frames=forged_frames)

        with tempfile.TemporaryDirectory() as tmpdir:
            created = create_databridge_generation(
                _current_dataset(),
                native_generation=cached_drift,
                business_date="2026-07-24",
                feature_date="2026-07-23",
                output_root=Path(tmpdir),
                schema_path=SCHEMA_PATH,
            )

        self.assertEqual(created.cutoffs.weekly_cutoff_key, "202629")

    def test_publish_fsyncs_data_staging_and_output_directories(
        self,
    ) -> None:
        from shared import databridge_input_generation as module

        native = _native_cutoff_context()
        events: list[tuple[str, Path | None]] = []
        real_replace = os.replace
        real_fsync_directory = module._fsync_directory

        def track_directory(path: Path) -> None:
            events.append(("fsync_dir", Path(path)))
            real_fsync_directory(path)

        def track_replace(source: object, destination: object) -> None:
            events.append(("replace", None))
            real_replace(source, destination)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir)
            with (
                patch.object(
                    module,
                    "_fsync_directory",
                    side_effect=track_directory,
                ),
                patch.object(
                    module.os,
                    "replace",
                    side_effect=track_replace,
                ),
            ):
                created = module.create_databridge_generation(
                    _current_dataset(),
                    native_generation=native,
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    output_root=output_root,
                    schema_path=SCHEMA_PATH,
                )

            replace_index = events.index(("replace", None))
            before_replace = [
                path
                for event, path in events[:replace_index]
                if event == "fsync_dir"
            ]
            self.assertTrue(
                any(path is not None and path.name == "data"
                    for path in before_replace)
            )
            self.assertTrue(
                any(
                    path is not None
                    and path.name.startswith(".building-")
                    for path in before_replace
                )
            )
            self.assertIn(
                ("fsync_dir", output_root),
                events[replace_index + 1 :],
            )
            self.assertTrue(created.manifest_path.is_file())

    def test_atomic_publish_renames_a_complete_generation_once(self) -> None:
        from shared import databridge_input_generation as module

        native = _native_cutoff_context()
        publication_count = 0
        real_replace = os.replace

        def track_replace(source: object, destination: object) -> None:
            nonlocal publication_count
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                source_path.name.startswith(".building-")
                and destination_path.name.startswith("databridge-")
            ):
                publication_count += 1
                self.assertTrue(
                    (source_path / "manifest.json").is_file(),
                    "DataBridge final directory was published without manifest",
                )
                module.open_databridge_generation(
                    source_path / "manifest.json",
                    expected_generation_id=destination_path.name,
                    expected_business_date="2026-07-24",
                    expected_feature_date="2026-07-23",
                    schema_path=SCHEMA_PATH,
                )
            real_replace(source, destination)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                module.os,
                "replace",
                side_effect=track_replace,
            ):
                created = module.create_databridge_generation(
                    _current_dataset(),
                    native_generation=native,
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    output_root=Path(tmpdir),
                    schema_path=SCHEMA_PATH,
                )

            self.assertEqual(publication_count, 1)
            self.assertTrue(created.manifest_path.is_file())

    def test_post_rename_failure_preserves_complete_final_generation(
        self,
    ) -> None:
        from shared import databridge_input_generation as module

        native = _native_cutoff_context()
        real_replace = os.replace
        real_open = module.open_databridge_generation
        published = False

        def track_replace(source: object, destination: object) -> None:
            nonlocal published
            source_path = Path(source)
            destination_path = Path(destination)
            real_replace(source, destination)
            if (
                source_path.name.startswith(".building-")
                and destination_path.name.startswith("databridge-")
            ):
                published = True

        def fail_final_reopen(path: Path, **kwargs: object):
            if (
                published
                and Path(path).parent.name.startswith("databridge-")
            ):
                raise OSError("injected post-rename reopen failure")
            return real_open(path, **kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir)
            with (
                patch.object(
                    module.os,
                    "replace",
                    side_effect=track_replace,
                ),
                patch.object(
                    module,
                    "open_databridge_generation",
                    side_effect=fail_final_reopen,
                ),
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "post-rename reopen failure",
                ):
                    module.create_databridge_generation(
                        _current_dataset(),
                        native_generation=native,
                        business_date="2026-07-24",
                        feature_date="2026-07-23",
                        output_root=output_root,
                        schema_path=SCHEMA_PATH,
                    )

            finals = [
                entry
                for entry in output_root.iterdir()
                if entry.name.startswith("databridge-")
            ]
            self.assertEqual(len(finals), 1)
            reopened = module.open_databridge_generation(
                finals[0] / "manifest.json",
                expected_generation_id=finals[0].name,
                expected_business_date="2026-07-24",
                expected_feature_date="2026-07-23",
                schema_path=SCHEMA_PATH,
            )
            self.assertEqual(reopened.generation_id, finals[0].name)

    def test_post_rename_root_fsync_failure_is_not_reported_as_success(
        self,
    ) -> None:
        from shared import databridge_input_generation as module

        native = _native_cutoff_context()
        real_replace = os.replace
        real_fsync_directory = module._fsync_directory
        published = False

        def track_replace(source: object, destination: object) -> None:
            nonlocal published
            real_replace(source, destination)
            if (
                Path(source).name.startswith(".building-")
                and Path(destination).name.startswith("databridge-")
            ):
                published = True

        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir)

            def fail_root_fsync(path: Path) -> None:
                if published and Path(path) == output_root:
                    raise OSError("injected root fsync failure")
                real_fsync_directory(path)

            with (
                patch.object(
                    module.os,
                    "replace",
                    side_effect=track_replace,
                ),
                patch.object(
                    module,
                    "_fsync_directory",
                    side_effect=fail_root_fsync,
                ),
                self.assertRaisesRegex(
                    OSError,
                    "root fsync failure",
                ),
            ):
                module.create_databridge_generation(
                    _current_dataset(),
                    native_generation=native,
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    output_root=output_root,
                    schema_path=SCHEMA_PATH,
                )

            finals = [
                entry
                for entry in output_root.iterdir()
                if entry.name.startswith("databridge-")
            ]
            self.assertEqual(len(finals), 1)
            module.open_databridge_generation(
                finals[0] / "manifest.json",
                expected_generation_id=finals[0].name,
                schema_path=SCHEMA_PATH,
            )

    def test_legacy_caller_supplied_retention_is_disabled(
        self,
    ) -> None:
        from shared import databridge_input_generation as module

        pruner = getattr(module, "prune_databridge_generations", None)
        self.assertIsNotNone(pruner)
        if pruner is None:
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            native = _native_cutoff_context()
            context = module.create_databridge_generation(
                _current_dataset(),
                native_generation=native,
                business_date="2026-07-24",
                feature_date="2026-07-23",
                output_root=Path(tmpdir),
                schema_path=SCHEMA_PATH,
            )
            with self.assertRaisesRegex(RuntimeError, "DB-resolved"):
                pruner(
                    tmpdir,
                    schema_path=SCHEMA_PATH,
                    bound_generation_ids=set(),
                    min_free_bytes=0,
                )
            self.assertTrue(context.root_dir.is_dir())

    def test_db_resolved_exact_delete_rehashes_before_removal(self) -> None:
        from shared import databridge_input_generation as module

        deleter = getattr(
            module,
            "delete_reclaimable_databridge_generation",
            None,
        )
        self.assertIsNotNone(deleter)
        if deleter is None:
            return

        native = _native_cutoff_context()
        with tempfile.TemporaryDirectory() as tmpdir:
            context = module.create_databridge_generation(
                _current_dataset(),
                native_generation=native,
                business_date="2026-07-24",
                feature_date="2026-07-23",
                output_root=Path(tmpdir),
                schema_path=SCHEMA_PATH,
            )
            with self.assertRaisesRegex(ValueError, "manifest .* mismatch"):
                deleter(
                    tmpdir,
                    schema_path=SCHEMA_PATH,
                    generation_id=context.generation_id,
                    manifest_sha256="f" * 64,
                    business_date=context.business_date,
                    feature_date=context.feature_date,
                )
            self.assertTrue(context.root_dir.is_dir())

            self.assertTrue(
                deleter(
                    tmpdir,
                    schema_path=SCHEMA_PATH,
                    generation_id=context.generation_id,
                    manifest_sha256=context.manifest_sha256,
                    business_date=context.business_date,
                    feature_date=context.feature_date,
                )
            )
            self.assertFalse(context.root_dir.exists())
            self.assertFalse(
                deleter(
                    tmpdir,
                    schema_path=SCHEMA_PATH,
                    generation_id=context.generation_id,
                    manifest_sha256=context.manifest_sha256,
                    business_date=context.business_date,
                    feature_date=context.feature_date,
                )
            )

    def test_storage_preflight_enforces_total_quota_and_free_watermark(
        self,
    ) -> None:
        from shared import databridge_input_generation as module

        preflight = getattr(
            module,
            "preflight_databridge_generation_storage",
            None,
        )
        self.assertIsNotNone(preflight)
        if preflight is None:
            return
        self.assertIn(
            "reserve_bytes",
            inspect.signature(preflight).parameters,
        )
        self.assertIn(
            "reserve_generation_count",
            inspect.signature(preflight).parameters,
        )

        native = _native_cutoff_context()
        with tempfile.TemporaryDirectory() as tmpdir:
            context = module.create_databridge_generation(
                _current_dataset(),
                native_generation=native,
                business_date="2026-07-24",
                feature_date="2026-07-23",
                output_root=Path(tmpdir),
                schema_path=SCHEMA_PATH,
            )
            with self.assertRaisesRegex(OSError, "quota"):
                preflight(
                    tmpdir,
                    min_free_bytes=0,
                    max_total_bytes=1,
                    max_generation_count=1,
                )
            with self.assertRaisesRegex(OSError, "generation count"):
                preflight(
                    tmpdir,
                    min_free_bytes=0,
                    max_total_bytes=10**9,
                    max_generation_count=0,
                )
            with (
                patch.object(
                    module.shutil,
                    "disk_usage",
                    return_value=shutil._ntuple_diskusage(
                        total=100,
                        used=99,
                        free=1,
                    ),
                ),
                self.assertRaisesRegex(OSError, "watermark"),
            ):
                preflight(
                    tmpdir,
                    min_free_bytes=2,
                    max_total_bytes=10**9,
                    max_generation_count=1,
                )
            result = preflight(
                tmpdir,
                min_free_bytes=0,
                max_total_bytes=10**9,
                max_generation_count=1,
            )
            self.assertGreaterEqual(result["total_bytes"], 1)
            self.assertEqual(result["generation_count"], 1)
            self.assertTrue(context.root_dir.is_dir())
            with self.assertRaisesRegex(OSError, "quota"):
                preflight(
                    tmpdir,
                    min_free_bytes=0,
                    max_total_bytes=result["total_bytes"],
                    max_generation_count=2,
                    reserve_bytes=1,
                )
            with self.assertRaisesRegex(OSError, "generation count"):
                preflight(
                    tmpdir,
                    min_free_bytes=0,
                    max_total_bytes=10**9,
                    max_generation_count=1,
                    reserve_generation_count=1,
                )

    def test_create_checks_actual_staging_bytes_before_publish(self) -> None:
        from shared import databridge_input_generation as module

        signature = inspect.signature(module.create_databridge_generation)
        self.assertIn("max_total_bytes", signature.parameters)
        self.assertIn("min_free_bytes", signature.parameters)
        native = _native_cutoff_context()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaisesRegex(OSError, "quota"):
                module.create_databridge_generation(
                    _current_dataset(),
                    native_generation=native,
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    output_root=root,
                    schema_path=SCHEMA_PATH,
                    max_total_bytes=1,
                    min_free_bytes=0,
                )
            self.assertEqual(list(root.glob("databridge-*")), [])
            self.assertEqual(list(root.glob(".building-*")), [])

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with (
                patch.object(
                    module.shutil,
                    "disk_usage",
                    return_value=shutil._ntuple_diskusage(
                        total=100,
                        used=100,
                        free=0,
                    ),
                ),
                self.assertRaisesRegex(OSError, "watermark"),
            ):
                module.create_databridge_generation(
                    _current_dataset(),
                    native_generation=native,
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    output_root=root,
                    schema_path=SCHEMA_PATH,
                    max_total_bytes=10**9,
                    min_free_bytes=1,
                )
            self.assertEqual(list(root.glob("databridge-*")), [])
            self.assertEqual(list(root.glob(".building-*")), [])

    def test_create_existing_content_addressed_final_is_not_double_counted(
        self,
    ) -> None:
        from shared import databridge_input_generation as module

        native = _native_cutoff_context()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = module.create_databridge_generation(
                _current_dataset(),
                native_generation=native,
                business_date="2026-07-24",
                feature_date="2026-07-23",
                output_root=root,
                schema_path=SCHEMA_PATH,
            )
            used_bytes = sum(
                path.stat().st_size
                for path in root.rglob("*")
                if path.is_file()
            )

            second = module.create_databridge_generation(
                _current_dataset(),
                native_generation=native,
                business_date="2026-07-24",
                feature_date="2026-07-23",
                output_root=root,
                schema_path=SCHEMA_PATH,
                max_total_bytes=used_bytes,
                min_free_bytes=0,
            )

            self.assertEqual(second.generation_id, first.generation_id)
            self.assertEqual(
                [path.name for path in root.glob("databridge-*")],
                [first.generation_id],
            )
            self.assertEqual(list(root.glob(".building-*")), [])


class DataBridgeInputGenerationRecoveryScanTests(unittest.TestCase):
    def test_create_makes_new_storage_root_private_and_open_requires_it(
        self,
    ) -> None:
        from shared import databridge_input_generation as module

        native = _native_cutoff_context()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = (
                Path(tmpdir) / "nested" / "databridge-generations"
            )
            context = module.create_databridge_generation(
                _current_dataset(),
                native_generation=native,
                business_date="2026-07-24",
                feature_date="2026-07-23",
                output_root=output_root,
                schema_path=SCHEMA_PATH,
            )
            self.assertEqual(
                stat.S_IMODE(output_root.lstat().st_mode),
                0o700,
            )
            output_root.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "private"):
                module.open_databridge_generation(
                    context.manifest_path,
                    schema_path=SCHEMA_PATH,
                )

    def test_create_rejects_existing_nonprivate_storage_root(self) -> None:
        from shared import databridge_input_generation as module

        native = _native_cutoff_context()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir) / "databridge-generations"
            output_root.mkdir(mode=0o755)
            with self.assertRaisesRegex(ValueError, "private"):
                module.create_databridge_generation(
                    _current_dataset(),
                    native_generation=native,
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    output_root=output_root,
                    schema_path=SCHEMA_PATH,
                )

    def test_owner_cleanup_removes_building_and_gc_debris(self) -> None:
        from shared import databridge_input_generation as module

        cleaner = getattr(
            module,
            "cleanup_databridge_generation_debris",
            None,
        )
        self.assertIsNotNone(cleaner)
        if cleaner is None:
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            building = root / ".building-interrupted"
            data = building / "data"
            data.mkdir(parents=True)
            (data / "partial.csv").write_text(
                "partial",
                encoding="utf-8",
            )
            tombstone = root / (
                f".gc-databridge-{'a' * 24}-{'b' * 32}"
            )
            tombstone_data = tombstone / "data"
            tombstone_data.mkdir(parents=True)
            (tombstone / "manifest.json").write_text(
                "{}",
                encoding="utf-8",
            )

            removed = cleaner(root)

            self.assertEqual(
                removed,
                (building.name, tombstone.name),
            )
            self.assertFalse(building.exists())
            self.assertFalse(tombstone.exists())

    def test_owner_cleanup_rejects_symlink_and_non_private_root(self) -> None:
        from shared import databridge_input_generation as module

        cleaner = getattr(
            module,
            "cleanup_databridge_generation_debris",
            None,
        )
        self.assertIsNotNone(cleaner)
        if cleaner is None:
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            outside = root / "outside"
            outside.mkdir()
            unsafe = root / ".building-symlink"
            unsafe.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                cleaner(root)
            self.assertTrue(outside.is_dir())

            unsafe.unlink()
            root.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "private"):
                cleaner(root)

    def test_scan_ignores_partial_staging_and_returns_zero_or_one(
        self,
    ) -> None:
        from shared import databridge_input_generation as module

        scanner = getattr(
            module,
            "find_published_databridge_generation",
            None,
        )
        self.assertIsNotNone(scanner)
        if scanner is None:
            return
        native = _native_cutoff_context()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            partial = root / ".building-interrupted"
            partial.mkdir()
            (partial / "payload.tmp").write_text(
                "partial",
                encoding="utf-8",
            )

            self.assertIsNone(
                scanner(
                    root,
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    native_generation=native,
                    schema_path=SCHEMA_PATH,
                )
            )
            created = module.create_databridge_generation(
                _current_dataset(),
                native_generation=native,
                business_date="2026-07-24",
                feature_date="2026-07-23",
                output_root=root,
                schema_path=SCHEMA_PATH,
            )
            recovered = scanner(
                root,
                business_date="2026-07-24",
                feature_date="2026-07-23",
                native_generation=native,
                schema_path=SCHEMA_PATH,
            )

            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(recovered.generation_id, created.generation_id)
            self.assertEqual(
                recovered.manifest_sha256,
                created.manifest_sha256,
            )

    def test_scan_rejects_ambiguous_same_day_generations(self) -> None:
        from shared import databridge_input_generation as module

        scanner = getattr(
            module,
            "find_published_databridge_generation",
            None,
        )
        self.assertIsNotNone(scanner)
        if scanner is None:
            return
        native = _native_cutoff_context()

        with tempfile.TemporaryDirectory() as tmpdir:
            for suffix in ("a", "b"):
                module.create_databridge_generation(
                    _current_dataset(
                        upstream_generation_id=f"upstream-{suffix}"
                    ),
                    native_generation=native,
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    output_root=Path(tmpdir),
                    schema_path=SCHEMA_PATH,
                )

            with self.assertRaisesRegex(ValueError, "ambiguous"):
                scanner(
                    tmpdir,
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    native_generation=native,
                    schema_path=SCHEMA_PATH,
                )

    def test_scan_rejects_native_relation_mismatch(self) -> None:
        from shared import databridge_input_generation as module

        scanner = getattr(
            module,
            "find_published_databridge_generation",
            None,
        )
        self.assertIsNotNone(scanner)
        if scanner is None:
            return
        first_native = _native_cutoff_context()
        second_native = _native_cutoff_context()

        with tempfile.TemporaryDirectory() as tmpdir:
            created = module.create_databridge_generation(
                _current_dataset(),
                native_generation=first_native,
                business_date="2026-07-24",
                feature_date="2026-07-23",
                output_root=Path(tmpdir),
                schema_path=SCHEMA_PATH,
            )
            self.assertNotEqual(
                created.native_manifest_sha256,
                second_native.manifest_sha256,
            )

            with self.assertRaisesRegex(ValueError, "Native relation"):
                scanner(
                    tmpdir,
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    native_generation=second_native,
                    schema_path=SCHEMA_PATH,
                )

    def test_scan_rejects_final_looking_directory_without_manifest(
        self,
    ) -> None:
        from shared import databridge_input_generation as module

        scanner = getattr(
            module,
            "find_published_databridge_generation",
            None,
        )
        self.assertIsNotNone(scanner)
        if scanner is None:
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            invalid = Path(tmpdir) / f"databridge-{'a' * 24}"
            invalid.mkdir()
            (invalid / "data").mkdir()

            with self.assertRaisesRegex(ValueError, "manifest|entries"):
                scanner(
                    tmpdir,
                    business_date="2026-07-24",
                    feature_date="2026-07-23",
                    native_generation=_native_cutoff_context(),
                    schema_path=SCHEMA_PATH,
                )


if __name__ == "__main__":
    unittest.main()
