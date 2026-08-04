from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from harness.authorization import issue_token, mark_token_used, parse_token, used_tokens_path
from harness.cli import main
from harness.config_loader import load_config_raw
from harness.context import GateContext
from scheduler.discovery import load_scheme_config
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _full_initial_validation():
    from harness.gates.activate_gate import NativeActivationValidation

    return NativeActivationValidation(
        validation_profile="full_initial_onboarding_v1",
        validation_harness_run_id="hr-full-initial",
        validation_stage="all",
        prior_admitted_scheme_version=None,
        registry_scheme_ids=(),
        benchmark_validation="passed_initial_admission",
    )


def _scaffold_scheme(root: Path, status: str = "paused") -> Path:
    scheme_dir = root / "schemes" / "t5_daily"
    scheme_dir.mkdir(parents=True)
    src = PROJECT_ROOT / "schemes" / "t5_daily" / "config.yaml"
    text = src.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip().startswith("status:"):
            lines[i] = f"status: {status}\n"
    (scheme_dir / "config.yaml").write_text("".join(lines), encoding="utf-8")
    return scheme_dir / "config.yaml"


class CliActivateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_secret = os.environ.get("HARNESS_AUTH_SECRET")
        os.environ["HARNESS_AUTH_SECRET"] = "activate-test-secret"
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        policy_path = self.root / "deploy" / "onboarding_policy_v1.json"
        policy_path.parent.mkdir(parents=True)
        policy_path.write_text(
            json.dumps(
                {
                    "policy_version": "1.0",
                    "new_scheme_runtime_type": "blackbox_v2",
                    "native_v1_mode": "maintenance_only",
                    "legacy_native_scheme_ids": ["t5_daily"],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        if self._prev_secret is None:
            os.environ.pop("HARNESS_AUTH_SECRET", None)
        else:
            os.environ["HARNESS_AUTH_SECRET"] = self._prev_secret
        self._tmp.cleanup()

    def _run_cli(self, argv: list[str]) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(argv)

    def test_activate_without_token_blocked(self) -> None:
        _scaffold_scheme(self.root, status="paused")
        code = self._run_cli(
            ["activate", "--scheme-id", "t5_daily", "--project-root", str(self.root)]
        )
        self.assertEqual(code, 2)

    def test_activate_with_valid_token_flips_status(self) -> None:
        config_path = _scaffold_scheme(self.root, status="paused")
        validation_version = load_scheme_config(config_path).scheme_version
        token = issue_token(
            "t5_daily",
            "activate",
            scheme_version=validation_version,
            issued_by="native-release-owner",
        )
        before_activation = datetime.now(timezone.utc)
        with patch(
            "harness.gates.activate_gate._resolve_native_activation_validation",
            return_value=(_full_initial_validation(), []),
        ):
            with patch(
                "harness.gates.activate_gate._sync_registry_after_activation",
                return_value="activated-version",
            ) as sync:
                code = self._run_cli(
                    [
                        "activate",
                        "--scheme-id",
                        "t5_daily",
                        "--project-root",
                        str(self.root),
                        "--authorize",
                        token,
                    ]
                )
        after_activation = datetime.now(timezone.utc)
        self.assertEqual(code, 0)
        sync.assert_called_once()
        self.assertEqual(sync.call_args.kwargs["approved_by"], "native-release-owner")
        self.assertGreaterEqual(sync.call_args.kwargs["approved_at"], before_activation)
        self.assertLessEqual(sync.call_args.kwargs["approved_at"], after_activation)
        raw = load_config_raw(config_path)
        self.assertEqual(raw["status"], "active")

    def test_activate_invalid_token_blocked(self) -> None:
        _scaffold_scheme(self.root, status="paused")
        code = self._run_cli(
            [
                "activate",
                "--scheme-id",
                "t5_daily",
                "--project-root",
                str(self.root),
                "--authorize",
                "not-a-valid-token",
            ]
        )
        self.assertEqual(code, 2)

    def test_stale_verify_replay_loser_is_blocked_before_native_mutation(self) -> None:
        config_path = _scaffold_scheme(self.root, status="paused")
        token = issue_token(
            "t5_daily",
            "activate",
            scheme_version=load_scheme_config(config_path).scheme_version,
            issued_by="native-release-owner",
        )
        auth = parse_token(token)
        mark_token_used(auth, used_tokens_path(self.root))

        with (
            patch("harness.gates.activate_gate.verify_authorization", return_value=(auth, [])),
            patch(
                "harness.gates.activate_gate._resolve_native_activation_validation",
                return_value=(_full_initial_validation(), []),
            ),
            patch(
                "harness.gates.activate_gate._sync_registry_after_activation",
                return_value="activated-version",
            ) as sync,
        ):
            code = self._run_cli(
                [
                    "activate",
                    "--scheme-id",
                    "t5_daily",
                    "--project-root",
                    str(self.root),
                    "--authorize",
                    token,
                ]
            )

        self.assertNotEqual(code, 0)
        sync.assert_not_called()
        self.assertEqual(load_config_raw(config_path)["status"], "paused")

    def test_activate_rejects_token_not_bound_to_validation_version_before_mutation(self) -> None:
        config_path = _scaffold_scheme(self.root, status="paused")
        token = issue_token(
            "t5_daily",
            "activate",
            scheme_version="different-validation-version",
            issued_by="native-release-owner",
        )

        with (
            patch(
                "harness.gates.activate_gate._resolve_native_activation_validation"
            ) as resolve_validation,
            patch("harness.gates.activate_gate._sync_registry_after_activation") as sync,
        ):
            code = self._run_cli(
                [
                    "activate",
                    "--scheme-id",
                    "t5_daily",
                    "--project-root",
                    str(self.root),
                    "--authorize",
                    token,
                ]
            )

        self.assertEqual(code, 2)
        resolve_validation.assert_not_called()
        sync.assert_not_called()
        self.assertEqual(load_config_raw(config_path)["status"], "paused")

    def test_activate_rejects_empty_token_issuer_before_mutation(self) -> None:
        config_path = _scaffold_scheme(self.root, status="paused")
        token = issue_token(
            "t5_daily",
            "activate",
            scheme_version=load_scheme_config(config_path).scheme_version,
            issued_by="   ",
        )

        with patch("harness.gates.activate_gate._sync_registry_after_activation") as sync:
            code = self._run_cli(
                [
                    "activate",
                    "--scheme-id",
                    "t5_daily",
                    "--project-root",
                    str(self.root),
                    "--authorize",
                    token,
                ]
            )

        self.assertEqual(code, 2)
        sync.assert_not_called()
        self.assertEqual(load_config_raw(config_path)["status"], "paused")

    def test_activate_reports_validation_and_distinct_post_flip_version(self) -> None:
        config_path = _scaffold_scheme(self.root, status="paused")
        validation_version = load_scheme_config(config_path).scheme_version
        token = issue_token(
            "t5_daily",
            "activate",
            scheme_version=validation_version,
            issued_by="native-release-owner",
        )
        observed: dict[str, str] = {}

        def sync_after_flip(_ctx, **_kwargs):
            activated_version = load_scheme_config(config_path).scheme_version
            observed["activated_version"] = activated_version
            return activated_version

        with (
            patch(
                "harness.gates.activate_gate._resolve_native_activation_validation",
                return_value=(_full_initial_validation(), []),
            ),
            patch(
                "harness.gates.activate_gate._sync_registry_after_activation",
                side_effect=sync_after_flip,
            ),
        ):
            code = self._run_cli(
                [
                    "activate",
                    "--scheme-id",
                    "t5_daily",
                    "--project-root",
                    str(self.root),
                    "--authorize",
                    token,
                ]
            )

        self.assertEqual(code, 0)
        self.assertNotEqual(observed["activated_version"], validation_version)
        self.assertEqual(load_config_raw(config_path)["status"], "active")

    def test_active_native_config_can_be_reapproved_without_version_flip(self) -> None:
        config_path = _scaffold_scheme(self.root, status="active")
        validation_cfg = load_scheme_config(config_path)
        validation_version = validation_cfg.scheme_version
        validation_bytes = config_path.read_bytes()
        token = issue_token(
            "t5_daily",
            "activate",
            scheme_version=validation_version,
            issued_by="native-release-owner",
        )

        with (
            patch(
                "harness.gates.activate_gate._resolve_native_activation_validation",
                return_value=(_full_initial_validation(), []),
            ),
            patch(
                "harness.gates.activate_gate._sync_registry_after_activation",
                return_value=validation_version,
            ) as activate,
        ):
            code = self._run_cli(
                [
                    "activate",
                    "--scheme-id",
                    "t5_daily",
                    "--project-root",
                    str(self.root),
                    "--authorize",
                    token,
                ]
            )

        self.assertEqual(code, 0)
        activate.assert_called_once()
        active_cfg = load_scheme_config(config_path)
        self.assertEqual(config_path.read_bytes(), validation_bytes)
        self.assertEqual(active_cfg.scheme_version, validation_version)
        self.assertEqual(active_cfg.config_hash, validation_cfg.config_hash)
        self.assertEqual(active_cfg.code_hash, validation_cfg.code_hash)
        self.assertEqual(active_cfg.manifest_hash, validation_cfg.manifest_hash)
        self.assertEqual(load_config_raw(config_path)["status"], "active")

    def test_auth_issue_activate_requires_nonempty_version_and_issuer(self) -> None:
        cases = (
            ["--issued-by", "operator"],
            ["--scheme-version", "", "--issued-by", "operator"],
            ["--scheme-version", "validation-version"],
            ["--scheme-version", "validation-version", "--issued-by", "   "],
        )
        for extra_args in cases:
            with self.subTest(extra_args=extra_args), self.assertRaises(SystemExit) as raised:
                self._run_cli(
                    [
                        "auth",
                        "issue",
                        "--scheme-id",
                        "t5_daily",
                        "--action",
                        "activate",
                        *extra_args,
                    ]
                )
            self.assertEqual(raised.exception.code, 2)

    def test_auth_issue_nonactivate_keeps_harness_default_issuer(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = main(
                [
                    "auth",
                    "issue",
                    "--scheme-id",
                    "t5_daily",
                    "--action",
                    "live",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(parse_token(stdout.getvalue().strip()).issued_by, "harness")

    def test_activation_db_failure_rolls_config_back_to_paused(self) -> None:
        config_path = _scaffold_scheme(self.root, status="paused")
        token = issue_token(
            "t5_daily",
            "activate",
            scheme_version=load_scheme_config(config_path).scheme_version,
            issued_by="native-release-owner",
        )

        with (
            patch(
                "harness.gates.activate_gate._resolve_native_activation_validation",
                return_value=(_full_initial_validation(), []),
            ),
            patch(
                "harness.gates.activate_gate._sync_registry_after_activation",
                side_effect=RuntimeError("injected DB failure"),
            ),
        ):
            code = self._run_cli(
                [
                    "activate",
                    "--scheme-id",
                    "t5_daily",
                    "--project-root",
                    str(self.root),
                    "--authorize",
                    token,
                ]
            )

        self.assertEqual(code, 1)
        self.assertEqual(load_config_raw(config_path)["status"], "paused")

    def test_native_registry_activation_uses_strict_discovery_and_only_target_config(self) -> None:
        from harness.gates.activate_gate import (
            _strict_native_activation_preflight,
            _sync_registry_after_activation,
        )

        config_path = _scaffold_scheme(self.root, status="active")
        target = load_scheme_config(config_path)
        other = SimpleNamespace(
            scheme_id="other_native",
            status="active",
            scheme_version="other-version",
        )
        engine = object()
        ctx = GateContext(
            scheme_id="t5_daily",
            predict_date="2026-07-20",
            project_root=self.root,
            report_dir=self.root / "reports",
            engine_factory=lambda: engine,
        )
        preflight = _strict_native_activation_preflight(
            ctx,
            target.scheme_version,
        )
        self.assertEqual(
            preflight.expected_active_config_bytes,
            preflight.config_bytes,
        )
        self.assertEqual(
            preflight.expected_active_config_hash,
            target.config_hash,
        )
        self.assertEqual(
            preflight.expected_active_scheme_version,
            target.scheme_version,
        )
        approved_at = datetime(2026, 7, 20, 8, 30)

        with (
            patch(
                "scheduler.discovery.discover_schemes",
                return_value=[other, target],
            ) as discover,
            patch(
                "scheduler.repository.apply_native_activation_state",
                return_value=target.scheme_version,
            ) as activate,
        ):
            result = _sync_registry_after_activation(
                ctx,
                preflight=preflight,
                approved_by="native-release-owner",
                approved_at=approved_at,
            )

        self.assertEqual(result, target.scheme_version)
        discover.assert_called_once_with(
            schemes_root=self.root / "schemes",
            strict=True,
        )
        activate.assert_called_once_with(
            engine,
            target,
            approved_by="native-release-owner",
            approved_at=approved_at,
        )

    def test_root_status_renderer_changes_only_the_unique_root_value(self) -> None:
        from harness.gates.activate_gate import _replace_root_status

        original = (
            b"scheme_id: demo\n"
            b"status: paused  # lifecycle\n"
            b"nested:\n"
            b"  status: shadow\n"
        )

        rendered = _replace_root_status(original, "active")

        self.assertEqual(
            rendered,
            (
                b"scheme_id: demo\n"
                b"status: active  # lifecycle\n"
                b"nested:\n"
                b"  status: shadow\n"
            ),
        )
        with self.assertRaisesRegex(ValueError, "exactly one root-level status"):
            _replace_root_status(
                b"status: paused\nstatus: active\n",
                "active",
            )

    def test_paused_activation_snapshot_changes_only_lifecycle_and_derived_version_fields(self) -> None:
        from harness.gates.activate_gate import (
            _strict_native_activation_preflight,
            _sync_registry_after_activation,
            _write_expected_active_config,
        )

        config_path = _scaffold_scheme(self.root, status="paused")
        validation_cfg = load_scheme_config(config_path)
        ctx = GateContext(
            scheme_id="t5_daily",
            predict_date="2026-07-20",
            project_root=self.root,
            report_dir=self.root / "reports",
            engine_factory=lambda: object(),
        )
        preflight = _strict_native_activation_preflight(
            ctx,
            validation_cfg.scheme_version,
        )

        flipped = _write_expected_active_config(config_path, preflight)
        captured: dict[str, object] = {}

        def activate(_engine, cfg, **_kwargs):
            captured["config"] = cfg
            return cfg.scheme_version

        with patch(
            "scheduler.repository.apply_native_activation_state",
            side_effect=activate,
        ):
            activated_version = _sync_registry_after_activation(
                ctx,
                preflight=preflight,
                approved_by="native-release-owner",
                approved_at=datetime(2026, 7, 20, 8, 30),
            )

        active_cfg = captured["config"]
        self.assertTrue(flipped)
        self.assertEqual(preflight.config_text.encode("utf-8"), preflight.config_bytes)
        self.assertEqual(
            preflight.expected_active_config_text.encode("utf-8"),
            preflight.expected_active_config_bytes,
        )
        self.assertEqual(config_path.read_bytes(), preflight.expected_active_config_bytes)
        self.assertEqual(active_cfg.status, "active")
        self.assertEqual(active_cfg.version_status, "active")
        self.assertEqual(active_cfg.code_hash, validation_cfg.code_hash)
        self.assertEqual(active_cfg.manifest_hash, validation_cfg.manifest_hash)
        self.assertEqual(active_cfg.config_hash, preflight.expected_active_config_hash)
        self.assertEqual(active_cfg.scheme_version, preflight.expected_active_scheme_version)
        self.assertEqual(activated_version, preflight.expected_active_scheme_version)
        self.assertEqual(
            preflight.business_identity,
            preflight.business_identity_for(active_cfg),
        )

    def _assert_gate_history_mutation_fails_before_engine(self, mutator) -> None:
        config_path = _scaffold_scheme(self.root, status="paused")
        token = issue_token(
            "t5_daily",
            "activate",
            scheme_version=load_scheme_config(config_path).scheme_version,
            issued_by="native-release-owner",
        )

        def mutate_after_history(*_args, **_kwargs):
            mutator(config_path)
            return _full_initial_validation(), []

        with (
            patch(
                "harness.gates.activate_gate._resolve_native_activation_validation",
                side_effect=mutate_after_history,
            ),
            patch("harness.gates.activate_gate._db_engine") as create_engine,
            patch(
                "scheduler.repository.apply_native_activation_state",
                return_value="unexpected-activation",
            ) as activate,
        ):
            code = self._run_cli(
                [
                    "activate",
                    "--scheme-id",
                    "t5_daily",
                    "--project-root",
                    str(self.root),
                    "--authorize",
                    token,
                ]
            )

        self.assertEqual(code, 1)
        create_engine.assert_not_called()
        activate.assert_not_called()
        self.assertEqual(load_config_raw(config_path)["status"], "paused")

    def test_predict_code_drift_after_gate_history_blocks_activation_before_engine(self) -> None:
        self._assert_gate_history_mutation_fails_before_engine(
            lambda config_path: (config_path.parent / "predict.py").write_text(
                "SCHEME_ID = 't5_daily'\n",
                encoding="utf-8",
            )
        )

    def test_manifest_drift_after_gate_history_blocks_activation_before_engine(self) -> None:
        self._assert_gate_history_mutation_fails_before_engine(
            lambda config_path: (config_path.parent / "manifest.json").write_text(
                '{"changed": true}\n',
                encoding="utf-8",
            )
        )

    def test_nonstatus_config_drift_after_gate_history_blocks_activation_before_engine(self) -> None:
        def mutate_config(config_path: Path) -> None:
            text = config_path.read_text(encoding="utf-8")
            config_path.write_text(
                text.replace("description:", "description: changed #", 1),
                encoding="utf-8",
            )

        self._assert_gate_history_mutation_fails_before_engine(mutate_config)

    def test_preflight_strict_discovery_failure_has_no_token_config_or_db_side_effect(self) -> None:
        config_path = _scaffold_scheme(self.root, status="paused")
        validation_version = load_scheme_config(config_path).scheme_version
        token = issue_token(
            "t5_daily",
            "activate",
            scheme_version=validation_version,
            issued_by="native-release-owner",
        )

        with (
            patch(
                "scheduler.discovery.discover_schemes",
                side_effect=ValueError("strict discovery failed"),
            ),
            patch(
                "harness.gates.activate_gate._resolve_native_activation_validation"
            ) as resolve_validation,
            patch("harness.gates.activate_gate.mark_token_used") as mark_used,
            patch("harness.gates.activate_gate._sync_registry_after_activation") as activate,
        ):
            code = self._run_cli(
                [
                    "activate",
                    "--scheme-id",
                    "t5_daily",
                    "--project-root",
                    str(self.root),
                    "--authorize",
                    token,
                ]
            )

        self.assertEqual(code, 1)
        resolve_validation.assert_not_called()
        mark_used.assert_not_called()
        activate.assert_not_called()
        self.assertEqual(load_config_raw(config_path)["status"], "paused")
        self.assertFalse((self.root / "reports" / "activation_authorization").exists())

    def test_preflight_rejects_discovered_validation_version_drift_before_db(self) -> None:
        config_path = _scaffold_scheme(self.root, status="paused")
        validation_cfg = load_scheme_config(config_path)
        token = issue_token(
            "t5_daily",
            "activate",
            scheme_version=validation_cfg.scheme_version,
            issued_by="native-release-owner",
        )
        drifted = SimpleNamespace(
            scheme_id="t5_daily",
            status="paused",
            scheme_version="drifted-version",
        )

        with (
            patch("scheduler.discovery.discover_schemes", return_value=[drifted]),
            patch(
                "harness.gates.activate_gate._resolve_native_activation_validation"
            ) as resolve_validation,
            patch("harness.gates.activate_gate.mark_token_used") as mark_used,
            patch("harness.gates.activate_gate._sync_registry_after_activation") as activate,
        ):
            code = self._run_cli(
                [
                    "activate",
                    "--scheme-id",
                    "t5_daily",
                    "--project-root",
                    str(self.root),
                    "--authorize",
                    token,
                ]
            )

        self.assertEqual(code, 1)
        resolve_validation.assert_not_called()
        mark_used.assert_not_called()
        activate.assert_not_called()
        self.assertEqual(load_config_raw(config_path)["status"], "paused")
        self.assertFalse((self.root / "reports" / "activation_authorization").exists())


if __name__ == "__main__":
    unittest.main()
