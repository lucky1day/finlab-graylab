from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.context import GateContext


class BlackboxV2PersistedGateProvenanceTests(unittest.TestCase):
    def test_persist_succeeds_only_with_exact_all_stage_provenance(self) -> None:
        attempt = _run_attempt()

        self.assertTrue(attempt["result"].passed, attempt["result"].errors)
        self.assertEqual(attempt["events"], ["audit", "consume", "persist"])
        self.assertTrue(attempt["token_used"])
        self.assertTrue(attempt["token_replay_blocked"])
        self.assertTrue(attempt["audit_exists"])
        self.assertEqual(attempt["build_calls"], 1)
        self.assertEqual(attempt["run_calls"], 1)
        self.assertEqual(attempt["persist_calls"], 1)

    def test_snapshot_mismatch_blocks_before_algorithm_and_does_not_consume_token(self) -> None:
        attempt = _run_attempt(passed_updates={"data_snapshot_id": "snapshot-other"})

        _assert_pre_execution_block(self, attempt, "data_snapshot_id")

    def test_generation_mismatch_blocks_before_algorithm_and_does_not_consume_token(self) -> None:
        attempt = _run_attempt(passed_updates={"generation_id": "generation-other"})

        _assert_pre_execution_block(self, attempt, "generation_id")

    def test_runtime_profile_mismatch_blocks_before_algorithm_and_does_not_consume_token(self) -> None:
        attempt = _run_attempt(passed_updates={"runtime_profile": "blackbox-v2-other"})

        _assert_pre_execution_block(self, attempt, "runtime_profile")

    def test_environment_mismatch_blocks_before_algorithm_and_does_not_consume_token(self) -> None:
        attempt = _run_attempt(passed_updates={"environment_fingerprint": "d" * 64})

        _assert_pre_execution_block(self, attempt, "environment_fingerprint")

    def test_final_canonical_version_drift_blocks_before_audit_or_token_consumption(self) -> None:
        attempt = _run_attempt(drift_on_reload=2)

        self.assertFalse(attempt["result"].passed)
        self.assertIn("canonical version changed", "\n".join(attempt["result"].errors))
        self.assertEqual(attempt["build_calls"], 1)
        self.assertEqual(attempt["run_calls"], 1)
        self.assertEqual(attempt["persist_calls"], 0)
        self.assertEqual(attempt["events"], [])
        self.assertFalse(attempt["token_used"])
        self.assertFalse(attempt["token_replay_blocked"])
        self.assertFalse(attempt["audit_exists"])

    def test_audit_failure_leaves_token_unused_and_writes_no_business_rows(self) -> None:
        attempt = _run_attempt(audit_failure=True)

        self.assertFalse(attempt["result"].passed)
        self.assertIn("injected audit failure", "\n".join(attempt["result"].errors))
        self.assertEqual(attempt["events"], ["audit"])
        self.assertFalse(attempt["token_used"])
        self.assertFalse(attempt["token_replay_blocked"])
        self.assertFalse(attempt["audit_exists"])
        self.assertEqual(attempt["persist_calls"], 0)

    def test_database_failure_happens_after_audit_and_consumes_token(self) -> None:
        attempt = _run_attempt(persist_failure=True)

        self.assertFalse(attempt["result"].passed)
        self.assertIn("injected database failure", "\n".join(attempt["result"].errors))
        self.assertEqual(attempt["events"], ["audit", "consume", "persist"])
        self.assertTrue(attempt["token_used"])
        self.assertTrue(attempt["token_replay_blocked"])
        self.assertTrue(attempt["audit_exists"])
        self.assertEqual(attempt["persist_calls"], 1)


def _assert_pre_execution_block(
    testcase: unittest.TestCase,
    attempt: dict[str, object],
    evidence_name: str,
) -> None:
    result = attempt["result"]
    testcase.assertFalse(result.passed)
    testcase.assertIn(evidence_name, "\n".join(result.errors))
    testcase.assertEqual(attempt["build_calls"], 0)
    testcase.assertEqual(attempt["run_calls"], 0)
    testcase.assertEqual(attempt["persist_calls"], 0)
    testcase.assertEqual(attempt["events"], [])
    testcase.assertFalse(attempt["token_used"])
    testcase.assertFalse(attempt["token_replay_blocked"])
    testcase.assertFalse(attempt["audit_exists"])


