from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harness import cli
from harness.authorization import issue_token, verify_authorization
from harness.context import GateContext
from harness.registry import gate_for_name as resolve_gate
from harness.result import Evidence, GateResult, GateStatus


PLAN_SHA = "a" * 64
READY_PLAN = {
    "schema_version": "active-signal-gap-plan-v6",
    "status": "READY",
    "plan_sha256": PLAN_SHA,
    "counts": {
        "blocked": 0,
        "actionable": 1,
        "open_gap": 1,
        "GRAY_LIVE_GAP": 1,
    },
    "actions": [{"action": "GRAY_LIVE_GAP"}],
}
PRESENT_PLAN = {
    "schema_version": "active-signal-gap-plan-v6",
    "status": "READY",
    "plan_sha256": "b" * 64,
    "counts": {
        "blocked": 0,
        "actionable": 0,
        "open_gap": 0,
        "GRAY_LIVE_GAP": 0,
    },
    "actions": [{"action": "SKIP_PRESENT"}],
}
BLOCKED_PLAN = {
    "schema_version": "active-signal-gap-plan-v6",
    "status": "BLOCKED",
    "plan_sha256": "c" * 64,
    "failure_code": "INPUT_AUTHORITY_BLOCKED",
    "counts": {"blocked": 1, "actionable": 0, "open_gap": 1},
    "actions": [{"action": "BLOCKED"}],
}
UNSUPPORTED_PLAN = {
    "schema_version": "active-signal-gap-plan-v6",
    "status": "READY",
    "plan_sha256": "d" * 64,
    "counts": {
        "blocked": 0,
        "actionable": 1,
        "open_gap": 1,
        "GRAY_LIVE_GAP": 0,
        "FULL_CANONICAL_RUN_REQUIRED": 1,
    },
    "actions": [{"action": "FULL_CANONICAL_RUN_REQUIRED"}],
}


def test_harness_help_only_exposes_single_gap_fill_entry() -> None:
    help_text = cli._build_parser().format_help()

    assert "signal-gap-fill" in help_text
    assert "signal-gap-native-" + "artifact" not in help_text


def _claim() -> SimpleNamespace:
    return SimpleNamespace(
        base_scheme_id="demo_blackbox",
        scheme_version="version-1",
        predict_date="2026-08-08",
        target_keys=(
            {
                "registry_scheme_id": "demo_blackbox__h1__10Y",
                "base_scheme_id": "demo_blackbox",
                "target_tenor": "10Y",
                "horizon": 1,
                "task_type": "weekly_point",
                "predict_date": "2026-08-08",
                "feature_date": "2026-08-07",
                "target_date": "2026-08-14",
                "prediction_phase": "gray_live",
            },
        ),
        source_authority={
            "authority_type": "databridge_current_generation",
            "generation_id": "generation-1",
        },
    )


def _passed_gate_result() -> GateResult:
    return GateResult(
        gate_name="signal-gap-fill",
        status=GateStatus.PASSED,
        passed=True,
        evidence=[
            Evidence(
                "signal_gap_fill",
                {
                    "status": "PASSED",
                    "completed": [
                        {
                            "base_scheme_id": "demo_blackbox",
                            "run_id": 101,
                            "records_written": 1,
                        }
                    ],
                },
            )
        ],
        errors=[],
        started_at="2026-08-09T00:00:00+00:00",
        finished_at="2026-08-09T00:00:01+00:00",
    )


def _failed_gate_result() -> GateResult:
    return GateResult(
        gate_name="signal-gap-fill",
        status=GateStatus.FAILED,
        passed=False,
        evidence=[],
        errors=["ALGORITHM_EXECUTION_FAILED"],
        started_at="2026-08-09T00:00:00+00:00",
        finished_at="2026-08-09T00:00:01+00:00",
    )


