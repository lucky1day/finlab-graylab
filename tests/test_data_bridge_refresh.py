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
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd


class DataBridgeRefreshTests(unittest.TestCase):
    def test_refresh_config_defaults_to_v2_preflight_timeline(self) -> None:
        from shared.data_bridge.refresh import DataBridgeRefreshConfig

        with patch.dict("os.environ", {}, clear=True):
            config = DataBridgeRefreshConfig.from_env()

        self.assertEqual(config.refresh_start, "06:30")
        self.assertEqual(config.refresh_deadline, "06:55")

    def test_refresh_config_uses_explicit_shared_paths_from_env(
        self,
    ) -> None:
        """发布 worktree 可显式指向唯一的生产 DataBridge 快照。"""
        from shared.data_bridge.refresh import DataBridgeRefreshConfig

        with patch.dict(
            "os.environ",
            {
                "DATABRIDGE_DATA_ROOT": "/srv/bond/data_bridge",
                "DATABRIDGE_RUNTIME_ROOT": "/srv/bond/data_bridge_runtime",
            },
            clear=True,
        ):
            config = DataBridgeRefreshConfig.from_env()

        self.assertEqual(config.data_root, Path("/srv/bond/data_bridge"))
        self.assertEqual(
            config.runtime_root,
            Path("/srv/bond/data_bridge_runtime"),
        )

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

    def test_local_mysql_source_token_must_stabilize_with_content(self) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshError,
            DownloadRound,
            select_stable_round,
        )

        rounds = [
            DownloadRound(
                round_id="a",
                directory=Path("/tmp/a"),
                dataset=None,
                digest="same",
                source_mode="local_mysql",
                source_provenance=_local_source_provenance(
                    "2026-07-18",
                    row_count=1,
                ),
            ),
            DownloadRound(
                round_id="b",
                directory=Path("/tmp/b"),
                dataset=None,
                digest="same",
                source_mode="local_mysql",
                source_provenance=_local_source_provenance(
                    "2026-07-18",
                    row_count=2,
                ),
            ),
        ]
        with self.assertRaisesRegex(DataBridgeRefreshError, "consecutive rounds"):
            select_stable_round(iter(rounds), max_rounds=2)

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
            self.assertEqual(state["last_attempt"]["error"], "refresh_failed")
            self.assertIn("old", (store.current_dir / "daily_output.csv").read_text())

    def test_failed_refresh_persists_safe_category_instead_of_driver_secret(
        self,
    ) -> None:
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
            )

            class SensitiveFailureClient(_FakeClient):
                def get_tables(self):
                    raise OSError("mysql://user:password=do-not-leak")

            with self.assertRaises(OSError):
                run_full_refresh(
                    client=SensitiveFailureClient(),
                    config=config,
                    expected_daily_date="2026-07-18",
                    refresh_date="2026-07-19",
                    publish=True,
                )

            state = DataBridgeStore(
                data_root=config.data_root,
                runtime_root=config.runtime_root,
            ).load_state()
            self.assertEqual(
                state["last_attempt"]["error"],
                "source_io_failed",
            )
            self.assertNotIn("do-not-leak", str(state))

    def test_first_failed_refresh_keeps_no_current_baseline_retryable(
        self,
    ) -> None:
        """首次失败的审计 state 不能被误判为损坏的已发布 current。"""
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
            )

            class FailingFirstClient(_FakeClient):
                def get_tables(self):
                    raise OSError("source unavailable")

            with self.assertRaises(OSError):
                run_full_refresh(
                    client=FailingFirstClient(),
                    config=config,
                    expected_daily_date="2026-07-18",
                    refresh_date="2026-07-19",
                    publish=True,
                )

            store = DataBridgeStore(
                data_root=config.data_root,
                runtime_root=config.runtime_root,
            )
            failed_state = store.load_state()
            self.assertEqual(set(failed_state), {"last_attempt"})
            self.assertFalse(store.current_dir.exists())

            retry = run_full_refresh(
                client=_FakeClient(),
                config=config,
                expected_daily_date="2026-07-18",
                refresh_date="2026-07-19",
                publish=True,
            )

            self.assertTrue(retry.published)
            self.assertTrue(store.current_dir.is_dir())
            self.assertEqual(store.load_state()["last_attempt"]["status"], "success")

    def test_missing_current_with_incomplete_audit_state_stays_fail_closed(
        self,
    ) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeRefreshError,
            run_full_refresh,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime_root = root / "runtime"
            runtime_root.mkdir(mode=0o700)
            (runtime_root / "state.json").write_text(
                json.dumps({"last_attempt": {"status": "failed"}}),
                encoding="utf-8",
            )
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=runtime_root,
                schema_path=_write_schema(root),
            )

            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "current is missing while state still exists",
            ):
                run_full_refresh(
                    client=_FakeClient(),
                    config=config,
                    expected_daily_date="2026-07-18",
                    refresh_date="2026-07-19",
                    publish=True,
                )

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

    def test_local_mysql_round_source_publishes_sealed_provenance(self) -> None:
        from shared.data_bridge.mysql_exporter import (
            MySqlDataBridgeRoundBuilder,
        )
        from shared.data_bridge.refresh import (
            CURRENT_PUBLICATION_MANIFEST_VERSION,
            DataBridgeRefreshConfig,
            check_current_dataset,
            run_full_refresh,
        )
        from shared.data_contract import (
            SourceCommitEvidence,
            SourceTableEvidence,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=_write_schema(root),
                max_rounds=3,
            )
            dataset = _validated_dataset(root, config.schema_path)
            raw_provenance = _local_source_provenance("2026-07-18")
            evidence = SourceCommitEvidence(
                feature_date="2026-07-18",
                source_commit_token=str(
                    raw_provenance["source_commit_token"]
                ),
                tables=tuple(
                    SourceTableEvidence(**table)
                    for table in raw_provenance["source_evidence"]["tables"]
                ),
            )

            class FakeConnection:
                def __init__(self) -> None:
                    self.commands: list[str] = []

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc_value, traceback) -> None:
                    return None

                def exec_driver_sql(self, statement: str) -> None:
                    self.commands.append(statement)

                def rollback(self) -> None:
                    return None

            class FakeEngine:
                def __init__(self) -> None:
                    self.connection = FakeConnection()

                def connect(self) -> FakeConnection:
                    return self.connection

            engine = FakeEngine()
            builder = MySqlDataBridgeRoundBuilder(
                engine=engine,
                config=config,
            )
            with (
                patch(
                    "shared.data_bridge.mysql_exporter."
                    "_assert_required_source_tables",
                ),
                patch(
                    "shared.data_bridge.mysql_exporter."
                    "build_daily_output_from_db",
                    return_value=dataset.frames["daily_output.csv"],
                ),
                patch(
                    "shared.data_bridge.mysql_exporter."
                    "build_weekly_output_from_db",
                    return_value=dataset.frames["weekly_output.csv"],
                ),
                patch(
                    "shared.data_bridge.mysql_exporter."
                    "build_monthly_output_from_db",
                    return_value=dataset.frames["monthly_output.csv"],
                ),
                patch(
                    "shared.data_bridge.mysql_exporter."
                    "capture_source_commit_evidence_from_connection",
                    return_value=evidence,
                ),
            ):
                result = run_full_refresh(
                    config=config,
                    expected_daily_date="2026-07-18",
                    refresh_date="2026-07-19",
                    publish=True,
                    round_builder=builder,
                    require_launchd_round_builder=True,
                )

            self.assertIs(type(builder), MySqlDataBridgeRoundBuilder)
            self.assertEqual(result.state["source_mode"], "local_mysql")
            current = check_current_dataset(
                config,
                required_refresh_date="2026-07-19",
                expected_daily_date="2026-07-18",
                require_source_provenance=True,
            )
            marker = json.loads(
                (config.data_root / "current" / ".publication-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                marker["manifest_version"],
                CURRENT_PUBLICATION_MANIFEST_VERSION,
            )
            self.assertEqual(
                current.state["source_provenance"]["feature_date"],
                "2026-07-18",
            )

    def test_launchd_only_refresh_rejects_builder_subclass_override(
        self,
    ) -> None:
        from shared.data_bridge.mysql_exporter import (
            MySqlDataBridgeRoundBuilder,
        )
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
                max_rounds=3,
            )
            class OverridingRoundBuilder(MySqlDataBridgeRoundBuilder):
                def __init__(self) -> None:
                    pass

                def build(
                    self,
                    round_id: str,
                    *,
                    end_date: str,
                    expected_daily_date: str,
                    previous_keys,
                    continuity_cutoffs=None,
                ):
                    raise AssertionError("must not call overridden build")

            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "requires MySqlDataBridgeRoundBuilder",
            ):
                run_full_refresh(
                    config=config,
                    expected_daily_date="2026-07-18",
                    refresh_date="2026-07-19",
                    publish=True,
                    round_builder=OverridingRoundBuilder(),
                    require_launchd_round_builder=True,
                )

            self.assertFalse((config.data_root / "current").exists())

    def test_launchd_only_refresh_rejects_protocol_only_round_builder(
        self,
    ) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeRefreshError,
            DownloadRound,
            run_full_refresh,
        )
        from shared.data_bridge.validation import write_validated_dataset

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=_write_schema(root),
                max_rounds=3,
            )
            dataset = _validated_dataset(root, config.schema_path)

            class ProtocolOnlyRoundBuilder:
                def build(
                    self,
                    round_id: str,
                    *,
                    end_date: str,
                    expected_daily_date: str,
                    previous_keys,
                    continuity_cutoffs=None,
                ):
                    del end_date, previous_keys, continuity_cutoffs
                    destination = config.runtime_root / "staging" / round_id
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    write_validated_dataset(dataset, destination)
                    return DownloadRound(
                        round_id=round_id,
                        directory=destination,
                        dataset=dataset,
                        digest=dataset.business_digest,
                        source_mode="local_mysql",
                        source_provenance=_local_source_provenance(
                            expected_daily_date
                        ),
                    )

            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "requires MySqlDataBridgeRoundBuilder",
            ):
                run_full_refresh(
                    config=config,
                    expected_daily_date="2026-07-18",
                    refresh_date="2026-07-19",
                    publish=True,
                    round_builder=ProtocolOnlyRoundBuilder(),
                    require_launchd_round_builder=True,
                )

            self.assertFalse((config.data_root / "current").exists())

    def test_local_mysql_provenance_is_required_and_marker_bound(self) -> None:
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
            candidate = write_validated_dataset(dataset, root / "candidate")
            state = _dataset_state(dataset, refresh_date="2026-07-19")
            state.update(
                {
                    "source_mode": "local_mysql",
                    "source_provenance": _local_source_provenance(
                        "2026-07-18"
                    ),
                }
            )
            store = DataBridgeStore(
                data_root=config.data_root,
                runtime_root=config.runtime_root,
            )
            store.publish(
                candidate,
                state,
            )
            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "requires expected_daily_date",
            ):
                check_current_dataset(
                    config,
                    require_source_provenance=True,
                )
            check_current_dataset(
                config,
                expected_daily_date="2026-07-18",
                require_source_provenance=True,
            )

            altered = store.load_state()
            altered["source_provenance"]["snapshot_started_at"] = (
                "2026-07-19T06:31:00+08:00"
            )
            store.state_path.write_text(
                json.dumps(altered),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "identity does not match",
            ):
                check_current_dataset(
                    config,
                    expected_daily_date="2026-07-18",
                    require_source_provenance=True,
                )

    def test_local_mysql_current_rejects_provenance_cutoff_mismatch(self) -> None:
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
            candidate = write_validated_dataset(dataset, root / "candidate")
            state = _dataset_state(dataset, refresh_date="2026-07-19")
            state.update(
                {
                    "source_mode": "local_mysql",
                    "source_provenance": _local_source_provenance("2026-07-17"),
                }
            )
            DataBridgeStore(
                data_root=config.data_root,
                runtime_root=config.runtime_root,
            ).publish(
                candidate,
                state,
            )

            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "feature_date must equal expected_daily_date",
            ):
                check_current_dataset(
                    config,
                    expected_daily_date="2026-07-18",
                    require_source_provenance=True,
                )

    def test_local_mysql_current_rejects_incomplete_source_evidence(self) -> None:
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
            candidate = write_validated_dataset(dataset, root / "candidate")
            state = _dataset_state(dataset, refresh_date="2026-07-19")
            state.update(
                {
                    "source_mode": "local_mysql",
                    "source_provenance": _local_source_provenance(
                        "2026-07-18",
                        missing_table="api_wind_daily",
                    ),
                }
            )
            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "evidence tables are incomplete",
            ):
                DataBridgeStore(
                    data_root=config.data_root,
                    runtime_root=config.runtime_root,
                ).publish(
                    candidate,
                    state,
                )

    def test_legacy_current_cannot_satisfy_local_mysql_freshness_read(self) -> None:
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
            candidate = write_validated_dataset(dataset, root / "candidate")
            DataBridgeStore(
                data_root=config.data_root,
                runtime_root=config.runtime_root,
            ).publish(candidate, _dataset_state(dataset, refresh_date="2026-07-19"))

            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "lacks sealed local MySQL",
            ):
                check_current_dataset(
                    config,
                    expected_daily_date="2026-07-18",
                    require_source_provenance=True,
                )

    def test_refresh_scopes_previous_keys_to_effective_continuity_cutoffs(
        self,
    ) -> None:
        from shared.data_bridge.authority import (
            DataBridgeContinuityAuthority,
        )
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeStore,
            data_bridge_continuity_authority_sha256,
            data_bridge_publication_identity_sha256,
            run_full_refresh,
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
                daily_start_date="2026-01-01",
                daily_chunk_months=3,
                download_concurrency=2,
                max_rounds=3,
            )
            previous = validate_dataset(
                {
                    "daily_output.csv": pd.DataFrame(
                        {
                            "date": [
                                "2026/01/02 00:00",
                                "2026/04/01 00:00",
                                "2026/07/18 00:00",
                            ],
                            "factor": ["1", "2", "3"],
                        }
                    ),
                    "weekly_output.csv": pd.DataFrame(
                        {
                            "week_id": [
                                "202628",
                                "202629",
                                "202630",
                            ],
                            "factor": ["3", "4", "5"],
                        }
                    ),
                    "monthly_output.csv": pd.DataFrame(
                        {
                            "month_id": ["202606", "202607"],
                            "factor": ["5", "6"],
                        }
                    ),
                },
                schema_path=schema,
            )
            candidate = write_validated_dataset(
                previous,
                root / "previous-candidate",
            )
            store = DataBridgeStore(
                data_root=config.data_root,
                runtime_root=config.runtime_root,
            )
            store.publish(
                candidate,
                _dataset_state(
                    previous,
                    refresh_date="2026-07-18",
                ),
            )
            previous_state = store.load_state()
            continuity_authority = DataBridgeContinuityAuthority(
                generation_id=str(previous_state["generation_id"]),
                business_digest=str(
                    previous_state["business_digest"]
                ),
                publication_identity_sha256=(
                    data_bridge_publication_identity_sha256(
                        previous_state
                    )
                ),
                stable_identity_sha256=(
                    data_bridge_continuity_authority_sha256(
                        generation_id=str(
                            previous_state["generation_id"]
                        ),
                        business_digest=str(
                            previous_state["business_digest"]
                        ),
                        publication_identity_sha256=(
                            data_bridge_publication_identity_sha256(
                                previous_state
                            )
                        ),
                        daily_cutoff_key="2026-07-18",
                        weekly_cutoff_key="202629",
                        monthly_cutoff_key="202607",
                    )
                ),
                daily_cutoff_key="2026-07-18",
                weekly_cutoff_key="202629",
                monthly_cutoff_key="202607",
            )

            result = run_full_refresh(
                client=_FakeClient(),
                config=config,
                expected_daily_date="2026-07-18",
                refresh_date="2026-07-19",
                publish=False,
                continuity_authority=continuity_authority,
            )

        self.assertEqual(result.rounds_completed, 2)
        self.assertEqual(
            result.state["files"]["weekly_output.csv"]["max_key"],
            "202629",
        )

    def test_refresh_requires_frozen_period_source_key_after_fallback(
        self,
    ) -> None:
        """fallback 只放宽连续性 cutoff，不能丢失同一只读快照的 exact key。"""
        from shared.data_bridge.authority import (
            DataBridgeContinuityAuthority,
        )
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeRefreshError,
            DataBridgeStore,
            data_bridge_continuity_authority_sha256,
            data_bridge_publication_identity_sha256,
            run_full_refresh,
        )
        from shared.data_bridge.validation import (
            validate_dataset,
            write_validated_dataset,
        )

        scenarios = (
            (
                "weekly_output.csv",
                "week_id",
                "required_weekly_key",
                "202630",
                {"weekly_keys": ("202628", "202629", "202630")},
            ),
            (
                "monthly_output.csv",
                "month_id",
                "required_monthly_key",
                "202608",
                {"monthly_keys": ("202606", "202607", "202608")},
            ),
        )
        for (
            filename,
            key_column,
            guard_field,
            frozen_key,
            complete_candidate,
        ) in scenarios:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                schema = _write_schema(root)
                config = DataBridgeRefreshConfig(
                    data_root=root / "data",
                    runtime_root=root / "runtime",
                    schema_path=schema,
                    daily_start_date="2026-01-01",
                    daily_chunk_months=3,
                    download_concurrency=2,
                    max_rounds=3,
                )
                previous_frames = {
                    "daily_output.csv": pd.DataFrame(
                        {
                            "date": [
                                "2026/01/02 00:00",
                                "2026/04/01 00:00",
                                "2026/07/18 00:00",
                            ],
                            "factor": ["1", "2", "3"],
                        }
                    ),
                    "weekly_output.csv": pd.DataFrame(
                        {
                            "week_id": ["202628", "202629", "202630"],
                            "factor": ["3", "4", "5"],
                        }
                    ),
                    # 202609 是早先 current 中的 future-labeled month；
                    # fallback 只冻结 source exact 202608，不能要求它继续存在。
                    "monthly_output.csv": pd.DataFrame(
                        {
                            "month_id": [
                                "202606",
                                "202607",
                                "202608",
                                "202609",
                            ],
                            "factor": ["5", "6", "7", "8"],
                        }
                    ),
                }
                previous = validate_dataset(
                    previous_frames,
                    schema_path=schema,
                )
                store = DataBridgeStore(
                    data_root=config.data_root,
                    runtime_root=config.runtime_root,
                )
                store.publish(
                    write_validated_dataset(
                        previous,
                        root / "previous-candidate",
                    ),
                    _dataset_state(
                        previous,
                        refresh_date="2026-07-18",
                    ),
                )
                previous_state = store.load_state()
                guard_kwargs = {guard_field: frozen_key}
                continuity_authority = DataBridgeContinuityAuthority(
                    generation_id=str(previous_state["generation_id"]),
                    business_digest=str(previous_state["business_digest"]),
                    publication_identity_sha256=(
                        data_bridge_publication_identity_sha256(
                            previous_state
                        )
                    ),
                    stable_identity_sha256=(
                        data_bridge_continuity_authority_sha256(
                            generation_id=str(
                                previous_state["generation_id"]
                            ),
                            business_digest=str(
                                previous_state["business_digest"]
                            ),
                            publication_identity_sha256=(
                                data_bridge_publication_identity_sha256(
                                    previous_state
                                )
                            ),
                            daily_cutoff_key="2026-07-18",
                            weekly_cutoff_key="202629",
                            monthly_cutoff_key="202607",
                            **guard_kwargs,
                        )
                    ),
                    daily_cutoff_key="2026-07-18",
                    weekly_cutoff_key="202629",
                    monthly_cutoff_key="202607",
                    **guard_kwargs,
                )

                with self.assertRaisesRegex(
                    DataBridgeRefreshError,
                    rf"frozen exact.*{frozen_key}",
                ):
                    run_full_refresh(
                        client=_FakeClient(),
                        config=config,
                        expected_daily_date="2026-07-18",
                        refresh_date="2026-07-19",
                        publish=True,
                        continuity_authority=continuity_authority,
                    )

                self.assertEqual(
                    store.load_state()["generation_id"],
                    previous_state["generation_id"],
                )
                self.assertIn(
                    frozen_key,
                    pd.read_csv(
                        store.current_dir / filename,
                        dtype={key_column: "string"},
                    )[key_column].tolist(),
                )

                result = run_full_refresh(
                    client=_FakeClient(**complete_candidate),
                    config=config,
                    expected_daily_date="2026-07-18",
                    refresh_date="2026-07-19",
                    publish=True,
                    continuity_authority=continuity_authority,
                )

                self.assertTrue(result.published)
                self.assertIn(
                    frozen_key,
                    pd.read_csv(
                        store.current_dir / filename,
                        dtype={key_column: "string"},
                    )[key_column].tolist(),
                )

    def test_continuity_authority_frozen_period_keys_are_hashed_and_validated(
        self,
    ) -> None:
        """frozen exact key 必须绑定 authority 摘要，且只接受规范 six-digit 键。"""
        from shared.data_bridge.authority import (
            DataBridgeContinuityAuthority,
        )
        from shared.data_bridge.refresh import (
            CurrentDataset,
            DataBridgeRefreshError,
            _validate_continuity_authority,
            data_bridge_continuity_authority_sha256,
            data_bridge_publication_identity_sha256,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema = _write_schema(root)
            dataset = _validated_dataset(root, schema)
            state = _dataset_state(dataset, refresh_date="2026-07-18")
            current = CurrentDataset(state=state, dataset=dataset)
            common = {
                "generation_id": str(state["generation_id"]),
                "business_digest": str(state["business_digest"]),
                "publication_identity_sha256": (
                    data_bridge_publication_identity_sha256(state)
                ),
                "daily_cutoff_key": "2026-07-18",
                "weekly_cutoff_key": "202629",
                "monthly_cutoff_key": "202607",
            }
            unguarded_digest = data_bridge_continuity_authority_sha256(
                **common,
            )
            guarded_digest = data_bridge_continuity_authority_sha256(
                **common,
                required_weekly_key="202630",
            )
            self.assertNotEqual(unguarded_digest, guarded_digest)

            forged = DataBridgeContinuityAuthority(
                **common,
                stable_identity_sha256=unguarded_digest,
                required_weekly_key="202630",
            )
            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "stable digest mismatch",
            ):
                _validate_continuity_authority(
                    current,
                    continuity_authority=forged,
                )

            malformed = DataBridgeContinuityAuthority(
                **common,
                stable_identity_sha256="a" * 64,
                required_monthly_key="20260x",
            )
            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "authority is invalid",
            ):
                _validate_continuity_authority(
                    current,
                    continuity_authority=malformed,
                )

    def test_refresh_rejects_current_replaced_after_authority_resolution(
        self,
    ) -> None:
        from shared.data_bridge.authority import (
            DataBridgeContinuityAuthority,
        )
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeRefreshError,
            DataBridgeStore,
            data_bridge_continuity_authority_sha256,
            data_bridge_publication_identity_sha256,
            run_full_refresh,
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
                daily_start_date="2026-01-01",
            )
            first = _validated_dataset(root, schema)
            store = DataBridgeStore(
                data_root=config.data_root,
                runtime_root=config.runtime_root,
            )
            first_state = _dataset_state(
                first,
                refresh_date="2026-07-18",
            )
            first_state["generation_id"] = "generation-first"
            store.publish(
                write_validated_dataset(first, root / "first"),
                first_state,
            )
            first_published_state = store.load_state()
            authority = DataBridgeContinuityAuthority(
                generation_id="generation-first",
                business_digest=first.business_digest,
                publication_identity_sha256=(
                    data_bridge_publication_identity_sha256(
                        first_published_state
                    )
                ),
                stable_identity_sha256=(
                    data_bridge_continuity_authority_sha256(
                        generation_id="generation-first",
                        business_digest=first.business_digest,
                        publication_identity_sha256=(
                            data_bridge_publication_identity_sha256(
                                first_published_state
                            )
                        ),
                        daily_cutoff_key="2026-07-18",
                        weekly_cutoff_key="202629",
                        monthly_cutoff_key="202607",
                    )
                ),
                daily_cutoff_key="2026-07-18",
                weekly_cutoff_key="202629",
                monthly_cutoff_key="202607",
            )

            changed = {
                name: frame.copy()
                for name, frame in first.frames.items()
            }
            changed["daily_output.csv"].loc[0, "factor"] = "9"
            replacement = validate_dataset(
                changed,
                schema_path=schema,
            )
            replacement_state = _dataset_state(
                replacement,
                refresh_date="2026-07-18",
            )
            replacement_state["generation_id"] = (
                "generation-replacement"
            )
            store.publish(
                write_validated_dataset(
                    replacement,
                    root / "replacement",
                ),
                replacement_state,
            )

            client = _FakeClient()
            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "continuity authority.*drift",
            ):
                run_full_refresh(
                    client=client,
                    config=config,
                    expected_daily_date="2026-07-18",
                    refresh_date="2026-07-19",
                    publish=False,
                    continuity_authority=authority,
                )

            current = store.load_state()
            self.assertEqual(
                current["generation_id"],
                "generation-replacement",
            )
            self.assertEqual(client.calls, [])

    def test_refresh_without_authority_rejects_current_appearing_race(
        self,
    ) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeRefreshError,
            DataBridgeStore,
            run_full_refresh,
        )
        from shared.data_bridge.validation import (
            write_validated_dataset,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema = _write_schema(root)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=schema,
                daily_start_date="2026-01-01",
            )
            current = _validated_dataset(root, schema)
            DataBridgeStore(
                data_root=config.data_root,
                runtime_root=config.runtime_root,
            ).publish(
                write_validated_dataset(current, root / "current"),
                _dataset_state(
                    current,
                    refresh_date="2026-07-18",
                ),
            )

            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "continuity authority is missing",
            ):
                run_full_refresh(
                    client=_FakeClient(),
                    config=config,
                    expected_daily_date="2026-07-18",
                    refresh_date="2026-07-19",
                    publish=False,
                    continuity_authority=None,
                )

    def test_refresh_rejects_cutoff_forged_under_previous_stable_digest(
        self,
    ) -> None:
        from dataclasses import replace

        from shared.data_bridge.authority import (
            DataBridgeContinuityAuthority,
        )
        from shared.data_bridge.refresh import (
            DataBridgeRefreshConfig,
            DataBridgeRefreshError,
            DataBridgeStore,
            data_bridge_continuity_authority_sha256,
            data_bridge_publication_identity_sha256,
            run_full_refresh,
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
                daily_start_date="2026-01-01",
            )
            previous = validate_dataset(
                {
                    "daily_output.csv": pd.DataFrame(
                        {
                            "date": ["2026/07/18 00:00"],
                            "factor": ["1"],
                        }
                    ),
                    "weekly_output.csv": pd.DataFrame(
                        {
                            "week_id": [
                                "202628",
                                "202629",
                                "202630",
                            ],
                            "factor": ["2", "3", "4"],
                        }
                    ),
                    "monthly_output.csv": pd.DataFrame(
                        {
                            "month_id": ["202606", "202607"],
                            "factor": ["5", "6"],
                        }
                    ),
                },
                schema_path=schema,
            )
            store = DataBridgeStore(
                data_root=config.data_root,
                runtime_root=config.runtime_root,
            )
            store.publish(
                write_validated_dataset(previous, root / "previous"),
                _dataset_state(
                    previous,
                    refresh_date="2026-07-18",
                ),
            )
            state = store.load_state()
            publication_identity = (
                data_bridge_publication_identity_sha256(state)
            )
            stable_identity = (
                data_bridge_continuity_authority_sha256(
                    generation_id=str(state["generation_id"]),
                    business_digest=previous.business_digest,
                    publication_identity_sha256=publication_identity,
                    daily_cutoff_key="2026-07-18",
                    weekly_cutoff_key="202629",
                    monthly_cutoff_key="202607",
                )
            )
            authority = DataBridgeContinuityAuthority(
                generation_id=str(state["generation_id"]),
                business_digest=previous.business_digest,
                publication_identity_sha256=publication_identity,
                stable_identity_sha256=stable_identity,
                daily_cutoff_key="2026-07-18",
                weekly_cutoff_key="202629",
                monthly_cutoff_key="202607",
            )
            forged = replace(
                authority,
                weekly_cutoff_key="202628",
            )
            client = _FakeClient()

            with self.assertRaisesRegex(
                DataBridgeRefreshError,
                "continuity authority.*digest",
            ):
                run_full_refresh(
                    client=client,
                    config=config,
                    expected_daily_date="2026-07-18",
                    refresh_date="2026-07-19",
                    publish=False,
                    continuity_authority=forged,
                )

            self.assertEqual(client.calls, [])

    def test_authority_entry_recovers_crash_and_legacy_current_states(
        self,
    ) -> None:
        from shared.blackbox_v2.snapshot import CutoffKeys
        from shared.data_bridge.authority import (
            resolve_databridge_continuity_authority_from_engine,
        )
        from shared.data_bridge.refresh import (
            CURRENT_PUBLICATION_MANIFEST,
            DataBridgeRefreshConfig,
            DataBridgeStore,
        )
        from shared.data_bridge.validation import (
            write_validated_dataset,
        )

        for scenario in (
            "previous_only",
            "damaged_current",
            "legacy_marker",
        ):
            with (
                self.subTest(scenario=scenario),
                tempfile.TemporaryDirectory() as tmpdir,
            ):
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
                state = _dataset_state(
                    dataset,
                    refresh_date="2026-07-18",
                )
                state["generation_id"] = "generation-recoverable"
                store.publish(
                    write_validated_dataset(
                        dataset,
                        root / "candidate",
                    ),
                    state,
                )
                if scenario == "previous_only":
                    os.replace(
                        store.current_dir,
                        store.previous_dir,
                    )
                elif scenario == "damaged_current":
                    shutil.copytree(
                        store.current_dir,
                        store.previous_dir,
                    )
                    daily = (
                        store.current_dir / "daily_output.csv"
                    )
                    daily.chmod(0o644)
                    daily.write_text(
                        daily.read_text(encoding="utf-8").replace(
                            ",1\n",
                            ",9\n",
                        ),
                        encoding="utf-8",
                    )
                    daily.chmod(0o444)
                else:
                    (
                        store.current_dir
                        / CURRENT_PUBLICATION_MANIFEST
                    ).unlink()

                connection = Mock()
                connection_context = Mock()
                connection_context.__enter__ = Mock(
                    return_value=connection
                )
                connection_context.__exit__ = Mock(
                    return_value=False
                )
                engine = Mock()
                engine.connect.return_value = connection_context
                with patch(
                    "shared.data_bridge.authority."
                    "_resolve_blackbox_input_cutoffs_with_source_keys_"
                    "bulk_from_keys",
                    return_value={
                        "2026-07-18": SimpleNamespace(
                            cutoff_keys=CutoffKeys(
                                daily_cutoff_key="2026-07-18",
                                weekly_cutoff_key="202629",
                                monthly_cutoff_key="202607",
                            ),
                            source_weekly_cutoff_key="202629",
                            source_monthly_cutoff_key="202607",
                        )
                    },
                ):
                    authority = (
                        resolve_databridge_continuity_authority_from_engine(
                            config,
                            feature_date="2026-07-18",
                            engine=engine,
                        )
                    )

                self.assertEqual(
                    authority.generation_id,
                    "generation-recoverable",
                )
                self.assertFalse(store.previous_dir.exists())
                self.assertTrue(
                    (
                        store.current_dir
                        / CURRENT_PUBLICATION_MANIFEST
                    ).is_file()
                )

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
        from shared.data_bridge.refresh import (
            CURRENT_PUBLICATION_MANIFEST_VERSION,
            DataBridgeStore,
        )
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

    def test_new_publication_writes_v3_without_legacy_capability(
        self,
    ) -> None:
        from shared.data_bridge.refresh import (
            CURRENT_PUBLICATION_MANIFEST_VERSION,
            DataBridgeStore,
        )
        from shared.data_bridge.validation import write_validated_dataset

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema = _write_schema(root)
            dataset = _validated_dataset(root, schema)
            state = _dataset_state(
                dataset,
                refresh_date="2026-07-24",
            )
            state["publication_capability"] = {
                "occurrence_id": 17,
                "business_date": "2026-07-24",
                "daily_coordinator_epoch": {
                    "epoch": 3,
                    "mode": "ledger",
                    "record_sha256": "7" * 64,
                },
            }
            store = DataBridgeStore(
                data_root=root / "data",
                runtime_root=root / "runtime",
            )
            published = store.publish(
                write_validated_dataset(dataset, root / "candidate"),
                state,
            )
            marker = json.loads(
                (
                    store.current_dir
                    / ".publication-manifest.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(
            marker["manifest_version"],
            CURRENT_PUBLICATION_MANIFEST_VERSION,
        )
        self.assertNotIn("publication_capability", marker)
        self.assertNotIn("publication_capability", published)
        self.assertEqual(
            published["publication_manifest_version"],
            CURRENT_PUBLICATION_MANIFEST_VERSION,
        )


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


def _local_source_provenance(
    feature_date: str,
    *,
    row_count: int = 1,
    missing_table: str | None = None,
) -> dict[str, object]:
    from shared.data_bridge.refresh import LOCAL_MYSQL_PROVENANCE_VERSION
    from shared.data_contract import (
        SourceCommitEvidence,
        SourceTableEvidence,
        CALENDAR_SOURCE_TABLES,
        FACTOR_SOURCE_TABLES,
        METADATA_SOURCE_TABLE,
        source_commit_evidence_payload,
        source_commit_evidence_sha256,
    )

    table_names = sorted(
        set(FACTOR_SOURCE_TABLES)
        | {METADATA_SOURCE_TABLE}
        | set(CALENDAR_SOURCE_TABLES)
    )
    if missing_table is not None:
        table_names.remove(missing_table)

    unsigned = SourceCommitEvidence(
        feature_date=feature_date,
        source_commit_token="",
        tables=tuple(
            SourceTableEvidence(
                table_name=table_name,
                row_count=row_count,
                latest_create_time=None,
            )
            for table_name in table_names
        ),
    )
    token = source_commit_evidence_sha256(unsigned)
    evidence = SourceCommitEvidence(
        feature_date=feature_date,
        source_commit_token=token,
        tables=unsigned.tables,
    )
    return {
        "provenance_version": LOCAL_MYSQL_PROVENANCE_VERSION,
        "feature_date": feature_date,
        "source_rdate_cutoff": feature_date,
        "snapshot_started_at": "2026-07-19T06:30:00+08:00",
        "source_commit_token": token,
        "source_evidence_sha256": token,
        "source_evidence": source_commit_evidence_payload(evidence),
    }


class _FakeClient:
    def __init__(
        self,
        *,
        conflicting_duplicate: bool = False,
        table_names: set[str] | None = None,
        weekly_keys: tuple[str, ...] = ("202628", "202629"),
        monthly_keys: tuple[str, ...] = ("202606", "202607"),
    ) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self.conflicting_duplicate = conflicting_duplicate
        self.weekly_keys = weekly_keys
        self.monthly_keys = monthly_keys
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
            rows = "".join(
                f"{key},{index + 3}\n"
                for index, key in enumerate(self.weekly_keys)
            )
            return ("week_id,factor\n" + rows).encode("utf-8")
        if frequency == "月":
            rows = "".join(
                f"{key},{index + 5}\n"
                for index, key in enumerate(self.monthly_keys)
            )
            return ("month_id,factor\n" + rows).encode("utf-8")
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
