from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


class BlackboxV2HarnessGateTests(unittest.TestCase):

    def test_generation_snapshot_contains_standard_calendar_file(self) -> None:
        from shared.blackbox_v2.snapshot import (
            SNAPSHOT_FILENAMES,
            create_snapshot_from_frames,
        )

        frames = _snapshot_frames()
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=Path(tmpdir),
                expected_columns={
                    name: list(frame.columns)
                    for name, frame in frames.items()
                },
                schema_version="data-bridge-v1",
            )

            self.assertEqual(
                set(path.name for path in snapshot.data_dir.iterdir()),
                set(SNAPSHOT_FILENAMES),
            )
            self.assertEqual(
                (snapshot.data_dir / "api_wind_date.csv").read_text(
                    encoding="utf-8"
                ),
                "rdate,week_id\n"
                "2026-07-14,202627\n"
                "2026-07-15,202627\n"
                "2026-07-16,202628\n",
            )

    def test_producer_prepares_snapshot_and_schemes_only_read_receipt(self) -> None:
        from shared.blackbox_v2.snapshot import SNAPSHOT_FILENAMES
        from shared.input_artifacts import (
            get_ready_blackbox_snapshot,
            prepare_blackbox_generation_snapshot,
        )

        frames = _snapshot_frames()
        for code in ("M0041340", "M0041341", "M0041342"):
            frames["monthly_output.csv"][code] = 0.0
        profiles = {
            name: SimpleNamespace(
                sha256=hashlib.sha256(
                    frame.to_csv(index=False, lineterminator="\n").encode(
                        "utf-8"
                    )
                ).hexdigest(),
                business_hash=(str(index + 1) * 64)[:64],
                rows=len(frame),
                columns=len(frame.columns),
                min_key=str(frame.iloc[0, 0]),
                max_key=str(frame.iloc[-1, 0]),
            )
            for index, (name, frame) in enumerate(frames.items())
        }
        state = {
            "generation_id": "generation-shared",
            "refresh_date": "2026-07-15",
            "business_digest": "b" * 64,
            "schema_version": "data-bridge-v1",
            "files": {
                name: {
                    "sha256": profiles[name].sha256,
                    "business_hash": profiles[name].business_hash,
                    "rows": profiles[name].rows,
                    "columns": profiles[name].columns,
                    "min_key": profiles[name].min_key,
                    "max_key": profiles[name].max_key,
                }
                for name in SNAPSHOT_FILENAMES
            },
        }

        class Store:
            def __init__(self, **kwargs) -> None:
                self.current_dir = Path(kwargs["data_root"]) / "current"

            @contextmanager
            def current_read(self, *, strict_read_only):
                self.assert_true(strict_read_only)
                yield state

            @staticmethod
            def assert_true(value):
                if not value:
                    raise AssertionError("strict read-only access required")

        dataset = SimpleNamespace(
            schema_version="data-bridge-v1",
            business_digest="b" * 64,
            frames=frames,
            files=profiles,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_root = root / "databridge"
            current_dir = data_root / "current"
            current_dir.mkdir(parents=True)
            (current_dir / ".publication-manifest.json").write_text(
                json.dumps(state, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with (
                patch("shared.input_artifacts.DataBridgeStore", Store),
                patch(
                    "shared.input_artifacts.check_current_dataset",
                    side_effect=AssertionError(
                        "ready snapshot path must not validate DataBridge current"
                    ),
                ) as check_current,
                patch(
                    "shared.input_artifacts._load_blackbox_schema",
                    return_value=(
                        "data-bridge-v1",
                        {name: list(frame.columns) for name, frame in frames.items()},
                    ),
                ),
            ):
                prepared = prepare_blackbox_generation_snapshot(
                    state=state,
                    dataset=dataset,
                    cache_root=root / "generation-cache",
                )
                ready = get_ready_blackbox_snapshot(
                    snapshot_date="2026-07-15",
                    data_root=data_root,
                    cache_root=root / "generation-cache",
                )

                mismatched = dict(state)
                mismatched["generation_id"] = "generation-other"
                (current_dir / ".publication-manifest.json").write_text(
                    json.dumps(mismatched, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "state and publication manifest differ",
                ):
                    get_ready_blackbox_snapshot(
                        snapshot_date="2026-07-15",
                        data_root=data_root,
                        cache_root=root / "generation-cache",
                    )

                (current_dir / ".publication-manifest.json").write_text(
                    json.dumps(state, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                damaged = prepared.data_dir / "daily_output.csv"
                damaged.chmod(0o644)
                damaged.write_bytes(damaged.read_bytes() + b"\n")
                damaged.chmod(0o444)
                with self.assertRaisesRegex(
                    ValueError,
                    "no longer matches producer seal",
                ):
                    get_ready_blackbox_snapshot(
                        snapshot_date="2026-07-15",
                        data_root=data_root,
                        cache_root=root / "generation-cache",
                    )

        self.assertEqual(prepared.snapshot_id, ready.snapshot_id)
        self.assertEqual(ready.generation_id, "generation-shared")
        check_current.assert_not_called()

    def test_runtime_view_does_not_rehash_or_parse_generation_csv(self) -> None:
        from shared import input_artifacts
        from shared.blackbox_v2.snapshot import (
            SNAPSHOT_FILENAMES,
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )
        from shared.input_artifacts import open_blackbox_runtime_view

        frames = _snapshot_frames()
        expected_columns = {
            name: list(frame.columns) for name, frame in frames.items()
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=root / "snapshots",
                expected_columns=expected_columns,
                schema_version="data-bridge-v1",
            )
            snapshot = replace(
                snapshot,
                sealed_file_fingerprints=(
                    input_artifacts._snapshot_file_fingerprints(snapshot)
                ),
            )
            bundle = compose_blackbox_input_bundle(snapshot)
            csv_reads: list[str] = []
            stable_read = input_artifacts._read_stable_regular_file

            def record_stable_read(path, label):
                if Path(path).suffix == ".csv":
                    csv_reads.append(Path(path).name)
                return stable_read(path, label)

            path_read_bytes = Path.read_bytes

            def reject_runtime_target_read(path):
                if root / "runtime-views" in Path(path).parents:
                    raise AssertionError("runtime target was read after write")
                return path_read_bytes(path)

            with (
                patch(
                    "shared.input_artifacts._read_stable_regular_file",
                    side_effect=record_stable_read,
                ),
                patch.object(
                    Path,
                    "read_bytes",
                    autospec=True,
                    side_effect=reject_runtime_target_read,
                ),
            ):
                with open_blackbox_runtime_view(
                    bundle,
                    runtime_root=root / "runtime-views",
                ) as runtime_view:
                    self.assertEqual(
                        sorted(path.name for path in runtime_view.data_dir.iterdir()),
                        sorted(SNAPSHOT_FILENAMES),
                    )

                damaged = snapshot.data_dir / "daily_output.csv"
                damaged.chmod(0o644)
                damaged.write_bytes(damaged.read_bytes() + b"\n")
                damaged.chmod(0o444)
                with self.assertRaisesRegex(
                    ValueError,
                    "source no longer matches producer seal",
                ):
                    with open_blackbox_runtime_view(
                        bundle,
                        runtime_root=root / "runtime-views",
                    ):
                        pass

        self.assertEqual(csv_reads, [])

    def test_runtime_view_accepts_databridge_timestamp_daily_keys(self) -> None:
        from shared.blackbox_v2.snapshot import (
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )
        from shared.input_artifacts import open_blackbox_runtime_view

        frames = _snapshot_frames()
        frames["daily_output.csv"]["date"] = [
            "2026-07-14 00:00:00",
            "2026-07-15 00:00:00",
            "2026-07-16 00:00:00",
        ]
        expected_columns = {
            name: list(frame.columns) for name, frame in frames.items()
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=root / "snapshots",
                expected_columns=expected_columns,
                schema_version="data-bridge-v1",
            )
            with open_blackbox_runtime_view(
                compose_blackbox_input_bundle(snapshot),
                runtime_root=root / "runtime-views",
            ) as runtime_view:
                self.assertTrue(runtime_view.data_dir.is_dir())

    def test_successful_backtest_is_the_activation_evidence(self) -> None:
        from sqlalchemy import create_engine, text

        from harness.blackbox_v2.gates import verify_passed_blackbox_backtest

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        summary = {
            "scheme_version": "version-test",
            "manifest_hash": "m" * 64,
            "data_snapshot_id": "snapshot-test",
            "generation_id": "generation-test",
            "runtime_profile": "blackbox-v2-v1",
            "environment_fingerprint": "e" * 64,
        }
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE t_backtest_runs ("
                "id INTEGER PRIMARY KEY, benchmark_id TEXT, scheme_id TEXT, "
                "data_source TEXT, status TEXT, run_mode TEXT, updated_at TEXT, "
                "summary TEXT, code_hash TEXT, config_hash TEXT, "
                "input_artifact_hash TEXT)"
            )
            connection.execute(
                text(
                    "INSERT INTO t_backtest_runs VALUES "
                    "(1, 'bbv2-test', 'trial_10y', "
                    "'blackbox_v2_current_snapshot_as_of', 'success', 'persist', "
                    "'2026-07-15 00:00:00', :summary, :code_hash, :config_hash, "
                    "'snapshot-test')"
                ),
                {
                    "summary": json.dumps(summary),
                    "code_hash": "c" * 64,
                    "config_hash": "f" * 64,
                },
            )

        passed = verify_passed_blackbox_backtest(
            engine,
            SimpleNamespace(
                scheme_id="trial_10y",
                scheme_version="version-test",
                code_hash="c" * 64,
                config_hash="f" * 64,
                manifest_hash="m" * 64,
            ),
        )
        engine.dispose()

        self.assertEqual(passed.backtest_run_id, 1)
        self.assertEqual(passed.benchmark_id, "bbv2-test")
        self.assertEqual(passed.data_snapshot_id, "snapshot-test")
        self.assertEqual(passed.generation_id, "generation-test")

    def test_backtest_preflight_reuses_intake_safety_validation(self) -> None:
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery
        from harness.blackbox_v2.gates import validate_canonical_blackbox_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(
                _delivery(root / "incoming"),
                schemes_root=root / "schemes",
            )
            script = scheme_dir / "delivery" / "trial_10y.py"
            script.chmod(0o644)
            script.write_text("import requests\n", encoding="utf-8")
            cfg = load_scheme_config(scheme_dir / "config.yaml")

            with self.assertRaisesRegex(ValueError, "forbidden import requests"):
                validate_canonical_blackbox_delivery(cfg)

def _delivery(path: Path, *, script: str = "import argparse\nimport json\n") -> Path:
    path.mkdir(parents=True)
    (path / "trial_10y.py").write_text(script, encoding="utf-8")
    (path / "trial_10y.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scheme_id": "trial_10y",
                "name": "10Y Trial",
                "algorithm_version": "1.0.0",
                "target_tenor": "10Y",
                "task_type": "T+1",
                "horizon": 1,
                "target_rule": "target_date_yield_vs_feature_date_yield",
                "owner": "ALGO-A",
                "description": "使用期限利差和滚动分类模型形成方向信号。",
            }
        ),
        encoding="utf-8",
    )
    return path


def _snapshot_frames() -> dict[str, pd.DataFrame]:
    return {
        "daily_output.csv": pd.DataFrame(
            {"date": ["2026-07-14", "2026-07-15", "2026-07-16"], "daily_factor": [0.0, 1.0, 2.0]}
        ),
        "weekly_output.csv": pd.DataFrame(
            {"week_id": ["202626", "202627", "202628"], "weekly_factor": [0.0, 1.0, 2.0]}
        ),
        "monthly_output.csv": pd.DataFrame(
            {"month_id": ["202605", "202606", "202607"], "monthly_factor": [0.0, 1.0, 2.0]}
        ),
        "api_wind_date.csv": pd.DataFrame(
            {
                "rdate": ["2026-07-14", "2026-07-15", "2026-07-16"],
                "week_id": ["202627", "202627", "202628"],
            }
        ),
    }