class SignalGapFillCliTests(unittest.TestCase):
    def test_parser_accepts_only_single_date_fill_interface(self) -> None:
        args = cli._build_parser().parse_args(
            ["signal-gap-fill", "--predict-date", "2026-08-08"]
        )

        self.assertEqual(args.predict_date, "2026-08-08")
        self.assertFalse(hasattr(args, "plan"))
        self.assertFalse(hasattr(args, "authorize"))
        self.assertFalse(hasattr(args, "algo_env"))

        with self.assertRaises(SystemExit):
            cli._build_parser().parse_args(
                ["signal-gap-fill", "--predict-date", "20260808"]
            )

    def test_manual_signal_gap_token_issuance_is_retired(self) -> None:
        with self.assertRaises(SystemExit):
            cli.main(
                [
                    "auth",
                    "issue",
                    "--scheme-id",
                    "demo_blackbox",
                    "--action",
                    "signal_gap_fill_write",
                    "--predict-date",
                    "2026-08-08",
                ]
            )

    def test_legacy_gray_backfill_entry_and_action_are_retired(self) -> None:
        with self.assertRaises(SystemExit):
            cli._build_parser().parse_args(
                [
                    "gate",
                    "gray-backfill",
                    "--scheme-id",
                    "demo_blackbox",
                    "--predict-date",
                    "2026-08-08",
                ]
            )

        ctx = GateContext(
            scheme_id="demo_blackbox",
            predict_date="2026-08-08",
            project_root=Path("/tmp/project"),
            report_dir=Path("/tmp/report"),
            config=SimpleNamespace(runtime_type="blackbox_v2"),
        )
        with self.assertRaisesRegex(ValueError, "unsupported Blackbox V2 gate"):
            resolve_gate("gray-backfill", ctx=ctx)

        with self.assertRaisesRegex(ValueError, "retired"):
            issue_token(
                "demo_blackbox",
                "gray_backfill_write",
                predict_date="2026-08-08",
            )
        authorization, errors = verify_authorization(
            None,
            scheme_id="demo_blackbox",
            action="gray_backfill_write",
            predict_date="2026-08-08",
            used_store_path=Path("/tmp/unused-token-store.json"),
        )
        self.assertIsNone(authorization)
        self.assertIn("retired", "\n".join(errors))

    def test_missing_hmac_secret_blocks_before_database_access(self) -> None:
        output = io.StringIO()
        with (
            patch.object(
                cli,
                "authorization_signing_enabled",
                return_value=False,
                create=True,
            ),
            patch.object(cli, "create_engine_from_env") as create_engine,
            redirect_stdout(output),
        ):
            exit_code = cli.main(
                ["signal-gap-fill", "--predict-date", "2026-08-08"]
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            json.loads(output.getvalue())["failure_code"],
            "SIGNAL_GAP_FILL_SIGNING_REQUIRED",
        )
        create_engine.assert_not_called()

    def test_blocked_plan_does_not_issue_tokens_or_run_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            gate_for_name = Mock()
            issue_token = Mock()
            with (
                patch.object(
                    cli,
                    "authorization_signing_enabled",
                    return_value=True,
                    create=True,
                ),
                patch.object(
                    cli,
                    "_plan_signal_gap_date",
                    return_value=BLOCKED_PLAN,
                    create=True,
                ),
                patch.object(
                    cli,
                    "issue_signal_gap_fill_token",
                    issue_token,
                    create=True,
                ),
                patch.object(cli, "gate_for_name", gate_for_name),
                redirect_stdout(output),
            ):
                exit_code = cli.main(
                    [
                        "signal-gap-fill",
                        "--predict-date",
                        "2026-08-08",
                        "--project-root",
                        directory,
                    ]
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertEqual(payload["failure_code"], "INPUT_AUTHORITY_BLOCKED")
        issue_token.assert_not_called()
        gate_for_name.assert_not_called()

    def test_no_gap_returns_success_without_token_or_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            gate_for_name = Mock()
            issue_token = Mock()
            with (
                patch.object(
                    cli,
                    "authorization_signing_enabled",
                    return_value=True,
                    create=True,
                ),
                patch.object(
                    cli,
                    "_plan_signal_gap_date",
                    return_value=PRESENT_PLAN,
                    create=True,
                ),
                patch.object(
                    cli,
                    "signal_gap_fill_authorization_claims",
                    return_value=(),
                    create=True,
                ),
                patch.object(
                    cli,
                    "issue_signal_gap_fill_token",
                    issue_token,
                    create=True,
                ),
                patch.object(cli, "gate_for_name", gate_for_name),
                redirect_stdout(output),
            ):
                exit_code = cli.main(
                    [
                        "signal-gap-fill",
                        "--predict-date",
                        "2026-08-08",
                        "--project-root",
                        directory,
                    ]
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "SKIP_PRESENT")
        issue_token.assert_not_called()
        gate_for_name.assert_not_called()

    def test_non_gray_gap_blocks_before_token_or_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            gate_for_name = Mock()
            issue_token = Mock()
            with (
                patch.object(
                    cli,
                    "authorization_signing_enabled",
                    return_value=True,
                ),
                patch.object(
                    cli,
                    "_plan_signal_gap_date",
                    return_value=UNSUPPORTED_PLAN,
                ),
                patch.object(
                    cli,
                    "signal_gap_fill_authorization_claims",
                    return_value=(),
                ),
                patch.object(
                    cli,
                    "issue_signal_gap_fill_token",
                    issue_token,
                ),
                patch.object(cli, "gate_for_name", gate_for_name),
                redirect_stdout(output),
            ):
                exit_code = cli.main(
                    [
                        "signal-gap-fill",
                        "--predict-date",
                        "2026-08-08",
                        "--project-root",
                        directory,
                    ]
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(
            payload["failure_code"],
            "SIGNAL_GAP_PLAN_NOT_GRAY_LIVE_ONLY",
        )
        issue_token.assert_not_called()
        gate_for_name.assert_not_called()

    def test_success_issues_exact_token_and_requires_postfill_zero_gap(
        self,
    ) -> None:
        claim = _claim()
        gate = Mock()
        gate.run.return_value = _passed_gate_result()
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with (
                patch.object(
                    cli,
                    "authorization_signing_enabled",
                    return_value=True,
                    create=True,
                ),
                patch.object(
                    cli,
                    "_plan_signal_gap_date",
                    side_effect=[READY_PLAN, PRESENT_PLAN],
                    create=True,
                ) as planner,
                patch.object(
                    cli,
                    "signal_gap_fill_authorization_claims",
                    side_effect=[(claim,), ()],
                    create=True,
                ),
                patch.object(
                    cli,
                    "issue_signal_gap_fill_token",
                    return_value="raw-secret-token",
                    create=True,
                ) as issue_token,
                patch.object(cli, "gate_for_name", return_value=gate),
                patch("getpass.getuser", return_value="macstudio0"),
                redirect_stdout(output),
            ):
                exit_code = cli.main(
                    [
                        "signal-gap-fill",
                        "--predict-date",
                        "2026-08-08",
                        "--project-root",
                        directory,
                    ]
                )

            report_root = Path(directory) / "reports" / "harness" / "signal-gap-fill"
            report_dirs = list(report_root.iterdir())
            report_files_exist = [
                (report_dirs[0] / name).is_file()
                for name in (
                    "frozen_plan.json",
                    "fill_result.json",
                    "post_fill_plan.json",
                )
            ]

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "PASSED")
        self.assertNotIn("raw-secret-token", output.getvalue())
        self.assertEqual(planner.call_count, 2)
        issue_token.assert_called_once_with(
            plan_sha256=PLAN_SHA,
            base_scheme_id=claim.base_scheme_id,
            predict_date=claim.predict_date,
            target_keys=claim.target_keys,
            scheme_version=claim.scheme_version,
            source_authority=claim.source_authority,
            ttl_seconds=900,
            issued_by="harness-cli:macstudio0",
        )
        self.assertEqual(len(report_dirs), 1)
        self.assertEqual(report_files_exist, [True, True, True])

    def test_postfill_remaining_gap_returns_failure_without_retry(self) -> None:
        claim = _claim()
        gate = Mock()
        gate.run.return_value = _passed_gate_result()
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with (
                patch.object(
                    cli,
                    "authorization_signing_enabled",
                    return_value=True,
                    create=True,
                ),
                patch.object(
                    cli,
                    "_plan_signal_gap_date",
                    side_effect=[READY_PLAN, READY_PLAN],
                    create=True,
                ),
                patch.object(
                    cli,
                    "signal_gap_fill_authorization_claims",
                    side_effect=[(claim,), (claim,)],
                    create=True,
                ),
                patch.object(
                    cli,
                    "issue_signal_gap_fill_token",
                    return_value="raw-secret-token",
                    create=True,
                ) as issue_token,
                patch.object(cli, "gate_for_name", return_value=gate),
                redirect_stdout(output),
            ):
                exit_code = cli.main(
                    [
                        "signal-gap-fill",
                        "--predict-date",
                        "2026-08-08",
                        "--project-root",
                        directory,
                    ]
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["failure_code"], "POSTFILL_GAPS_REMAIN")
        issue_token.assert_called_once()
        gate.run.assert_called_once()

    def test_gate_failure_is_reported_without_postfill_or_retry(self) -> None:
        claim = _claim()
        gate = Mock()
        gate.run.return_value = _failed_gate_result()
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with (
                patch.object(
                    cli,
                    "authorization_signing_enabled",
                    return_value=True,
                ),
                patch.object(
                    cli,
                    "_plan_signal_gap_date",
                    return_value=READY_PLAN,
                ) as planner,
                patch.object(
                    cli,
                    "signal_gap_fill_authorization_claims",
                    return_value=(claim,),
                ),
                patch.object(
                    cli,
                    "issue_signal_gap_fill_token",
                    return_value="raw-secret-token",
                ),
                patch.object(cli, "gate_for_name", return_value=gate),
                redirect_stdout(output),
            ):
                exit_code = cli.main(
                    [
                        "signal-gap-fill",
                        "--predict-date",
                        "2026-08-08",
                        "--project-root",
                        directory,
                    ]
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "FAILED")
        self.assertEqual(
            payload["failure_code"],
            "ALGORITHM_EXECUTION_FAILED",
        )
        self.assertNotIn("raw-secret-token", output.getvalue())
        planner.assert_called_once()
        gate.run.assert_called_once()


class SignalGapFillClaimsTests(unittest.TestCase):
    def test_claims_are_derived_from_frozen_groups(self) -> None:
        group = SimpleNamespace(
            base_scheme_id="demo_blackbox",
            scheme_version="version-1",
            predict_date="2026-08-08",
            expected_target_keys=({"target_tenor": "10Y"},),
            source_authority={"authority_type": "databridge_current_generation"},
        )
        from harness.gates import signal_gap_fill_gate

        with patch.object(
            signal_gap_fill_gate,
            "_build_groups",
            return_value=(group,),
        ):
            claims = signal_gap_fill_gate.signal_gap_fill_authorization_claims(
                READY_PLAN
            )

        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].base_scheme_id, "demo_blackbox")
        self.assertEqual(claims[0].scheme_version, "version-1")
        self.assertEqual(claims[0].predict_date, "2026-08-08")
        self.assertEqual(claims[0].target_keys, ({"target_tenor": "10Y"},))
        self.assertEqual(
            claims[0].source_authority,
            {"authority_type": "databridge_current_generation"},
        )


if __name__ == "__main__":
    unittest.main()
