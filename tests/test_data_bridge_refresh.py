from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd


class DataBridgeRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self._mode_patcher = patch.dict(
            os.environ,
            {"BOND_DAILY_COORDINATOR_MODE": "legacy"},
        )
        self._mode_patcher.start()

    def tearDown(self) -> None:
        self._mode_patcher.stop()

    def test_refresh_config_defaults_to_v2_preflight_timeline(self) -> None:
        from shared.data_bridge.refresh import DataBridgeRefreshConfig

        with patch.dict("os.environ", {}, clear=True):
            config = DataBridgeRefreshConfig.from_env()

        self.assertEqual(config.refresh_start, "06:30")
        self.assertEqual(config.refresh_deadline, "06:55")

    def test_exclusive_publish_waits_for_snapshot_shared_lock(self) -> None:
        from shared.data_bridge.refresh import DataBridgeStore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = DataBridgeStore(data_root=root / "data", runtime_root=root / "runtime")
            store.publish(_write_candidate(root / "old", "old"), _state("old"))
            candidate = _write_candidate(root / "new", "new")
            started = threading.Event()
            finished = threading.Event()

            def publish() -> None:
                started.set()
                store.publish(candidate, _state("new"))
                finished.set()

            with store.lock(exclusive=False):
                thread = threading.Thread(target=publish)
                thread.start()
                self.assertTrue(started.wait(timeout=1))
                time.sleep(0.05)
                self.assertFalse(finished.is_set())
            thread.join(timeout=2)

            self.assertTrue(finished.is_set())
            self.assertIn("new", (store.current_dir / "daily_output.csv").read_text())

    def test_refresh_config_rejects_more_than_four_downloads(self) -> None:
        from shared.data_bridge.refresh import DataBridgeRefreshConfig

        with patch.dict("os.environ", {"DATABRIDGE_DOWNLOAD_CONCURRENCY": "5"}):
            with self.assertRaisesRegex(ValueError, "at most 4"):
                DataBridgeRefreshConfig.from_env()

    def test_refresh_rejects_missing_source_tables(self) -> None:
        from shared.data_bridge.refresh import DataBridgeRefreshConfig, DataBridgeRefreshError, run_full_refresh

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=_write_schema(root),
            )
            with self.assertRaisesRegex(DataBridgeRefreshError, "required tables"):
                run_full_refresh(
                    client=_FakeClient(table_names={"api_wind_daily"}),
                    config=config,
                    expected_daily_date="2026-07-18",
                    refresh_date="2026-07-19",
                    publish=False,
                )

    def test_refresh_refuses_to_start_after_deadline(self) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeRefreshError,
            run_full_refresh,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=_write_schema(root),
            )
            with self.assertRaisesRegex(DataBridgeRefreshError, "deadline"):
                run_full_refresh(
                    client=_FakeClient(),
                    config=config,
                    expected_daily_date="2026-07-18",
                    refresh_date="2026-07-19",
                    publish=True,
                    deadline_at=datetime(2000, 1, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
                )

    def test_ledger_publish_requires_coordinator_scope(self) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeRefreshError,
            run_full_refresh,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            client = _FakeClient()
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=_write_schema(root),
            )
            with (
                patch.dict(
                    os.environ,
                    {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
                ),
                self.assertRaisesRegex(
                    DataBridgeRefreshError,
                    "coordinator",
                ),
            ):
                run_full_refresh(
                    client=client,
                    config=config,
                    expected_daily_date="2026-07-18",
                    refresh_date="2026-07-19",
                    publish=True,
                )

            self.assertEqual(client.calls, [])

    def test_ledger_dry_run_requires_coordinator_scope(self) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeRefreshError,
            run_full_refresh,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            client = _FakeClient()
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=_write_schema(root),
            )
            with (
                patch.dict(
                    os.environ,
                    {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
                ),
                self.assertRaisesRegex(
                    DataBridgeRefreshError,
                    "coordinator",
                ),
            ):
                run_full_refresh(
                    client=client,
                    config=config,
                    expected_daily_date="2026-07-18",
                    refresh_date="2026-07-19",
                    publish=False,
                )

            self.assertEqual(client.calls, [])

    def test_machine_ledger_rejects_env_legacy_without_capability(
        self,
    ) -> None:
        from shared.data_bridge.refresh import (
            DataBridgePublicationFenceError,
            _require_publish_authority,
        )

        with (
            patch.dict(
                os.environ,
                {"BOND_DAILY_COORDINATOR_MODE": "legacy"},
            ),
            patch(
                "shared.data_bridge.refresh."
                "require_current_daily_coordinator_identity",
                return_value=SimpleNamespace(mode="ledger"),
            ),
            self.assertRaises(DataBridgePublicationFenceError),
        ):
            _require_publish_authority(publish=True)

    def test_env_ledger_cannot_override_machine_legacy_identity_error(
        self,
    ) -> None:
        from shared.data_bridge.refresh import (
            DataBridgePublicationFenceError,
            _require_publish_authority,
        )

        with (
            patch.dict(
                os.environ,
                {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
            ),
            patch(
                "shared.data_bridge.refresh."
                "require_current_daily_coordinator_identity",
                side_effect=RuntimeError(
                    "process mode does not match current epoch mode legacy"
                ),
            ),
            self.assertRaises(DataBridgePublicationFenceError),
        ):
            _require_publish_authority(publish=True)

    def test_machine_chain_ledger_ignores_repository_rollout_legacy(
        self,
    ) -> None:
        from shared.data_bridge.refresh import (
            DataBridgePublicationFenceError,
            _require_publish_authority,
        )

        identity = SimpleNamespace(
            mode="ledger",
            source="epoch_chain",
        )
        with (
            patch(
                "shared.data_bridge.refresh."
                "require_current_daily_coordinator_identity",
                return_value=identity,
            ),
            self.assertRaises(DataBridgePublicationFenceError),
        ):
            _require_publish_authority(publish=False)

    def test_publication_capability_revalidates_exact_occurrence_identity(
        self,
    ) -> None:
        from shared.data_bridge.refresh import (
            DailyCoordinatorPublicationCapability,
            DataBridgePublicationFenceError,
            _validate_publication_capability,
        )

        occurrence = {
            "occurrence_id": 42,
            "business_date": "2026-07-24",
            "daily_coordinator_epoch": {
                "epoch": 2,
                "mode": "ledger",
                "record_sha256": "2" * 64,
            },
        }
        capability = DailyCoordinatorPublicationCapability(
            occurrence_id=42,
            business_date="2026-07-24",
            epoch=2,
            mode="ledger",
            record_sha256="2" * 64,
        )
        object.__setattr__(
            capability,
            "occurrence_validator",
            lambda: dict(occurrence),
        )

        with patch(
            "shared.data_bridge.refresh."
            "assert_daily_coordinator_epoch_payload_matches_current",
        ):
            self.assertIs(
                _validate_publication_capability(capability),
                capability,
            )
            occurrence["occurrence_id"] = 43
            with self.assertRaisesRegex(
                DataBridgePublicationFenceError,
                "occurrence",
            ):
                _validate_publication_capability(capability)

    def test_store_lock_epoch_drift_preserves_current_previous_and_state(
        self,
    ) -> None:
        from shared.data_bridge.refresh import (
            DailyCoordinatorPublicationCapability,
            DataBridgePublicationFenceError,
            DataBridgeStore,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = DataBridgeStore(
                data_root=root / "data",
                runtime_root=root / "runtime",
            )
            store.publish(
                _write_candidate(root / "old", "old"),
                _state("old"),
            )
            current_before = {
                path.name: path.read_bytes()
                for path in store.current_dir.iterdir()
            }
            state_before = store.state_path.read_bytes()
            candidate = _write_candidate(root / "new", "new")
            caller_state = _state("new")
            caller_state_before = json.loads(json.dumps(caller_state))
            capability = DailyCoordinatorPublicationCapability(
                occurrence_id=42,
                business_date="2026-07-24",
                epoch=2,
                mode="ledger",
                record_sha256="2" * 64,
            )
            drift = DataBridgePublicationFenceError("epoch drift")

            with (
                patch(
                    "shared.data_bridge.refresh."
                    "require_current_daily_coordinator_identity",
                    return_value=SimpleNamespace(mode="ledger"),
                ),
                patch(
                    "shared.data_bridge.refresh."
                    "_validate_publication_capability",
                    side_effect=(capability, drift),
                ),
                patch(
                    "shared.data_bridge.refresh.os.replace",
                    wraps=os.replace,
                ) as replace,
                patch(
                    "shared.data_bridge.refresh.shutil.rmtree",
                    wraps=__import__("shutil").rmtree,
                ) as remove,
                self.assertRaises(DataBridgePublicationFenceError),
            ):
                store.publish(
                    candidate,
                    caller_state,
                    publication_capability=capability,
                )

            replace.assert_not_called()
            remove.assert_not_called()
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in store.current_dir.iterdir()
                },
                current_before,
            )
            self.assertFalse(store.previous_dir.exists())
            self.assertEqual(store.state_path.read_bytes(), state_before)
            self.assertEqual(caller_state, caller_state_before)

    def test_post_stability_epoch_drift_never_publishes_or_writes_state(
        self,
    ) -> None:
        from shared.data_bridge.refresh import (
            DailyCoordinatorPublicationCapability,
            DataBridgePublicationFenceError,
            DataBridgeRefreshConfig,
            DataBridgeStore,
            run_full_refresh,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=_write_schema(root),
            )
            capability = DailyCoordinatorPublicationCapability(
                occurrence_id=42,
                business_date="2026-07-19",
                epoch=2,
                mode="ledger",
                record_sha256="2" * 64,
            )
            drift = DataBridgePublicationFenceError("epoch drift")
            with (
                patch(
                    "shared.data_bridge.refresh."
                    "require_current_daily_coordinator_identity",
                    return_value=SimpleNamespace(mode="ledger"),
                ),
                patch(
                    "shared.data_bridge.refresh."
                    "_validate_publication_capability",
                    side_effect=(capability, capability, drift),
                ),
                patch.object(
                    DataBridgeStore,
                    "publish",
                ) as publish,
                patch.object(
                    DataBridgeStore,
                    "record_failed_attempt",
                ) as record_failure,
                self.assertRaises(DataBridgePublicationFenceError),
            ):
                run_full_refresh(
                    client=_FakeClient(),
                    config=config,
                    expected_daily_date="2026-07-18",
                    refresh_date="2026-07-19",
                    publish=True,
                    publication_capability=capability,
                )

            publish.assert_not_called()
            record_failure.assert_not_called()
            self.assertFalse(
                (config.runtime_root / "state.json").exists()
            )

    def test_outer_refresh_lock_rechecks_epoch_before_recovery(
        self,
    ) -> None:
        from shared.data_bridge.refresh import (
            DailyCoordinatorPublicationCapability,
            DataBridgePublicationFenceError,
            DataBridgeRefreshConfig,
            DataBridgeStore,
            run_full_refresh,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=_write_schema(root),
            )
            client = _FakeClient()
            capability = DailyCoordinatorPublicationCapability(
                occurrence_id=42,
                business_date="2026-07-19",
                epoch=2,
                mode="ledger",
                record_sha256="2" * 64,
            )
            with (
                patch(
                    "shared.data_bridge.refresh."
                    "require_current_daily_coordinator_identity",
                    return_value=SimpleNamespace(mode="ledger"),
                ),
                patch(
                    "shared.data_bridge.refresh."
                    "_validate_publication_capability",
                    side_effect=(
                        capability,
                        DataBridgePublicationFenceError("epoch drift"),
                    ),
                ),
                patch.object(DataBridgeStore, "recover") as recover,
                self.assertRaises(DataBridgePublicationFenceError),
            ):
                run_full_refresh(
                    client=client,
                    config=config,
                    expected_daily_date="2026-07-18",
                    refresh_date="2026-07-19",
                    publish=True,
                    publication_capability=capability,
                )

            recover.assert_not_called()
            self.assertEqual(client.calls, [])
            self.assertFalse(
                (config.runtime_root / "state.json").exists()
            )

    def test_post_state_failure_restores_previous_dataset_and_state(
        self,
    ) -> None:
        from shared.data_bridge.refresh import DataBridgeStore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = DataBridgeStore(
                data_root=root / "data",
                runtime_root=root / "runtime",
            )
            store.publish(
                _write_candidate(root / "old", "old"),
                _state("old"),
            )
            current_before = {
                path.name: path.read_bytes()
                for path in store.current_dir.iterdir()
            }
            state_before = store.state_path.read_bytes()
            original_fsync = __import__(
                "shared.data_bridge.refresh",
                fromlist=["_fsync_directory"],
            )._fsync_directory
            fsync_calls = 0

            def fail_after_state(directory: Path) -> None:
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 2:
                    raise OSError("injected fsync failure after state")
                original_fsync(directory)

            with (
                patch(
                    "shared.data_bridge.refresh._fsync_directory",
                    side_effect=fail_after_state,
                ),
                self.assertRaisesRegex(OSError, "after state"),
            ):
                store.publish(
                    _write_candidate(root / "new", "new"),
                    _state("new"),
                )

            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in store.current_dir.iterdir()
                },
                current_before,
            )
            self.assertFalse(store.previous_dir.exists())
            self.assertEqual(store.state_path.read_bytes(), state_before)

    def test_repository_bootstrap_legacy_does_not_require_env_mode(
        self,
    ) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeRefreshError,
            run_full_refresh,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            client = _FakeClient()
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=_write_schema(root),
            )
            with patch.dict(os.environ, {}, clear=True):
                result = run_full_refresh(
                    client=client,
                    config=config,
                    expected_daily_date="2026-07-18",
                    refresh_date="2026-07-19",
                    publish=True,
                )

            self.assertTrue(result.published)
            self.assertGreater(len(client.calls), 0)

    def test_second_refresh_fails_busy_without_touching_first_staging(self) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeRefreshError,
            DataBridgeStore,
            run_full_refresh,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=_write_schema(root),
                daily_start_date="2026-01-01",
                download_concurrency=2,
            )
            store = DataBridgeStore(
                data_root=config.data_root,
                runtime_root=config.runtime_root,
            )
            staging = config.runtime_root / "staging"
            config.runtime_root.mkdir(parents=True, mode=0o700)
            staging.mkdir(mode=0o700)
            sentinel = staging / "owned-by-first-refresh"
            sentinel.write_text("do-not-delete", encoding="utf-8")
            client = _FakeClient()
            finished = threading.Event()
            errors: list[BaseException] = []

            def competing_refresh() -> None:
                try:
                    run_full_refresh(
                        client=client,
                        config=config,
                        expected_daily_date="2026-07-18",
                        refresh_date="2026-07-19",
                        publish=False,
                    )
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    finished.set()

            with store.lock(exclusive=True):
                thread = threading.Thread(target=competing_refresh)
                thread.start()
                completed_while_busy = finished.wait(timeout=0.2)
            thread.join(timeout=5)

            self.assertTrue(completed_while_busy)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], DataBridgeRefreshError)
            self.assertIn("busy", str(errors[0]).lower())
            self.assertTrue(sentinel.is_file())
            self.assertEqual(client.calls, [])

    def test_quarter_ranges_are_complete_and_non_overlapping(self) -> None:
        from shared.data_bridge.refresh import quarter_ranges

        self.assertEqual(
            quarter_ranges("2026-01-01", "2026-07-19", months=3),
            [
                ("2026-01-01", "2026-03-31"),
                ("2026-04-01", "2026-06-30"),
                ("2026-07-01", "2026-07-19"),
            ],
        )

    def test_uses_second_round_when_two_consecutive_digests_match(self) -> None:
        from shared.data_bridge.refresh import select_stable_round

        rounds = [_round("a", "one"), _round("b", "same"), _round("c", "same")]
        selected = select_stable_round(iter(rounds), max_rounds=3)

        self.assertEqual(selected.round_id, "c")

    def test_fails_when_three_rounds_never_stabilize(self) -> None:
        from shared.data_bridge.refresh import DataBridgeRefreshError, select_stable_round

        rounds = [_round("a", "one"), _round("b", "two"), _round("c", "three")]
        with self.assertRaisesRegex(DataBridgeRefreshError, "consecutive rounds"):
            select_stable_round(iter(rounds), max_rounds=3)

    def test_publish_replaces_all_files_and_failure_preserves_old_current(self) -> None:
        from shared.data_bridge.refresh import DataBridgeStore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = DataBridgeStore(data_root=root / "data", runtime_root=root / "runtime")
            old = _write_candidate(root / "old", "old")
            new = _write_candidate(root / "new", "new")
            store.publish(old, _state("old"))
            old_hashes = _hashes(store.current_dir)

            with self.assertRaises(RuntimeError):
                store.publish(new, _state("new"), fail_after_backup=True)

            self.assertEqual(_hashes(store.current_dir), old_hashes)
            store.publish(new, _state("new"))
            self.assertIn("new", (store.current_dir / "daily_output.csv").read_text())
            self.assertEqual(json.loads(store.state_path.read_text())["generation_id"], "new")

    def test_publish_fsyncs_copied_payload_before_current_rename(self) -> None:
        from shared.data_bridge import refresh as module

        events: list[tuple[str, Path]] = []
        real_replace = os.replace
        real_fsync_directory = module._fsync_directory

        def fsync_file(path: Path) -> None:
            normalized = Path(path)
            events.append(("fsync_file", normalized))
            descriptor = os.open(normalized, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

        def fsync_directory(path: Path) -> None:
            normalized = Path(path)
            events.append(("fsync_dir", normalized))
            real_fsync_directory(normalized)

        def replace(source: object, destination: object) -> None:
            events.append(("replace", Path(destination)))
            real_replace(source, destination)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = module.DataBridgeStore(
                data_root=root / "data",
                runtime_root=root / "runtime",
            )
            with (
                patch.object(
                    module,
                    "_fsync_regular_file",
                    side_effect=fsync_file,
                    create=True,
                ),
                patch.object(
                    module,
                    "_fsync_directory",
                    side_effect=fsync_directory,
                ),
                patch.object(
                    module.os,
                    "replace",
                    side_effect=replace,
                ),
            ):
                store.publish(
                    _write_candidate(root / "candidate", "durable"),
                    _state("durable"),
                )

            publish_index = events.index(
                ("replace", store.current_dir)
            )
            payload_fsyncs = {
                path.name
                for event, path in events[:publish_index]
                if (
                    event == "fsync_file"
                    and path.parent.name.startswith(".current-next-")
                )
            }
            self.assertEqual(
                payload_fsyncs,
                {
                    ".publication-manifest.json",
                    "daily_output.csv",
                    "weekly_output.csv",
                    "monthly_output.csv",
                },
            )
            self.assertTrue(
                any(
                    event == "fsync_dir"
                    and path.name.startswith(".current-next-")
                    for event, path in events[:publish_index]
                )
            )

    def test_publish_creates_private_owner_roots_and_rejects_nonprivate(
        self,
    ) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshError,
            DataBridgeStore,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = DataBridgeStore(
                data_root=root / "new" / "data",
                runtime_root=root / "new" / "runtime",
            )
            store.publish(
                _write_candidate(root / "candidate", "private"),
                _state("private"),
            )
            self.assertEqual(
                stat.S_IMODE(store.data_root.lstat().st_mode),
                0o700,
            )
            self.assertEqual(
                stat.S_IMODE(store.runtime_root.lstat().st_mode),
                0o700,
            )

            unsafe_root = root / "unsafe"
            unsafe_data = unsafe_root / "data"
            unsafe_runtime = unsafe_root / "runtime"
            unsafe_data.mkdir(parents=True, mode=0o755)
            unsafe_runtime.mkdir(mode=0o700)
            unsafe_store = DataBridgeStore(
                data_root=unsafe_data,
                runtime_root=unsafe_runtime,
            )
            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "private",
            ):
                unsafe_store.publish(
                    _write_candidate(root / "candidate-2", "unsafe"),
                    _state("unsafe"),
                )

    def test_publish_rejects_symlink_candidate_file(self) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshError,
            DataBridgeStore,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidate = _write_candidate(root / "candidate", "safe")
            outside = root / "outside.csv"
            outside.write_text("outside", encoding="utf-8")
            linked = candidate / "daily_output.csv"
            linked.unlink()
            linked.symlink_to(outside)
            store = DataBridgeStore(
                data_root=root / "data",
                runtime_root=root / "runtime",
            )
            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "symlink",
            ):
                store.publish(candidate, _state("unsafe"))
            self.assertFalse(store.current_dir.exists())

    def test_owner_cleanup_removes_current_next_and_rejects_symlink(self) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshError,
            DataBridgeStore,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_root = root / "data"
            runtime_root = root / "runtime"
            data_root.mkdir(mode=0o700)
            runtime_root.mkdir(mode=0o700)
            store = DataBridgeStore(
                data_root=data_root,
                runtime_root=runtime_root,
            )
            partial = data_root / ".current-next-interrupted"
            partial.mkdir()
            (partial / "partial.csv").write_text(
                "partial",
                encoding="utf-8",
            )

            removed = store.cleanup_crash_debris()

            self.assertEqual(
                removed,
                (".current-next-interrupted",),
            )
            self.assertFalse(partial.exists())

            outside = root / "outside"
            outside.mkdir()
            unsafe = data_root / ".current-next-symlink"
            unsafe.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "symlink",
            ):
                store.cleanup_crash_debris()
            self.assertTrue(outside.is_dir())

    def test_owner_cleanup_rejects_non_private_data_root(self) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshError,
            DataBridgeStore,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_root = root / "data"
            runtime_root = root / "runtime"
            data_root.mkdir(mode=0o755)
            runtime_root.mkdir(mode=0o700)
            store = DataBridgeStore(
                data_root=data_root,
                runtime_root=runtime_root,
            )

            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "private",
            ):
                store.cleanup_crash_debris()

    def test_failed_publish_refresh_keeps_generation_and_records_attempt(self) -> None:
        from shared.data_bridge.refresh import DataBridgeStore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = DataBridgeStore(data_root=root / "data", runtime_root=root / "runtime")
            store.publish(_write_candidate(root / "old", "old"), _state("old"))

            store.record_failed_attempt(
                refresh_date="2026-07-19",
                error="source not ready",
                duration_sec=12.5,
            )

            state = store.load_state()
            self.assertEqual(state["generation_id"], "old")
            self.assertEqual(state["last_attempt"]["status"], "failed")
            self.assertEqual(state["last_attempt"]["error"], "source not ready")
            self.assertIn("old", (store.current_dir / "daily_output.csv").read_text())

    def test_recovery_restores_state_matching_previous_and_cleans_staging(self) -> None:
        from shared.data_bridge.refresh import DataBridgeStore
        from shared.data_bridge.validation import write_validated_dataset

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema = _write_schema(root)
            old_dataset = _validated_dataset(root, schema)
            changed_frames = {name: frame.copy() for name, frame in old_dataset.frames.items()}
            changed_frames["daily_output.csv"].loc[0, "factor"] = "9"
            from shared.data_bridge.validation import validate_dataset

            new_dataset = validate_dataset(changed_frames, schema_path=schema)
            store = DataBridgeStore(data_root=root / "data", runtime_root=root / "runtime")
            old_candidate = write_validated_dataset(old_dataset, root / "old")
            store.publish(old_candidate, _dataset_state(old_dataset, refresh_date="2026-07-18"))

            store.previous_dir.parent.mkdir(parents=True, exist_ok=True)
            store.current_dir.rename(store.previous_dir)
            write_validated_dataset(new_dataset, store.current_dir)
            stale = store.runtime_root / "staging" / "round-1"
            stale.mkdir(parents=True)
            (stale / "partial.csv").write_text("partial", encoding="utf-8")

            store.recover(schema_path=schema)

            self.assertFalse(store.previous_dir.exists())
            self.assertFalse((store.runtime_root / "staging").exists())
            self.assertIn(",1\n", (store.current_dir / "daily_output.csv").read_text())

    def test_recovery_uses_generation_identity_when_digests_match(
        self,
    ) -> None:
        from shared.data_bridge.refresh import DataBridgeStore
        from shared.data_bridge.validation import write_validated_dataset

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema = _write_schema(root)
            dataset = _validated_dataset(root, schema)
            candidate = write_validated_dataset(dataset, root / "candidate")
            state = _dataset_state(
                dataset,
                refresh_date="2026-07-24",
            )
            state["generation_id"] = "generation-owned"
            store = DataBridgeStore(
                data_root=root / "data",
                runtime_root=root / "runtime",
            )
            store.publish(candidate, state)
            marker_name = ".publication-manifest.json"
            self.assertTrue(
                (store.current_dir / marker_name).is_file(),
                "published current must carry its private identity marker",
            )

            os.replace(store.current_dir, store.previous_dir)
            shutil.copytree(store.previous_dir, store.current_dir)
            polluted_marker_path = store.current_dir / marker_name
            polluted_marker = json.loads(
                polluted_marker_path.read_text(encoding="utf-8")
            )
            polluted_marker["generation_id"] = "generation-polluted"
            polluted_marker_path.chmod(0o644)
            polluted_marker_path.write_text(
                json.dumps(polluted_marker),
                encoding="utf-8",
            )

            store.recover(schema_path=schema)

            recovered_marker = json.loads(
                (store.current_dir / marker_name).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                recovered_marker["generation_id"],
                "generation-owned",
            )
            self.assertFalse(store.previous_dir.exists())

    def test_recovery_bootstraps_exact_legacy_current_manifest(
        self,
    ) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeStore,
            check_current_dataset,
        )
        from shared.data_bridge.validation import write_validated_dataset

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema = _write_schema(root)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=schema,
            )
            dataset = _validated_dataset(root, schema)
            store = DataBridgeStore(
                data_root=config.data_root,
                runtime_root=config.runtime_root,
            )
            store.data_root.mkdir(parents=True, mode=0o700)
            write_validated_dataset(dataset, store.current_dir)
            store.runtime_root.mkdir(parents=True, mode=0o700)
            store.state_path.write_text(
                json.dumps(
                    _dataset_state(
                        dataset,
                        refresh_date="2026-07-24",
                    )
                ),
                encoding="utf-8",
            )

            store.recover(schema_path=schema)

            self.assertTrue(
                (
                    store.current_dir
                    / ".publication-manifest.json"
                ).is_file()
            )
            checked = check_current_dataset(
                config,
                required_refresh_date="2026-07-24",
            )
            self.assertEqual(
                checked.state["generation_id"],
                "test-generation",
            )

    def test_round_builder_downloads_and_merges_all_three_frequencies(self) -> None:
        from shared.data_bridge.refresh import DataBridgeRefreshConfig, DataBridgeRoundBuilder

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema = _write_schema(root)
            client = _FakeClient()
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=schema,
                daily_start_date="2026-01-01",
                daily_chunk_months=3,
                download_concurrency=2,
                max_rounds=3,
                round_timeout_sec=900,
            )

            result = DataBridgeRoundBuilder(client, config).build(
                "round-a",
                end_date="2026-07-19",
                expected_daily_date="2026-07-18",
                previous_keys=None,
            )

            self.assertEqual(result.dataset.files["daily_output.csv"].rows, 3)
            self.assertEqual(result.dataset.files["weekly_output.csv"].max_key, "202629")
            self.assertEqual(result.dataset.files["monthly_output.csv"].max_key, "202607")
            self.assertCountEqual(
                [call for call in client.calls if call[0] == "日"],
                [
                    ("日", "2026-01-01", "2026-03-31"),
                    ("日", "2026-04-01", "2026-06-30"),
                    ("日", "2026-07-01", "2026-07-19"),
                ],
            )
            self.assertEqual(
                {path.name for path in result.directory.iterdir()},
                {"daily_output.csv", "weekly_output.csv", "monthly_output.csv"},
            )

    def test_round_builder_rejects_conflicting_daily_boundary_rows(self) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeRefreshError,
            DataBridgeRoundBuilder,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema = _write_schema(root)
            client = _FakeClient(conflicting_duplicate=True)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=schema,
                daily_start_date="2026-01-01",
                daily_chunk_months=3,
                download_concurrency=2,
                max_rounds=3,
                round_timeout_sec=900,
            )
            with self.assertRaisesRegex(DataBridgeRefreshError, "conflicting duplicate"):
                DataBridgeRoundBuilder(client, config).build(
                    "round-a",
                    end_date="2026-07-19",
                    expected_daily_date="2026-07-18",
                    previous_keys=None,
                )

    def test_download_reader_rejects_duplicate_raw_csv_headers(self) -> None:
        from shared.data_bridge.refresh import DataBridgeRefreshError, _read_csv

        with self.assertRaisesRegex(DataBridgeRefreshError, "duplicate columns"):
            _read_csv(
                b"date,factor,factor\n2026-07-18,1,2\n",
                "daily_output.csv",
            )

    def test_daily_merge_preserves_business_column_named_like_internal_key(self) -> None:
        from shared.data_bridge.refresh import _merge_daily_payloads

        merged = _merge_daily_payloads(
            [
                b"date,__normalized_date,factor\n2026-07-17,11,1\n",
                b"date,__normalized_date,factor\n2026-07-18,12,2\n",
            ]
        )

        self.assertEqual(
            list(merged.columns),
            ["date", "__normalized_date", "factor"],
        )
        self.assertEqual(merged["__normalized_date"].tolist(), ["11", "12"])

    def test_dry_run_builds_two_rounds_without_publishing_current(self) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeStore,
            run_full_refresh,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=_write_schema(root),
                daily_start_date="2026-01-01",
                daily_chunk_months=3,
                download_concurrency=2,
                max_rounds=3,
                round_timeout_sec=900,
            )
            result = run_full_refresh(
                client=_FakeClient(),
                config=config,
                expected_daily_date="2026-07-18",
                refresh_date="2026-07-19",
                publish=False,
            )

            store = DataBridgeStore(data_root=config.data_root, runtime_root=config.runtime_root)
            self.assertFalse(store.current_dir.exists())
            self.assertFalse(store.state_path.exists())
            self.assertEqual(result.rounds_completed, 2)
            self.assertFalse(result.published)
            self.assertEqual(result.state["stability_rounds"], 2)
            self.assertEqual(result.state["source_mode"], "full_export")
            self.assertIn("refresh_started_at", result.state)
            self.assertIn("refreshed_at", result.state)
            self.assertIsNone(result.state["published_at"])

    def test_published_state_records_trusted_refresh_timeline(self) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            run_full_refresh,
        )

        timestamps = [
            datetime(
                2026,
                7,
                24,
                6,
                30,
                0,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
            datetime(
                2026,
                7,
                24,
                6,
                49,
                58,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
            datetime(
                2026,
                7,
                24,
                6,
                50,
                0,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=_write_schema(root),
                daily_start_date="2026-01-01",
                download_concurrency=2,
            )
            with patch(
                "shared.data_bridge.refresh._shanghai_now",
                side_effect=timestamps,
            ):
                result = run_full_refresh(
                    client=_FakeClient(),
                    config=config,
                    expected_daily_date="2026-07-18",
                    refresh_date="2026-07-24",
                    publish=True,
                )

            self.assertEqual(
                result.state["refresh_started_at"],
                "2026-07-24T06:30:00+08:00",
            )
            self.assertEqual(
                result.state["refreshed_at"],
                "2026-07-24T06:49:58+08:00",
            )
            self.assertEqual(
                result.state["published_at"],
                "2026-07-24T06:50:00+08:00",
            )
            self.assertEqual(result.state["source_mode"], "full_export")
            self.assertGreaterEqual(result.state["stability_rounds"], 2)

    def test_check_current_rejects_stale_or_tampered_publication(self) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeRefreshError,
            DataBridgeStore,
            check_current_dataset,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema = _write_schema(root)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=schema,
            )
            dataset = _validated_dataset(root, schema)
            candidate = root / "candidate"
            from shared.data_bridge.validation import write_validated_dataset

            write_validated_dataset(dataset, candidate)
            state = _dataset_state(dataset, refresh_date="2026-07-19")
            DataBridgeStore(data_root=config.data_root, runtime_root=config.runtime_root).publish(
                candidate,
                state,
            )

            checked = check_current_dataset(config, required_refresh_date="2026-07-19")
            self.assertEqual(checked.state["business_digest"], dataset.business_digest)
            with self.assertRaisesRegex(DataBridgeRefreshError, "refresh_date"):
                check_current_dataset(config, required_refresh_date="2026-07-20")

            current = config.data_root / "current" / "daily_output.csv"
            current.chmod(0o644)
            current.write_text(current.read_text().replace(",1\n", ",9\n"), encoding="utf-8")
            with self.assertRaisesRegex(DataBridgeRefreshError, "digest|hash"):
                check_current_dataset(config, required_refresh_date="2026-07-19")

    def test_check_current_rejects_another_refresh_winning_the_current_pointer(
        self,
    ) -> None:
        """协调器只能消费自己刚发布的精确 generation，而非随后覆盖的 current。"""
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeRefreshError,
            DataBridgeStore,
            check_current_dataset,
        )
        from shared.data_bridge.validation import (
            validate_dataset,
            write_validated_dataset,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema = _write_schema(root)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=schema,
            )
            first = _validated_dataset(root, schema)
            first_state = _dataset_state(
                first,
                refresh_date="2026-07-19",
            )
            first_state["generation_id"] = "refresh-a"
            first_candidate = write_validated_dataset(
                first,
                root / "first",
            )
            store = DataBridgeStore(
                data_root=config.data_root,
                runtime_root=config.runtime_root,
            )
            store.publish(first_candidate, first_state)

            changed = {
                name: frame.copy()
                for name, frame in first.frames.items()
            }
            changed["daily_output.csv"].loc[0, "factor"] = "9"
            second = validate_dataset(changed, schema_path=schema)
            second_state = _dataset_state(
                second,
                refresh_date="2026-07-19",
            )
            second_state["generation_id"] = "refresh-b"
            second_candidate = write_validated_dataset(
                second,
                root / "second",
            )
            store.publish(second_candidate, second_state)

            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "generation_id|business_digest",
            ):
                check_current_dataset(
                    config,
                    required_refresh_date="2026-07-19",
                    expected_generation_id="refresh-a",
                    expected_business_digest=first.business_digest,
                )

    def test_check_current_rejects_state_current_identity_drift(self) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeRefreshError,
            DataBridgeStore,
            check_current_dataset,
        )
        from shared.data_bridge.validation import write_validated_dataset

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema = _write_schema(root)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=schema,
            )
            dataset = _validated_dataset(root, schema)
            candidate = write_validated_dataset(
                dataset,
                root / "candidate",
            )
            state = _dataset_state(
                dataset,
                refresh_date="2026-07-24",
            )
            state["generation_id"] = "generation-owned"
            store = DataBridgeStore(
                data_root=config.data_root,
                runtime_root=config.runtime_root,
            )
            store.publish(candidate, state)

            drifted_state = store.load_state()
            drifted_state["generation_id"] = "generation-drifted"
            store.state_path.write_text(
                json.dumps(drifted_state),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "identity|manifest|generation",
            ):
                check_current_dataset(
                    config,
                    required_refresh_date="2026-07-24",
                )

    def test_check_current_rejects_noncanonical_manifest_json(
        self,
    ) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeRefreshError,
            DataBridgeStore,
            check_current_dataset,
        )
        from shared.data_bridge.validation import write_validated_dataset

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema = _write_schema(root)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=schema,
            )
            dataset = _validated_dataset(root, schema)
            candidate = write_validated_dataset(
                dataset,
                root / "candidate",
            )
            store = DataBridgeStore(
                data_root=config.data_root,
                runtime_root=config.runtime_root,
            )
            store.publish(
                candidate,
                _dataset_state(
                    dataset,
                    refresh_date="2026-07-24",
                ),
            )
            marker_path = (
                store.current_dir / ".publication-manifest.json"
            )
            marker = json.loads(
                marker_path.read_text(encoding="utf-8")
            )
            marker_path.chmod(0o644)
            marker_path.write_text(
                json.dumps(marker, sort_keys=True),
                encoding="utf-8",
            )
            marker_path.chmod(0o444)

            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "canonical|manifest",
            ):
                check_current_dataset(
                    config,
                    required_refresh_date="2026-07-24",
                )

    def test_publication_manifest_excludes_post_rename_published_at(
        self,
    ) -> None:
        from shared.data_bridge.refresh import DataBridgeStore
        from shared.data_bridge.validation import write_validated_dataset

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema = _write_schema(root)
            dataset = _validated_dataset(root, schema)
            candidate = write_validated_dataset(
                dataset,
                root / "candidate",
            )
            store = DataBridgeStore(
                data_root=root / "data",
                runtime_root=root / "runtime",
            )

            published = store.publish(
                candidate,
                _dataset_state(
                    dataset,
                    refresh_date="2026-07-24",
                ),
            )

            marker = json.loads(
                (
                    store.current_dir
                    / ".publication-manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertIsNotNone(published["published_at"])
            self.assertNotIn("published_at", marker)


def _round(round_id: str, digest: str):
    from shared.data_bridge.refresh import DownloadRound

    return DownloadRound(round_id=round_id, directory=Path("/tmp") / round_id, dataset=None, digest=digest)


def _write_candidate(path: Path, value: str) -> Path:
    path.mkdir(parents=True)
    for name, key in (
        ("daily_output.csv", "date"),
        ("weekly_output.csv", "week_id"),
        ("monthly_output.csv", "month_id"),
    ):
        pd.DataFrame({key: ["2026"], "factor": [value]}).to_csv(path / name, index=False)
    return path


def _state(generation_id: str) -> dict[str, object]:
    return {"schema_version": "data-bridge-v1", "generation_id": generation_id, "files": {}}


def _hashes(path: Path) -> dict[str, bytes]:
    return {item.name: item.read_bytes() for item in path.iterdir()}


def _write_schema(root: Path) -> Path:
    path = root / "schema.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "data-bridge-v1",
                "files": {
                    "daily_output.csv": {"columns": ["date", "factor"]},
                    "weekly_output.csv": {"columns": ["week_id", "factor"]},
                    "monthly_output.csv": {"columns": ["month_id", "factor"]},
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _validated_dataset(root: Path, schema: Path):
    from shared.data_bridge.validation import validate_dataset

    return validate_dataset(
        {
            "daily_output.csv": pd.DataFrame({"date": ["2026/07/18 00:00"], "factor": ["1"]}),
            "weekly_output.csv": pd.DataFrame({"week_id": ["202629"], "factor": ["2"]}),
            "monthly_output.csv": pd.DataFrame({"month_id": ["202607"], "factor": ["3"]}),
        },
        schema_path=schema,
    )


def _dataset_state(dataset, *, refresh_date: str) -> dict[str, object]:
    return {
        "schema_version": dataset.schema_version,
        "generation_id": "test-generation",
        "refresh_date": refresh_date,
        "business_digest": dataset.business_digest,
        "files": {
            filename: {
                "sha256": profile.sha256,
                "business_hash": profile.business_hash,
            }
            for filename, profile in dataset.files.items()
        },
    }


class _FakeClient:
    def __init__(
        self,
        *,
        conflicting_duplicate: bool = False,
        table_names: set[str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self.conflicting_duplicate = conflicting_duplicate
        self.table_names = table_names or {
            "api_wind_daily",
            "api_wind_derivative_daily",
            "api_wind_weekly",
            "api_wind_derivative_weekly",
            "api_wind_monthly",
            "api_wind_derivative_monthly",
        }

    def get_tables(self) -> dict[str, object]:
        return {
            "success": True,
            "tables": [{"name": name, "status": "ok"} for name in sorted(self.table_names)],
        }

    def export_csv(
        self,
        frequency: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        allow_empty: bool = False,
    ) -> bytes:
        self.calls.append((frequency, start_date, end_date))
        if frequency == "周":
            return b"week_id,factor\n202628,3\n202629,4\n"
        if frequency == "月":
            return b"month_id,factor\n202606,5\n202607,6\n"
        if start_date == end_date == "2026-07-18":
            return b"date,factor\n2026/07/18 00:00,3\n"
        if start_date == "2026-01-01":
            return b"date,factor\n2026/01/02 00:00,1\n2026/04/01 00:00,2\n"
        if start_date == "2026-04-01":
            value = b"9" if self.conflicting_duplicate else b"2"
            return b"date,factor\n2026/04/01 00:00," + value + b"\n"
        return b"date,factor\n2026/07/18 00:00,3\n"


if __name__ == "__main__":
    unittest.main()
