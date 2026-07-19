from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.context import GateContext


class BlackboxActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_secret = os.environ.get("HARNESS_AUTH_SECRET")
        os.environ["HARNESS_AUTH_SECRET"] = "blackbox-activation-test-secret"
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = self._scaffold()

    def tearDown(self) -> None:
        if self._previous_secret is None:
            os.environ.pop("HARNESS_AUTH_SECRET", None)
        else:
            os.environ["HARNESS_AUTH_SECRET"] = self._previous_secret
        self._tmp.cleanup()

    def _scaffold(self):
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        delivery = self.root / "incoming"
        delivery.mkdir()
        (delivery / "trial_10y.py").write_text("import argparse\n", encoding="utf-8")
        (delivery / "trial_10y.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "scheme_id": "trial_10y",
                    "name": "Trial",
                    "algorithm_version": "1.0.0",
                    "target_tenor": "10Y",
                    "task_type": "T+1",
                    "horizon": 1,
                    "target_rule": "target_date_yield_vs_feature_date_yield",
                }
            ),
            encoding="utf-8",
        )
        scheme_dir = intake_delivery(delivery, schemes_root=self.root / "schemes")
        path = scheme_dir / "config.yaml"
        text = path.read_text(encoding="utf-8").replace("version_status: draft", "version_status: shadow")
        path.write_text(text, encoding="utf-8")
        return load_scheme_config(path)

    def _ctx(self, token: str | None) -> GateContext:
        return GateContext(
            scheme_id=self.cfg.scheme_id,
            predict_date="2026-07-20",
            project_root=self.root,
            report_dir=self.root / "reports" / "activation",
            config=self.cfg,
            authorization=token,
            engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
        )

    def _passed_run(self):
        return SimpleNamespace(
            harness_run_id="hr_passed",
            report_uri=self.root / "reports" / "all",
            data_snapshot_id="snapshot-1",
            generation_id="full-20260720-053000-deadbeef",
            runtime_profile="blackbox-v2-v1",
            environment_fingerprint="e" * 64,
        )

    def _db_state(self, version="shadow", registry="paused"):
        return SimpleNamespace(
            scheme_id=self.cfg.scheme_id,
            scheme_version=self.cfg.scheme_version,
            runtime_type="blackbox_v2",
            version_status=version,
            registry_status=registry,
            environment_fingerprint="e" * 64,
            data_snapshot_id="snapshot-1",
            code_hash=self.cfg.code_hash,
            config_hash=self.cfg.config_hash,
            manifest_hash=self.cfg.manifest_hash,
            approved_by=None,
            approved_at=None,
        )

    def _signed_activation_token_with_expiry(self, expires_at) -> str:
        from harness.authorization import issue_token

        token = issue_token(
            self.cfg.scheme_id,
            "blackbox_activate",
            scheme_version=self.cfg.scheme_version,
            harness_run_id="hr_passed",
            ttl_seconds=60,
            issued_by="release-owner",
        )
        padding = "=" * (-len(token) % 4)
        envelope = json.loads(base64.urlsafe_b64decode((token + padding).encode()).decode())
        envelope["payload"]["expires_at"] = expires_at
        canonical = json.dumps(
            envelope["payload"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hmac.new(
            os.environ["HARNESS_AUTH_SECRET"].encode("utf-8"),
            canonical,
            hashlib.sha256,
        ).digest()
        envelope["sig"] = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def test_activation_requires_signing(self) -> None:
        from harness.authorization import issue_token
        from harness.blackbox_v2.activation import activate_blackbox

        os.environ.pop("HARNESS_AUTH_SECRET", None)
        token = issue_token(
            self.cfg.scheme_id,
            "blackbox_activate",
            scheme_version=self.cfg.scheme_version,
            harness_run_id="hr_passed",
            ttl_seconds=60,
            issued_by="release-owner",
        )
        result = activate_blackbox(self._ctx(token))
        self.assertFalse(result.passed)
        self.assertIn("HMAC signing", "\n".join(result.errors))

    def test_formal_activation_binds_run_and_promotes_all_three_stores(self) -> None:
        from harness.authorization import issue_token
        from harness.blackbox_v2.activation import activate_blackbox

        token = issue_token(
            self.cfg.scheme_id,
            "blackbox_activate",
            scheme_version=self.cfg.scheme_version,
            harness_run_id="hr_passed",
            ttl_seconds=60,
            issued_by="release-owner",
        )
        state = {"db": self._db_state()}

        def apply(_engine, cfg, **kwargs):
            state["db"] = self._db_state(kwargs["version_status"], kwargs["registry_status"])
            state["db"].approved_by = kwargs.get("approved_by")
            state["db"].approved_at = kwargs.get("approved_at")
            return state["db"]

        with (
            patch("harness.blackbox_v2.activation._verify_passed_all", return_value=self._passed_run()),
            patch("harness.blackbox_v2.activation._environment_fingerprint", return_value="e" * 64),
            patch("harness.blackbox_v2.activation.read_blackbox_lifecycle_state", side_effect=lambda *_: state["db"]),
            patch("harness.blackbox_v2.activation.apply_blackbox_lifecycle_state", side_effect=apply) as apply_state,
        ):
            result = activate_blackbox(self._ctx(token))

        self.assertTrue(result.passed, result.errors)
        apply_state.assert_called()
        kwargs = apply_state.call_args.kwargs
        self.assertEqual(kwargs["version_status"], "active")
        self.assertEqual(kwargs["registry_status"], "active")
        self.assertEqual(kwargs["approved_by"], "release-owner")
        self.assertIsInstance(kwargs["approved_at"], datetime)
        updated = __import__("scheduler.discovery", fromlist=["load_scheme_config"]).load_scheme_config(
            self.cfg.path / "config.yaml"
        )
        self.assertEqual((updated.status, updated.version_status), ("active", "active"))

    def test_activation_rejects_mismatched_and_expired_tokens(self) -> None:
        from harness.authorization import issue_token
        from harness.blackbox_v2.activation import activate_blackbox

        cases = [
            issue_token(
                self.cfg.scheme_id,
                "blackbox_activate",
                scheme_version="wrong-version",
                harness_run_id="hr_passed",
                ttl_seconds=60,
                issued_by="release-owner",
            ),
            issue_token(
                self.cfg.scheme_id,
                "blackbox_activate",
                scheme_version=self.cfg.scheme_version,
                harness_run_id="hr_passed",
                ttl_seconds=-1,
                issued_by="release-owner",
            ),
        ]
        with patch("harness.blackbox_v2.activation._verify_passed_all", return_value=self._passed_run()):
            results = [activate_blackbox(self._ctx(token)) for token in cases]
        self.assertTrue(all(not result.passed for result in results))

    def test_activation_requires_nonempty_well_formed_future_expiry(self) -> None:
        from harness.blackbox_v2.activation import activate_blackbox

        cases = {
            "missing": None,
            "empty": "",
            "whitespace": "   ",
            "malformed": "not-a-date",
            "not-future": "2000-01-01T00:00:00+00:00",
        }
        for label, expires_at in cases.items():
            with self.subTest(label=label):
                token = self._signed_activation_token_with_expiry(expires_at)
                result = activate_blackbox(self._ctx(token))
                self.assertFalse(result.passed)
                self.assertIn("expires_at", "\n".join(result.errors))

    def test_activation_rejects_token_signed_with_another_secret(self) -> None:
        from harness.authorization import issue_token
        from harness.blackbox_v2.activation import activate_blackbox

        token = issue_token(
            self.cfg.scheme_id,
            "blackbox_activate",
            scheme_version=self.cfg.scheme_version,
            harness_run_id="hr_passed",
            ttl_seconds=60,
            issued_by="release-owner",
        )
        os.environ["HARNESS_AUTH_SECRET"] = "different-secret"

        result = activate_blackbox(self._ctx(token))

        self.assertFalse(result.passed)
        self.assertIn("signature is invalid", "\n".join(result.errors))

    def test_common_activation_gate_dispatches_blackbox_without_native_mutation(self) -> None:
        from harness.gates.activate_gate import ActivationGate
        from harness.result import GateResult, GateStatus

        expected = GateResult(
            gate_name="activate",
            status=GateStatus.BLOCKED,
            passed=False,
            evidence=[],
            errors=["delegated"],
            started_at="start",
            finished_at="finish",
        )
        with patch("harness.blackbox_v2.activation.activate_blackbox", return_value=expected) as delegated:
            result = ActivationGate().run(self._ctx("token"))
        self.assertIs(result, expected)
        delegated.assert_called_once()

    def test_activation_gate_fails_closed_for_malformed_explicit_blackbox_config(self) -> None:
        from harness.gates.activate_gate import ActivationGate

        config_path = self.cfg.path / "config.yaml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "runtime_profile: blackbox-v2-v1",
                'runtime_profile: ""',
            ),
            encoding="utf-8",
        )
        ctx = replace_context(self._ctx("token"), config=None)

        with patch.object(ActivationGate, "_run", side_effect=AssertionError("native fallback")):
            result = ActivationGate().run(ctx)

        self.assertFalse(result.passed)
        self.assertIn("Blackbox", "\n".join(result.errors))

    def test_live_gate_rejects_actual_paused_blackbox_config_without_execution(self) -> None:
        from harness.authorization import issue_token
        from harness.gates.live_gate import LiveGate

        token = issue_token(
            self.cfg.scheme_id,
            "live_write",
            "2026-07-20",
            ttl_seconds=60,
            issued_by="operator",
        )
        ctx = replace_context(self._ctx(token), prediction_phase="gray_live")
        with (
            patch("harness.gates.live_gate.snapshot_table_counts", return_value={}),
            patch("harness.gates.live_gate.snapshot_scheme_counts", return_value={}),
            patch("harness.gates.live_gate.execute_scheme") as execute,
        ):
            result = LiveGate().run(ctx)

        self.assertFalse(result.passed)
        self.assertIn("active+active", "\n".join(result.errors))
        execute.assert_not_called()

    def test_activation_verification_rejects_config_version_status_mismatch(self) -> None:
        from dataclasses import replace

        from harness.authorization import issue_token
        from harness.blackbox_v2.activation import activate_blackbox
        from scheduler.discovery import load_scheme_config

        token = issue_token(
            self.cfg.scheme_id,
            "blackbox_activate",
            scheme_version=self.cfg.scheme_version,
            harness_run_id="hr_passed",
            ttl_seconds=60,
            issued_by="release-owner",
        )
        state = {"db": self._db_state()}
        real_load = load_scheme_config

        def apply(_engine, _cfg, **kwargs):
            state["db"] = self._db_state(kwargs["version_status"], kwargs["registry_status"])
            return state["db"]

        def load_with_mismatch(cfg):
            current = real_load(cfg.path / "config.yaml")
            if current.status == "active":
                return replace(current, version_status="shadow")
            return current

        with (
            patch("harness.blackbox_v2.activation._verify_passed_all", return_value=self._passed_run()),
            patch("harness.blackbox_v2.activation._environment_fingerprint", return_value="e" * 64),
            patch("harness.blackbox_v2.activation.read_blackbox_lifecycle_state", side_effect=lambda *_: state["db"]),
            patch("harness.blackbox_v2.activation.apply_blackbox_lifecycle_state", side_effect=apply),
            patch("harness.blackbox_v2.activation._reload_with_evidence", side_effect=load_with_mismatch),
        ):
            result = activate_blackbox(self._ctx(token))

        self.assertFalse(result.passed)
        restored = real_load(self.cfg.path / "config.yaml")
        self.assertEqual((restored.status, restored.version_status), ("paused", "shadow"))
        self.assertEqual((state["db"].version_status, state["db"].registry_status), ("shadow", "paused"))

    def test_activation_rejects_delivery_version_drift_before_database_write(self) -> None:
        from harness.authorization import issue_token
        from harness.blackbox_v2.activation import activate_blackbox
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.lifecycle import pending_journals

        token = issue_token(
            self.cfg.scheme_id,
            "blackbox_activate",
            scheme_version=self.cfg.scheme_version,
            harness_run_id="hr_passed",
            ttl_seconds=60,
            issued_by="release-owner",
        )
        state = {"db": self._db_state()}

        def mutate_delivery(*_args, **_kwargs):
            script = self.cfg.path / "delivery" / f"{self.cfg.scheme_id}.py"
            script.chmod(0o644)
            script.write_text("import argparse\n# changed after authorization\n", encoding="utf-8")

        with (
            patch("harness.blackbox_v2.activation._verify_passed_all", return_value=self._passed_run()),
            patch("harness.blackbox_v2.activation._environment_fingerprint", return_value="e" * 64),
            patch("harness.blackbox_v2.activation.read_blackbox_lifecycle_state", side_effect=lambda *_: state["db"]),
            patch("harness.blackbox_v2.activation.mark_token_used", side_effect=mutate_delivery),
            patch("harness.blackbox_v2.activation.apply_blackbox_lifecycle_state") as apply_state,
        ):
            result = activate_blackbox(self._ctx(token))

        self.assertFalse(result.passed)
        self.assertIn("canonical version changed", "\n".join(result.errors))
        apply_state.assert_not_called()
        current = load_scheme_config(self.cfg.path / "config.yaml")
        self.assertNotEqual(current.scheme_version, self.cfg.scheme_version)
        self.assertEqual((current.status, current.version_status), ("paused", "shadow"))
        self.assertEqual(pending_journals(self.root, self.cfg.scheme_id)[0][1].phase, "unresolved")

    def test_unresolved_journal_blocks_common_live_gate_before_engine_or_token_use(self) -> None:
        from harness.authorization import issue_token
        from harness.gates.live_gate import LiveGate
        from shared.blackbox_v2.lifecycle import LifecycleJournal, LifecycleState, write_journal

        journal = LifecycleJournal.prepare(
            action="activate",
            scheme_id=self.cfg.scheme_id,
            scheme_version=self.cfg.scheme_version,
            harness_run_id="hr_1",
            previous=LifecycleState("paused", "shadow", "paused"),
            target=LifecycleState("active", "active", "active"),
            token_hash="hash",
        )
        write_journal(self.root, journal)
        token = issue_token(
            self.cfg.scheme_id,
            "live_write",
            "2026-07-20",
            ttl_seconds=60,
            issued_by="operator",
        )
        ctx = replace_context(
            self._ctx(token),
            prediction_phase="gray_live",
            engine_factory=lambda: self.fail("engine must not be created while lifecycle is unresolved"),
        )

        result = LiveGate().run(ctx)

        self.assertFalse(result.passed)
        self.assertIn("lifecycle journal", "\n".join(result.errors))

    def test_signed_manual_reconcile_restores_safe_state_and_never_promotes(self) -> None:
        from harness.authorization import issue_token
        from harness.blackbox_v2.activation import BlackboxLifecycleReconcileGate
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.lifecycle import (
            LifecycleJournal,
            LifecycleState,
            load_journal,
            write_journal,
        )

        config_path = self.cfg.path / "config.yaml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            .replace("status: paused", "status: active")
            .replace("version_status: shadow", "version_status: active"),
            encoding="utf-8",
        )
        journal = LifecycleJournal.prepare(
            action="activate",
            scheme_id=self.cfg.scheme_id,
            scheme_version=self.cfg.scheme_version,
            harness_run_id="hr_passed",
            previous=LifecycleState("paused", "shadow", "paused"),
            target=LifecycleState("active", "active", "active"),
            token_hash="old-token-hash",
        ).transition("config_written").transition("db_committed")
        journal_path = write_journal(self.root, journal)
        token = issue_token(
            self.cfg.scheme_id,
            "blackbox_reconcile",
            scheme_version=self.cfg.scheme_version,
            ttl_seconds=60,
            issued_by="recovery-owner",
        )
        state = {"db": self._db_state("active", "active")}

        def apply(_engine, _cfg, **kwargs):
            state["db"] = self._db_state(kwargs["version_status"], kwargs["registry_status"])
            return state["db"]

        ctx = replace_context(self._ctx(token), config=load_scheme_config(config_path))
        with (
            patch("harness.blackbox_v2.activation.read_blackbox_lifecycle_state", side_effect=lambda *_: state["db"]),
            patch("harness.blackbox_v2.activation.apply_blackbox_lifecycle_state", side_effect=apply),
        ):
            result = BlackboxLifecycleReconcileGate().run(ctx)

        self.assertTrue(result.passed, result.errors)
        restored = load_scheme_config(config_path)
        self.assertEqual((restored.status, restored.version_status), ("paused", "shadow"))
        self.assertEqual((state["db"].version_status, state["db"].registry_status), ("shadow", "paused"))
        self.assertEqual(load_journal(journal_path).phase, "compensated")
        evidence = {item.key: item.value for item in result.evidence}
        self.assertFalse(evidence["promoted"])
        self.assertEqual(
            evidence["actual_state_before"],
            {
                "config_status": "active",
                "config_version_status": "active",
                "version_status": "active",
                "registry_status": "active",
            },
        )
        self.assertEqual(
            evidence["actual_state_after"],
            {
                "config_status": "paused",
                "config_version_status": "shadow",
                "version_status": "shadow",
                "registry_status": "paused",
            },
        )

    def test_manual_reconcile_rejects_journal_version_drift_without_mutation(self) -> None:
        from harness.authorization import issue_token, used_tokens_path
        from harness.blackbox_v2.activation import BlackboxLifecycleReconcileGate
        from shared.blackbox_v2.lifecycle import (
            LifecycleJournal,
            LifecycleState,
            load_journal,
            pending_journals,
            write_journal,
        )

        journal = LifecycleJournal.prepare(
            action="activate",
            scheme_id=self.cfg.scheme_id,
            scheme_version=self.cfg.scheme_version,
            harness_run_id="hr_passed",
            previous=LifecycleState("paused", "shadow", "paused"),
            target=LifecycleState("active", "active", "active"),
            token_hash="old-token-hash",
        ).transition("config_written").transition("db_committed").transition(
            "unresolved",
            error="forced unresolved state",
        )
        journal_path = write_journal(self.root, journal)
        token = issue_token(
            self.cfg.scheme_id,
            "blackbox_reconcile",
            scheme_version=self.cfg.scheme_version,
            ttl_seconds=60,
            issued_by="recovery-owner",
        )
        script = self.cfg.path / "delivery" / f"{self.cfg.scheme_id}.py"
        script.chmod(0o644)
        script.write_text("import argparse\n# canonical version drift\n", encoding="utf-8")

        with (
            patch("harness.blackbox_v2.activation.read_blackbox_lifecycle_state") as read_state,
            patch("harness.blackbox_v2.activation.apply_blackbox_lifecycle_state") as apply_state,
        ):
            result = BlackboxLifecycleReconcileGate().run(self._ctx(token))

        self.assertFalse(result.passed)
        self.assertIn("scheme_version", "\n".join(result.errors))
        read_state.assert_not_called()
        apply_state.assert_not_called()
        preserved = load_journal(journal_path)
        self.assertEqual(preserved.phase, "unresolved")
        self.assertIsNone(preserved.reconciled_by)
        self.assertEqual(pending_journals(self.root, self.cfg.scheme_id)[0][0], journal_path)
        self.assertFalse(used_tokens_path(self.root).exists())


def replace_context(ctx: GateContext, **updates) -> GateContext:
    values = dict(ctx.__dict__)
    values.update(updates)
    return GateContext(**values)


if __name__ == "__main__":
    unittest.main()