def _run_attempt(
    *,
    passed_updates: dict[str, str] | None = None,
    drift_on_reload: int | None = None,
    audit_failure: bool = False,
    persist_failure: bool = False,
) -> dict[str, object]:
    from harness.authorization import (
        authorization_token_hash,
        issue_token,
        mark_token_used as real_mark_token_used,
        used_tokens_path,
        verify_authorization,
        write_authorization_audit as real_write_authorization_audit,
    )
    from harness.blackbox_v2.gates import BlackboxBacktestGate, InputState, PassedAllRun
    from scheduler.discovery import load_scheme_config as real_load_scheme_config
    from shared.blackbox_v2.intake import intake_delivery
    from shared.blackbox_v2.requests import write_request
    from shared.blackbox_v2.snapshot import create_snapshot_from_frames
    from tests.test_blackbox_v2_backtest_persistence import _cases, _output
    from tests.test_blackbox_v2_harness_gates import _delivery, _snapshot_frames

    with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
        os.environ,
        {"HARNESS_AUTH_SECRET": "persisted-gate-test-secret"},
    ):
        root = Path(tmpdir)
        scheme_dir = intake_delivery(_delivery(root / "incoming"), schemes_root=root / "schemes")
        config_path = scheme_dir / "config.yaml"
        config = real_load_scheme_config(config_path)
        frames = _snapshot_frames()
        snapshot = create_snapshot_from_frames(
            frames,
            output_root=root / "snapshots",
            expected_columns={name: list(frame.columns) for name, frame in frames.items()},
            schema_version="data-bridge-v1",
        )
        request = _cases(1)[0].request
        state = InputState(snapshot, write_request(request, root / "request.json"), request)
        passed = PassedAllRun(
            harness_run_id="hr_passed",
            report_uri=root / "reports" / "all",
            data_snapshot_id=snapshot.snapshot_id,
            generation_id="generation-current",
            runtime_profile="blackbox-v2-v1",
            environment_fingerprint="e" * 64,
        )
        if passed_updates:
            passed = replace(passed, **passed_updates)
        token = issue_token(
            config.scheme_id,
            "backtest_persist",
            "2026-07-16",
            scheme_version=config.scheme_version,
            harness_run_id="hr_passed",
            ttl_seconds=300,
            issued_by="platform-test",
        )
        report_dir = root / "reports" / "persist"
        ctx = GateContext(
            scheme_id=config.scheme_id,
            predict_date="2026-07-16",
            project_root=root,
            report_dir=report_dir,
            config=config,
            authorization=token,
            persist_backtest=True,
            engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
        )
        provenance = {
            "generation_id": "generation-current",
            "refresh_date": "2026-07-16",
            "refreshed_at": "2026-07-16T05:40:00+08:00",
            "business_digest": "digest-current",
            "runtime_profile": "blackbox-v2-v1",
            "environment_fingerprint": "e" * 64,
        }
        reload_count = 0

        def reload_config(path: Path):
            nonlocal reload_count
            reload_count += 1
            current = real_load_scheme_config(path)
            if reload_count == drift_on_reload:
                return replace(current, scheme_version="changed-version")
            return current

        events: list[str] = []

        def write_audit(*args, **kwargs):
            events.append("audit")
            if audit_failure:
                raise OSError("injected audit failure")
            return real_write_authorization_audit(*args, **kwargs)

        def consume(*args, **kwargs):
            events.append("consume")
            return real_mark_token_used(*args, **kwargs)

        def persist(*_args, **_kwargs):
            events.append("persist")
            if persist_failure:
                raise RuntimeError("injected database failure")
            return 301

        counts = [
            {"t_backtest_runs": 10, "t_backtest_predictions": 1000, "t_backtest_monthly_metrics": 20},
            {"t_backtest_runs": 11, "t_backtest_predictions": 1100, "t_backtest_monthly_metrics": 24},
        ]
        with (
            patch("harness.blackbox_v2.gates._ensure_input_state", return_value=state),
            patch("harness.blackbox_v2.gates._verify_passed_all", return_value=passed),
            patch("harness.blackbox_v2.gates._data_bridge_provenance", return_value=provenance),
            patch("harness.blackbox_v2.gates._environment_fingerprint", return_value="e" * 64),
            patch("harness.blackbox_v2.gates.load_scheme_config", side_effect=reload_config),
            patch("harness.blackbox_v2.gates.build_historical_cases", return_value=_cases(100)) as build_cases,
            patch("harness.blackbox_v2.gates.run_blackbox_historical_backtest", return_value=_output()) as run_history,
            patch("harness.blackbox_v2.gates.write_authorization_audit", side_effect=write_audit),
            patch("harness.blackbox_v2.gates.mark_token_used", side_effect=consume),
            patch("harness.blackbox_v2.gates.persist_backtest_output_atomic", side_effect=persist) as persist_call,
            patch("harness.blackbox_v2.gates.snapshot_backtest_scope_counts", side_effect=counts),
        ):
            result = BlackboxBacktestGate().run(ctx)

        replay_path = used_tokens_path(root)
        used = json.loads(replay_path.read_text(encoding="utf-8")) if replay_path.exists() else []
        _, replay_errors = verify_authorization(
            token,
            scheme_id=config.scheme_id,
            action="backtest_persist",
            predict_date="2026-07-16",
            used_store_path=replay_path,
        )
        return {
            "result": result,
            "events": events,
            "token_used": authorization_token_hash(token) in used,
            "token_replay_blocked": "authorization token already used" in replay_errors,
            "audit_exists": (report_dir / "backtest_authorization" / "authorization.json").is_file(),
            "build_calls": build_cases.call_count,
            "run_calls": run_history.call_count,
            "persist_calls": persist_call.call_count,
        }


if __name__ == "__main__":
    unittest.main()
