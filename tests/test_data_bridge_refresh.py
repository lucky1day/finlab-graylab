from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd


class DataBridgeRefreshTests(unittest.TestCase):
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
            self.assertEqual(
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
