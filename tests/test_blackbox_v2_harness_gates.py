from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.context import GateContext
import pandas as pd


class BlackboxV2HarnessGateTests(unittest.TestCase):
    def test_comparison_requests_normalize_databridge_daily_timestamps(self) -> None:
        from harness.blackbox_v2.gates import _comparison_requests
        from shared.blackbox_v2.contracts import BlackboxRequest
        from shared.blackbox_v2.snapshot import create_snapshot_from_frames

        frames = _snapshot_frames()
        frames["daily_output.csv"]["date"] = [
            "2026/07/13 00:00",
            "2026/07/14 00:00",
            "2026/07/15 00:00",
        ]
        request = BlackboxRequest(
            request_id="trial-request",
            predict_date="2026-07-15",
            feature_date="2026-07-15",
            target_date="2026-07-16",
            daily_cutoff_key="2026-07-15",
            weekly_cutoff_key="202627",
            monthly_cutoff_key="202606",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=Path(tmpdir),
                expected_columns={name: list(frame.columns) for name, frame in frames.items()},
                schema_version="data-bridge-v1",
            )
            requests = _comparison_requests(request, snapshot.data_dir)

        self.assertEqual(requests[0].daily_cutoff_key, "2026-07-14")
        self.assertEqual(requests[1].daily_cutoff_key, "2026-07-15")

    def test_cleanup_removes_runtime_snapshot_but_keeps_audit_state(self) -> None:
        from harness.blackbox_v2.gates import cleanup_runtime_input

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_dir = root / "reports"
            gate_root = report_dir / "blackbox_v2"
            snapshot_root = gate_root / "runtime_snapshot" / "snapshot-test"
            (snapshot_root / "data").mkdir(parents=True)
            state_path = gate_root / "input_state.json"
            state_path.write_text(
                json.dumps({"snapshot_root": str(snapshot_root)}),
                encoding="utf-8",
            )
            ctx = GateContext(
                scheme_id="trial",
                predict_date="2026-07-16",
                project_root=root,
                report_dir=report_dir,
            )

            cleanup_runtime_input(ctx)

            self.assertFalse((gate_root / "runtime_snapshot").exists())
            self.assertTrue(state_path.is_file())

    def test_comparison_requests_use_distinct_existing_cutoffs(self) -> None:
        from harness.blackbox_v2.gates import _comparison_requests
        from shared.blackbox_v2.contracts import BlackboxRequest
        from shared.blackbox_v2.snapshot import create_snapshot_from_frames

        frames = _snapshot_frames()
        request = BlackboxRequest(
            request_id="trial-request",
            predict_date="2026-07-15",
            feature_date="2026-07-15",
            target_date="2026-07-16",
            daily_cutoff_key="2026-07-15",
            weekly_cutoff_key="202627",
            monthly_cutoff_key="202606",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=Path(tmpdir),
                expected_columns={name: list(frame.columns) for name, frame in frames.items()},
                schema_version="data-bridge-v1",
            )
            requests = _comparison_requests(request, snapshot.data_dir)

        self.assertEqual(len(requests), 2)
        self.assertEqual({item.daily_cutoff_key for item in requests}, {"2026-07-14", "2026-07-15"})
        self.assertEqual({item.weekly_cutoff_key for item in requests}, {"202626", "202627"})
        self.assertEqual({item.monthly_cutoff_key for item in requests}, {"202605", "202606"})

    def test_automatic_execution_gates_run_end_to_end_without_business_writes(self) -> None:
        from harness.blackbox_v2.gates import (
            BlackboxApiReadinessGate,
            BlackboxBacktestGate,
            BlackboxCompareGate,
            BlackboxDryRunGate,
            BlackboxUnitGate,
            InputState,
        )
        from scheduler.blackbox_v2_runner import RuntimeProfile
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.contracts import BlackboxRequest
        from shared.blackbox_v2.intake import intake_delivery
        from shared.blackbox_v2.requests import write_request
        from shared.blackbox_v2.snapshot import create_snapshot_from_frames
        from tests.test_blackbox_v2_runner import _SUCCESS_SCRIPT

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(
                _delivery(root / "incoming", script=_SUCCESS_SCRIPT),
                schemes_root=root / "schemes",
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
            state = InputState(snapshot=snapshot, request_path=request_path, request=request)
            ctx = _context(root, config)
            gates = [
                BlackboxUnitGate(),
                BlackboxDryRunGate(),
                BlackboxCompareGate(),
                BlackboxBacktestGate(),
                BlackboxApiReadinessGate(),
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

        self.assertTrue(all(result.passed for result in results), [result.errors for result in results])

    def test_shadow_register_requires_scoped_token_and_keeps_registry_paused(self) -> None:
        from harness.authorization import issue_token
        from harness.blackbox_v2.gates import BlackboxShadowRegisterGate, PassedAllRun
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(_delivery(root / "incoming"), schemes_root=root / "schemes")
            config = load_scheme_config(scheme_dir / "config.yaml")
            token = issue_token(
                "trial_10y",
                "shadow_register",
                "2026-07-16",
                scheme_version=config.scheme_version,
                harness_run_id="hr_passed",
            )
            ctx = GateContext(
                scheme_id="trial_10y",
                predict_date="2026-07-16",
                project_root=root,
                report_dir=root / "reports" / "shadow",
                config=config,
                authorization=token,
                engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
            )
            passed = PassedAllRun(
                harness_run_id="hr_passed",
                report_uri=root / "reports" / "all",
                data_snapshot_id="snapshot-test",
            )
            with patch("harness.blackbox_v2.gates._verify_passed_all", return_value=passed):
                with patch("harness.blackbox_v2.gates._environment_fingerprint", return_value="e" * 64):
                    with patch("harness.blackbox_v2.gates._register_shadow") as register:
                        result = BlackboxShadowRegisterGate().run(ctx)

            updated = load_scheme_config(scheme_dir / "config.yaml")

        self.assertTrue(result.passed, result.errors)
        self.assertEqual(updated.status, "paused")
        self.assertEqual(updated.version_status, "shadow")
        validated_cfg, shadow_cfg = register.call_args.args[1:]
        self.assertEqual(validated_cfg.version_status, "validated")
        self.assertEqual(shadow_cfg.version_status, "shadow")
        self.assertEqual(shadow_cfg.data_snapshot_id, "snapshot-test")
        self.assertEqual(shadow_cfg.environment_fingerprint, "e" * 64)

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


if __name__ == "__main__":
    unittest.main()
