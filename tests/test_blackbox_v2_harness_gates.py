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

from harness.context import GateContext
from harness.operation import build_direct_operation
import pandas as pd


def build_operation(
    scheme_id: str,
    action: str,
    predict_date: str | None = None,
    *,
    scheme_version: str,
    issued_by: str = "test-operator",
    **_ignored,
):
    return build_direct_operation(
        scheme_id,
        action,
        predict_date,
        scheme_version=scheme_version,
        issued_by=issued_by,
    )


class BlackboxV2HarnessGateTests(unittest.TestCase):

    def test_generation_snapshot_is_built_once_and_reused_across_schemes(self) -> None:
        from shared.blackbox_v2.snapshot import SNAPSHOT_FILENAMES
        from shared import input_artifacts
        from shared.input_artifacts import get_blackbox_generation_snapshot

        frames = _snapshot_frames()
        for code in ("M0041340", "M0041341", "M0041342"):
            frames["monthly_output.csv"][code] = 0.0
        state = {
            "generation_id": "generation-shared",
            "refresh_date": "2026-07-15",
            "business_digest": "b" * 64,
            "schema_version": "data-bridge-v1",
            "files": {
                name: {
                    "sha256": hashlib.sha256(
                        frames[name]
                        .to_csv(index=False, lineterminator="\n")
                        .encode("utf-8")
                    ).hexdigest(),
                    "rows": len(frames[name]),
                    "columns": list(frames[name].columns),
                }
                for name in SNAPSHOT_FILENAMES
            },
        }

        class Store:
            def __init__(self, **_kwargs) -> None:
                pass

            @contextmanager
            def current_read(self, *, strict_read_only):
                self.assert_true(strict_read_only)
                yield state

            @staticmethod
            def assert_true(value):
                if not value:
                    raise AssertionError("strict read-only access required")

        current = SimpleNamespace(
            state=state,
            dataset=SimpleNamespace(frames=frames),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with (
                patch("shared.input_artifacts.DataBridgeStore", Store),
                patch(
                    "shared.input_artifacts.check_current_dataset",
                    return_value=current,
                ) as check_current,
                patch(
                    "shared.input_artifacts._load_blackbox_schema",
                    return_value=(
                        "data-bridge-v1",
                        {name: list(frame.columns) for name, frame in frames.items()},
                    ),
                ),
            ):
                first = get_blackbox_generation_snapshot(
                    snapshot_date="2026-07-15",
                    cache_root=root / "generation-cache",
                )
                cached_csv_reads: list[Path] = []
                stable_read = input_artifacts._read_stable_regular_file

                def record_stable_read(path, label):
                    if Path(path).suffix == ".csv":
                        cached_csv_reads.append(Path(path))
                    return stable_read(path, label)

                with patch(
                    "shared.input_artifacts._read_stable_regular_file",
                    side_effect=record_stable_read,
                ):
                    second = get_blackbox_generation_snapshot(
                        snapshot_date="2026-07-15",
                        cache_root=root / "generation-cache",
                    )
                self.assertEqual(cached_csv_reads, [])
                receipt_path = next(
                    (root / "generation-cache" / "receipts").iterdir()
                )
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    receipt["identity"]["cache_schema_version"],
                    "blackbox-generation-snapshot-v1",
                )
                receipt["cutoff_keys"]["date"] = receipt["cutoff_keys"][
                    "date"
                ][:-1]
                receipt_path.chmod(0o644)
                receipt_path.write_text(
                    json.dumps(receipt),
                    encoding="utf-8",
                )
                receipt_path.chmod(0o444)
                tampered_cutoff_snapshot = get_blackbox_generation_snapshot(
                    snapshot_date="2026-07-15",
                    cache_root=root / "generation-cache",
                )
                from shared.blackbox_v2.snapshot import (
                    compose_blackbox_input_bundle,
                )

                with self.assertRaisesRegex(
                    ValueError,
                    "cutoff cache does not match verified CSV content",
                ):
                    with input_artifacts.open_blackbox_runtime_view(
                        compose_blackbox_input_bundle(
                            tampered_cutoff_snapshot
                        ),
                        runtime_root=root / "runtime-views",
                    ):
                        self.fail("forged cutoff cache reached the algorithm view")

        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(first.generation_id, "generation-shared")
        check_current.assert_called_once()

    def test_runtime_view_reads_each_generation_csv_once(self) -> None:
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

        self.assertEqual(sorted(csv_reads), sorted(SNAPSHOT_FILENAMES))

    def test_runtime_view_rejects_generation_csv_changed_after_cache_hit(self) -> None:
        from shared.blackbox_v2.snapshot import (
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
            bundle = compose_blackbox_input_bundle(snapshot)
            daily_path = snapshot.data_dir / "daily_output.csv"
            daily_path.chmod(0o644)
            daily_path.write_bytes(daily_path.read_bytes() + b"\n")
            daily_path.chmod(0o444)

            with self.assertRaisesRegex(
                ValueError,
                "daily_output.csv sha256 does not match manifest",
            ):
                with open_blackbox_runtime_view(
                    bundle,
                    runtime_root=root / "runtime-views",
                ):
                    self.fail("tampered generation CSV reached the algorithm view")

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

    def test_passed_all_reads_input_identity_from_database_evidence(self) -> None:
        from sqlalchemy import text

        from harness.blackbox_v2.gates import _verify_passed_all
        from tests.harness_control_plane import create_harness_control_plane_engine

        engine = create_harness_control_plane_engine()
        summary = json.dumps(
            {
                "passed": True,
                "evidence": [
                    {"key": "data_snapshot_id", "value": "snapshot-test"},
                    {"key": "generation_id", "value": "generation-test"},
                    {"key": "runtime_profile", "value": "blackbox-v2-v1"},
                    {"key": "environment_fingerprint", "value": "e" * 64},
                ],
                "errors": [],
            }
        )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO t_harness_runs VALUES "
                    "('hr-test', 'trial_10y', 'version-test', 'all', "
                    "'passed', '2026-07-15 00:00:00')"
                )
            )
            for gate_name in ("static", "input", "compare"):
                connection.execute(
                    text(
                        "INSERT INTO t_harness_gate_results VALUES "
                        "(:run_id, :gate_name, 'passed', :summary)"
                    ),
                    {
                        "run_id": "hr-test",
                        "gate_name": gate_name,
                        "summary": summary if gate_name == "input" else "{}",
                    },
                )

        passed = _verify_passed_all(
            engine,
            SimpleNamespace(
                scheme_id="trial_10y",
                scheme_version="version-test",
            ),
        )
        engine.dispose()

        self.assertEqual(passed.harness_run_id, "hr-test")
        self.assertEqual(passed.data_snapshot_id, "snapshot-test")
        self.assertEqual(passed.generation_id, "generation-test")


    def test_input_state_captures_declared_platform_input_in_read_only_transaction(self) -> None:
        from harness.blackbox_v2.gates import (
            _ensure_input_state,
            cleanup_runtime_input,
        )
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.contracts import BlackboxRequest
        from shared.blackbox_v2.intake import intake_delivery
        from shared.blackbox_v2.platform_inputs import freeze_platform_input
        from shared.blackbox_v2.snapshot import (
            CutoffKeys,
            create_snapshot_from_frames,
        )

        class Connection:
            def __init__(self) -> None:
                self.commands: list[str] = []
                self.rolled_back = False

            def exec_driver_sql(self, statement):
                self.commands.append(str(statement))

            def rollback(self):
                self.rolled_back = True

        class ConnectionContext:
            def __init__(self, connection) -> None:
                self.connection = connection

            def __enter__(self):
                return self.connection

            def __exit__(self, exc_type, exc, tb):
                return None

        class Engine:
            def __init__(self) -> None:
                self.connection = Connection()
                self.disposed = False

            def connect(self):
                return ConnectionContext(self.connection)

            def dispose(self):
                self.disposed = True

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(
                _delivery(root / "incoming"),
                schemes_root=root / "schemes",
                platform_inputs=["api-wind-date-v1"],
            )
            config = load_scheme_config(scheme_dir / "config.yaml")
            snapshot = replace(
                create_snapshot_from_frames(
                    _snapshot_frames(),
                    output_root=root / "snapshots",
                    expected_columns={
                        name: list(frame.columns)
                        for name, frame in _snapshot_frames().items()
                    },
                    schema_version="data-bridge-v1",
                ),
                generation_id="generation-test",
                refresh_date="2026-07-15",
            )
            cutoffs = CutoffKeys("2026-07-15", "202627", "202606")
            request = BlackboxRequest(
                request_id="trial-request",
                predict_date="2026-07-15",
                feature_date="2026-07-15",
                target_date="2026-07-16",
                daily_cutoff_key=cutoffs.daily_cutoff_key,
                weekly_cutoff_key=cutoffs.weekly_cutoff_key,
                monthly_cutoff_key=cutoffs.monthly_cutoff_key,
            )
            artifact = freeze_platform_input(
                "api-wind-date-v1",
                pd.DataFrame(
                    {
                        "rdate": ["2026-07-14", "2026-07-15"],
                        "week_id": ["202627", "202627"],
                    }
                ),
                weekly_cutoff_key=cutoffs.weekly_cutoff_key,
                audit_provenance={"source_kind": "harness_database"},
            )
            engine = Engine()
            ctx = GateContext(
                scheme_id=config.scheme_id,
                predict_date="2026-07-15",
                project_root=root,
                config=config,
                engine_factory=lambda: engine,
            )
            with (
                patch(
                    "harness.blackbox_v2.gates.get_blackbox_generation_snapshot",
                    return_value=snapshot,
                ) as generation_snapshot,
                patch(
                    "harness.blackbox_v2.gates.resolve_blackbox_input_cutoffs",
                    return_value=cutoffs,
                ),
                patch(
                    "harness.blackbox_v2.gates._feature_date",
                    return_value="2026-07-15",
                ),
                patch(
                    "harness.blackbox_v2.gates.build_live_request",
                    return_value=request,
                ),
                patch(
                    "harness.blackbox_v2.gates.capture_blackbox_platform_inputs_from_connection",
                    return_value=(artifact,),
                ) as capture,
                patch(
                    "harness.blackbox_v2.gates._data_bridge_provenance",
                    return_value={
                        "generation_id": "generation-test",
                        "refresh_date": "2026-07-15",
                        "refreshed_at": "2026-07-15T00:02:00+08:00",
                        "business_digest": "digest",
                        "runtime_profile": "blackbox-v2-v1",
                        "environment_fingerprint": "e" * 64,
                    },
                ),
                patch("harness.blackbox_v2.gates.get_calendar"),
            ):
                state = _ensure_input_state(ctx)
                same_state = _ensure_input_state(ctx)

            self.assertIs(same_state, state)
            generation_snapshot.assert_called_once_with(
                snapshot_date="2026-07-15",
                require_fresh=False,
            )
            self.assertNotEqual(
                state.bundle.combined_snapshot_id,
                snapshot.snapshot_id,
            )
            self.assertEqual(
                state.bundle.platform_input_ids,
                ("api-wind-date-v1",),
            )
            capture.assert_called_once_with(
                config.platform_inputs,
                connection=engine.connection,
                weekly_cutoff_key="202627",
            )
            self.assertEqual(
                engine.connection.commands,
                ["START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"],
            )
            self.assertTrue(engine.connection.rolled_back)
            self.assertTrue(engine.disposed)
            self.assertEqual(
                state.bundle.audit_manifest["base_snapshot"][
                    "generation_id"
                ],
                "generation-test",
            )
            self.assertEqual(
                state.bundle.audit_manifest["base_snapshot"][
                    "refresh_date"
                ],
                "2026-07-15",
            )
            self.assertFalse((root / "reports").exists())
            self.assertIn("blackbox_v2.input_state", ctx.runtime_state)
            cleanup_runtime_input(ctx)
            self.assertNotIn("blackbox_v2.input_state", ctx.runtime_state)


    def test_automatic_execution_gates_run_end_to_end_without_business_writes(self) -> None:
        from harness.blackbox_v2.gates import (
            BlackboxCompareGate,
            InputState,
        )
        from scheduler.blackbox_v2_runner import RuntimeProfile
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.contracts import BlackboxRequest
        from shared.blackbox_v2.intake import intake_delivery
        from shared.blackbox_v2.platform_inputs import freeze_platform_input
        from shared.blackbox_v2.snapshot import (
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )
        from tests.test_blackbox_v2_runner import _SUCCESS_SCRIPT

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(
                _delivery(root / "incoming", script=_SUCCESS_SCRIPT),
                schemes_root=root / "schemes",
                platform_inputs=["api-wind-date-v1"],
            )
            config = load_scheme_config(scheme_dir / "config.yaml")
            frames = _snapshot_frames()
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=root / "snapshots",
                expected_columns={name: list(frame.columns) for name, frame in frames.items()},
                schema_version="data-bridge-v1",
            )
            request = BlackboxRequest(
                request_id="trial-request",
                predict_date="2026-07-15",
                feature_date="2026-07-15",
                target_date="2026-07-16",
                daily_cutoff_key="2026-07-15",
                weekly_cutoff_key="202627",
                monthly_cutoff_key="202606",
            )
            calendar_artifact = freeze_platform_input(
                "api-wind-date-v1",
                pd.DataFrame(
                    {
                        "rdate": ["2026-07-13", "2026-07-14", "2026-07-15"],
                        "week_id": ["202627", "202627", "202627"],
                    }
                ),
                weekly_cutoff_key=request.weekly_cutoff_key,
                audit_provenance={"source_kind": "harness_database"},
            )
            bundle = compose_blackbox_input_bundle(
                snapshot,
                platform_input_ids=config.platform_inputs,
                platform_input_artifacts=[calendar_artifact],
            )
            state = InputState(
                snapshot=snapshot,
                request=request,
                bundle=bundle,
            )
            ctx = _context(root, config)
            gates = [BlackboxCompareGate()]
            with patch("harness.blackbox_v2.gates._ensure_input_state", return_value=state):
                with patch(
                    "harness.blackbox_v2.gates._profile",
                    return_value=RuntimeProfile.for_tests(),
                ):
                    results = [gate.run(ctx) for gate in gates]
            self.assertFalse((root / "reports").exists())
            for result in results:
                evidence = {item.key: item.value for item in result.evidence}
                self.assertEqual(
                    evidence["data_snapshot_id"],
                    bundle.combined_snapshot_id,
                )
                self.assertEqual(
                    evidence["parent_data_snapshot_id"],
                    snapshot.snapshot_id,
                )
                self.assertEqual(
                    evidence["platform_input_ids"],
                    ["api-wind-date-v1"],
                )
                self.assertEqual(
                    evidence["platform_input_hashes"],
                    {"api-wind-date-v1": calendar_artifact.sha256},
                )
            self.assertFalse(
                (root / "reports" / "blackbox_v2" / "runtime_views").exists()
            )

        self.assertTrue(all(result.passed for result in results), [result.errors for result in results])


    def test_shadow_failure_restores_exact_config_and_marks_compensated(self) -> None:
        from harness.blackbox_v2.gates import BlackboxShadowRegisterGate, PassedAllRun
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(_delivery(root / "incoming"), schemes_root=root / "schemes")
            config_path = scheme_dir / "config.yaml"
            original = config_path.read_text(encoding="utf-8")
            config = load_scheme_config(config_path)
            token = build_operation(
                config.scheme_id,
                "shadow_register",
                "2026-07-16",
                scheme_version=config.scheme_version,
                harness_run_id="hr_passed",
            )
            ctx = GateContext(
                scheme_id=config.scheme_id,
                predict_date="2026-07-16",
                project_root=root,
                config=config,
                operation=token,
                engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
            )
            state = {"version": "draft", "registry": "paused"}
            passed = PassedAllRun("hr_passed", "snapshot-test")

            def read_state(_engine, _cfg):
                return SimpleNamespace(
                    version_status=state["version"],
                    registry_status=state["registry"],
                )

            def compensate(_engine, _cfg, **kwargs):
                state["version"] = kwargs["version_status"]
                state["registry"] = kwargs["registry_status"]

            with (
                patch("harness.blackbox_v2.gates._verify_passed_all", return_value=passed),
                patch("harness.blackbox_v2.gates._environment_fingerprint", return_value="e" * 64),
                patch("harness.blackbox_v2.gates._read_shadow_state", side_effect=read_state),
                patch("harness.blackbox_v2.gates._register_shadow", side_effect=RuntimeError("injected")),
                patch("scheduler.repository.apply_blackbox_lifecycle_state", side_effect=compensate),
            ):
                result = BlackboxShadowRegisterGate().run(ctx)

            evidence = {item.key: item.value for item in result.evidence}
            journal = json.loads(Path(evidence["journal_path"]).read_text(encoding="utf-8"))
            self.assertFalse(result.passed)
            self.assertTrue(evidence["compensated"])
            self.assertEqual(journal["phase"], "compensated")
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)
            self.assertEqual(state, {"version": "draft", "registry": "paused"})


    def test_static_gate_accepts_exact_two_file_delivery(self) -> None:
        from harness.blackbox_v2.gates import BlackboxStaticGate
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(_delivery(root / "incoming"), schemes_root=root / "schemes")
            config = load_scheme_config(scheme_dir / "config.yaml")
            result = BlackboxStaticGate().run(_context(root, config))

        self.assertTrue(result.passed, result.errors)


def _context(root: Path, config) -> GateContext:
    return GateContext(
        scheme_id="trial_10y",
        predict_date="2026-07-16",
        project_root=root,
        config=config,
    )


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
    }
