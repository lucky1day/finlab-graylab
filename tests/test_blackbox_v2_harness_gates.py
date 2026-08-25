from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.context import GateContext
from harness.operation import build_direct_operation
import pandas as pd
from sqlalchemy import create_engine, text


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


    def test_passed_all_accepts_directory_report_uri(self) -> None:
        from harness.blackbox_v2.gates import _verify_passed_all
        from harness.registry import BLACKBOX_AUTO_SEQUENCE
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(
                _delivery(root / "incoming"),
                schemes_root=root / "schemes",
            )
            config = load_scheme_config(scheme_dir / "config.yaml")
            report_dir = root / "reports" / "all"
            state_dir = report_dir / "blackbox_v2"
            state_dir.mkdir(parents=True)
            (state_dir / "input_state.json").write_text(
                json.dumps(
                    {
                        "snapshot_id": "snapshot-parent",
                        "combined_snapshot_id": "snapshot-combined",
                        "generation_id": "generation-test",
                        "runtime_profile": "blackbox-v2-v1",
                        "environment_fingerprint": "env-test",
                    }
                ),
                encoding="utf-8",
            )
            engine = create_engine("sqlite:///:memory:")
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE TABLE t_harness_runs ("
                        "harness_run_id TEXT, scheme_id TEXT, scheme_version TEXT, "
                        "stage TEXT, status TEXT, finished_at TEXT, report_uri TEXT)"
                    )
                )
                conn.execute(
                    text(
                        "CREATE TABLE t_harness_gate_results ("
                        "harness_run_id TEXT, gate_name TEXT, status TEXT)"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO t_harness_runs VALUES ("
                        "'hr-directory', :scheme_id, :scheme_version, "
                        "'all', 'passed', '2026-08-10 12:00:00', :report_uri)"
                    ),
                    {
                        "scheme_id": config.scheme_id,
                        "scheme_version": config.scheme_version,
                        "report_uri": str(report_dir),
                    },
                )
                conn.execute(
                    text(
                        "INSERT INTO t_harness_gate_results VALUES ("
                        "'hr-directory', :gate_name, 'passed')"
                    ),
                    [
                        {"gate_name": gate_name}
                        for gate_name in BLACKBOX_AUTO_SEQUENCE
                    ],
                )

            try:
                passed = _verify_passed_all(engine, config)
            finally:
                engine.dispose()

        self.assertEqual(passed.harness_run_id, "hr-directory")
        self.assertEqual(passed.report_uri, report_dir)
        self.assertEqual(passed.data_snapshot_id, "snapshot-combined")
        self.assertEqual(passed.generation_id, "generation-test")
        self.assertEqual(passed.runtime_profile, "blackbox-v2-v1")
        self.assertEqual(passed.environment_fingerprint, "env-test")



    def test_input_state_captures_declared_platform_input_in_read_only_transaction(self) -> None:
        from harness.blackbox_v2.gates import (
            _ensure_input_state,
            _read_input_state,
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
                report_dir=root / "reports",
                config=config,
                engine_factory=lambda: engine,
            )
            with (
                patch(
                    "harness.blackbox_v2.gates.build_blackbox_input_snapshot",
                    return_value=snapshot,
                ),
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
            raw_state = json.loads(
                (
                    root
                    / "reports"
                    / "blackbox_v2"
                    / "input_state.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                raw_state["combined_snapshot_id"],
                state.bundle.combined_snapshot_id,
            )
            self.assertEqual(
                raw_state["platform_input_ids"],
                ["api-wind-date-v1"],
            )
            self.assertEqual(
                raw_state["platform_input_artifacts"][0]["sha256"],
                artifact.sha256,
            )
            self.assertEqual(
                raw_state["audit_manifest"]["base_snapshot"][
                    "generation_id"
                ],
                "generation-test",
            )
            self.assertEqual(
                raw_state["audit_manifest"]["base_snapshot"][
                    "refresh_date"
                ],
                "2026-07-15",
            )
            self.assertTrue(
                (
                    root
                    / "reports"
                    / "blackbox_v2"
                    / "input_state.initialized"
                ).is_file()
            )
            self.assertNotIn(
                "content_base64",
                raw_state["platform_input_artifacts"][0],
            )
            self.assertTrue(
                Path(
                    raw_state["platform_input_artifacts"][0][
                        "runtime_content_path"
                    ]
                ).is_file()
            )
            self.assertRegex(
                raw_state["request_sha256"],
                r"^[0-9a-f]{64}$",
            )
            reloaded = _read_input_state(
                root
                / "reports"
                / "blackbox_v2"
                / "input_state.json"
            )
            self.assertEqual(
                reloaded.bundle.combined_snapshot_id,
                state.bundle.combined_snapshot_id,
            )
            self.assertEqual(
                reloaded.bundle.audit_manifest,
                state.bundle.audit_manifest,
            )
            self.assertEqual(
                reloaded.bundle.base_snapshot.generation_id,
                "generation-test",
            )
            self.assertEqual(
                reloaded.bundle.base_snapshot.refresh_date,
                "2026-07-15",
            )
            state.request_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Request"):
                _read_input_state(
                    root
                    / "reports"
                    / "blackbox_v2"
                    / "input_state.json"
                )
            (
                root
                / "reports"
                / "blackbox_v2"
                / "input_state.json"
            ).unlink()
            with self.assertRaisesRegex(ValueError, "initialized"):
                _ensure_input_state(ctx)

    def test_monthly_persist_input_treats_predict_date_as_target_cutoff(
        self,
    ) -> None:
        from harness.blackbox_v2.gates import _ensure_input_state
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery
        from shared.blackbox_v2.snapshot import (
            CutoffKeys,
            create_snapshot_from_frames,
        )

        class Calendar:
            requested: str | None = None

            def previous_trading_day(self, value):
                self.requested = value
                return "2026-05-29"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _delivery(root / "incoming")
            metadata_path = delivery / "trial_10y.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata.update(
                {
                    "task_type": "monthly",
                    "horizon": 1,
                    "target_rule": (
                        "target_month_observation_yield_vs_"
                        "feature_month_observation_yield"
                    ),
                }
            )
            metadata_path.write_text(
                json.dumps(metadata),
                encoding="utf-8",
            )
            scheme_dir = intake_delivery(
                delivery,
                schemes_root=root / "schemes",
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
            cutoffs = CutoffKeys("2026-05-29", "202622", "202604")
            calendar = Calendar()
            engine = SimpleNamespace(dispose=lambda: None)
            ctx = GateContext(
                scheme_id=config.scheme_id,
                predict_date="2026-06-01",
                project_root=root,
                report_dir=root / "reports" / "persist",
                config=config,
                persist_backtest=True,
                engine_factory=lambda: engine,
            )
            with (
                patch(
                    "harness.blackbox_v2.gates."
                    "build_blackbox_input_snapshot",
                    return_value=snapshot,
                ),
                patch(
                    "harness.blackbox_v2.gates."
                    "resolve_blackbox_input_cutoffs",
                    return_value=cutoffs,
                ) as resolve_cutoffs,
                patch(
                    "harness.blackbox_v2.gates."
                    "_data_bridge_provenance",
                    return_value={
                        "generation_id": "generation-test",
                        "refresh_date": "2026-07-15",
                        "refreshed_at": "2026-07-15T00:02:00+08:00",
                        "business_digest": "digest",
                        "runtime_profile": "blackbox-v2-v1",
                        "environment_fingerprint": "e" * 64,
                    },
                ),
                patch(
                    "harness.blackbox_v2.gates.get_calendar",
                    return_value=calendar,
                ),
                patch(
                    "harness.blackbox_v2.gates.build_live_request",
                    side_effect=AssertionError(
                        "persist cutoff must not build a live Request"
                    ),
                ),
            ):
                state = _ensure_input_state(ctx)

        self.assertEqual(calendar.requested, "2026-06-01")
        resolve_cutoffs.assert_called_once_with(
            snapshot,
            feature_date="2026-05-29",
            engine=engine,
        )
        self.assertEqual(state.request.predict_date, "2026-05-29")
        self.assertEqual(state.request.feature_date, "2026-05-29")
        self.assertEqual(state.request.target_date, "2026-06-01")

    def test_non_input_gate_fails_closed_on_missing_or_invalid_input_state(self) -> None:
        from harness.blackbox_v2.gates import (
            INPUT_STATE_INITIALIZED_SEAL,
            _ensure_input_state,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ctx = GateContext(
                scheme_id="trial",
                predict_date="2026-07-16",
                project_root=root,
                report_dir=root / "reports",
            )
            gate_root = ctx.report_dir / "blackbox_v2"
            gate_root.mkdir(parents=True)
            (gate_root / "request.json").write_text(
                "{}\n",
                encoding="utf-8",
            )
            with patch(
                "harness.blackbox_v2.gates.build_blackbox_input_snapshot"
            ) as build, self.assertRaisesRegex(ValueError, "residual"):
                _ensure_input_state(ctx)
            build.assert_not_called()

            (gate_root / "request.json").unlink()
            seal_path = gate_root / "input_state.initialized"
            seal_path.write_bytes(INPUT_STATE_INITIALIZED_SEAL)
            with patch(
                "harness.blackbox_v2.gates.build_blackbox_input_snapshot"
            ) as build, self.assertRaisesRegex(ValueError, "initialized"):
                _ensure_input_state(ctx)
            build.assert_not_called()

            state_path = gate_root / "input_state.json"
            state_path.write_text("{not-json", encoding="utf-8")
            with (
                patch(
                    "harness.blackbox_v2.gates.build_blackbox_input_snapshot"
                ) as build,
                self.assertRaises(Exception),
            ):
                _ensure_input_state(ctx)
            build.assert_not_called()







    def test_automatic_execution_gates_run_end_to_end_without_business_writes(self) -> None:
        from harness.blackbox_v2.gates import (
            BlackboxCompareGate,
            BlackboxUnitGate,
            InputState,
        )
        from scheduler.blackbox_v2_runner import RuntimeProfile
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.contracts import BlackboxRequest
        from shared.blackbox_v2.intake import intake_delivery
        from shared.blackbox_v2.requests import write_request
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
            request_path = write_request(request, root / "request.json")
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
                request_path=request_path,
                request=request,
                bundle=bundle,
            )
            ctx = _context(root, config)
            gates = [
                BlackboxUnitGate(),
                BlackboxCompareGate(),
            ]
            with patch("harness.blackbox_v2.gates._ensure_input_state", return_value=state):
                with patch(
                    "harness.blackbox_v2.gates._profile",
                    return_value=RuntimeProfile.for_tests(),
                ):
                    results = [gate.run(ctx) for gate in gates]
            unit_root = root / "reports" / "blackbox_v2" / "unit"
            self.assertTrue((unit_root / "request" / "invalid_request.json").is_file())
            self.assertFalse((unit_root / "invalid_request.json").exists())
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

    def test_register_shadow_persists_exact_shadow_db_state(self) -> None:
        from harness.blackbox_v2.gates import _register_shadow
        from tests.test_repository_registry import _CaptureEngine, _blackbox_config

        engine = _CaptureEngine()
        validated_cfg = _blackbox_config(status="paused", version_status="validated")
        shadow_cfg = _blackbox_config(status="paused", version_status="shadow")

        state = _register_shadow(engine, validated_cfg, shadow_cfg)

        self.assertEqual(engine.store["version_row"]["status"], "shadow")
        self.assertEqual({row["status"] for row in engine.store["registry_rows"]}, {"paused"})
        self.assertEqual(engine.store["version_row"]["environment_fingerprint"], "e" * 64)
        self.assertEqual(engine.store["version_row"]["data_snapshot_id"], "snapshot-1")
        self.assertEqual(state.version_status, "shadow")
        self.assertEqual(state.registry_status, "paused")

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
                report_dir=root / "reports" / "shadow",
                config=config,
                operation=token,
                engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
            )
            state = {"version": "draft", "registry": "paused"}
            passed = PassedAllRun("hr_passed", root / "reports" / "all", "snapshot-test")

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


    def test_static_gate_keeps_immutable_historical_metadata_compatible(self) -> None:
        from harness.blackbox_v2.gates import BlackboxStaticGate
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(
                _delivery(root / "incoming"),
                schemes_root=root / "schemes",
            )
            metadata_path = scheme_dir / "delivery" / "trial_10y.json"
            metadata_path.chmod(0o644)
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            payload.pop("owner")
            payload.pop("description")
            metadata_path.write_text(json.dumps(payload), encoding="utf-8")
            _write_legacy_metadata_policy(root, metadata_path)
            config = load_scheme_config(scheme_dir / "config.yaml")

            result = BlackboxStaticGate().run(_context(root, config))

        self.assertTrue(result.passed, result.errors)


    def test_static_gate_rejects_changed_metadata_under_legacy_scheme_id(self) -> None:
        from harness.blackbox_v2.gates import BlackboxStaticGate
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(
                _delivery(root / "incoming"),
                schemes_root=root / "schemes",
            )
            metadata_path = scheme_dir / "delivery" / "trial_10y.json"
            metadata_path.chmod(0o644)
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            payload.pop("owner")
            metadata_path.write_text(json.dumps(payload), encoding="utf-8")
            _write_legacy_metadata_policy(root, metadata_path)
            payload["algorithm_version"] = "changed-without-owner"
            metadata_path.write_text(json.dumps(payload), encoding="utf-8")
            config = load_scheme_config(scheme_dir / "config.yaml")

            result = BlackboxStaticGate().run(_context(root, config))

        self.assertFalse(result.passed)
        self.assertIn("metadata SHA-256", "\n".join(result.errors))





    def test_static_gate_records_declared_platform_input_provider(self) -> None:
        from harness.blackbox_v2.gates import BlackboxStaticGate
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(
                _delivery(root / "incoming"),
                schemes_root=root / "schemes",
                platform_inputs=["api-wind-date-v1"],
            )
            config = load_scheme_config(scheme_dir / "config.yaml")
            result = BlackboxStaticGate().run(_context(root, config))

        evidence = {item.key: item.value for item in result.evidence}
        self.assertTrue(result.passed, result.errors)
        self.assertEqual(
            evidence["platform_inputs"],
            ["api-wind-date-v1"],
        )

    def test_static_gate_rejects_network_and_database_imports(self) -> None:
        from harness.blackbox_v2.gates import BlackboxStaticGate
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _delivery(root / "incoming", script="import socket\nimport sqlalchemy\n")
            scheme_dir = intake_delivery(delivery, schemes_root=root / "schemes")
            config = load_scheme_config(scheme_dir / "config.yaml")
            result = BlackboxStaticGate().run(_context(root, config))

        self.assertFalse(result.passed)
        self.assertIn("forbidden import socket", "\n".join(result.errors))
        self.assertIn("forbidden import sqlalchemy", "\n".join(result.errors))


def _context(root: Path, config) -> GateContext:
    return GateContext(
        scheme_id="trial_10y",
        predict_date="2026-07-16",
        project_root=root,
        report_dir=root / "reports",
        config=config,
    )


def _delivery(path: Path, *, script: str = "import argparse\nimport json\n") -> Path:
    registry_path = path.parent / "deploy" / "scheme_owner_v1.json"
    if not registry_path.exists():
        registry_path.parent.mkdir(parents=True)
        registry_path.write_text(
            json.dumps(
                {"schema_version": "scheme-owner-v1", "owners": {}},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
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


def _write_legacy_metadata_policy(
    root: Path,
    metadata_path: Path | None,
) -> None:
    deploy = root / "deploy"
    deploy.mkdir(parents=True, exist_ok=True)
    (deploy / "blackbox_v2_legacy_metadata_v1.json").write_text(
        json.dumps(
            {
                "schema_version": "blackbox-v2-legacy-metadata-v1",
                "metadata_sha256": (
                    {}
                    if metadata_path is None
                    else {
                        "trial_10y": hashlib.sha256(
                            metadata_path.read_bytes()
                        ).hexdigest()
                    }
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


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


if __name__ == "__main__":
    unittest.main()
