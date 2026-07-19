from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.context import GateContext


class BlackboxLiveGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_secret = os.environ.get("HARNESS_AUTH_SECRET")
        os.environ["HARNESS_AUTH_SECRET"] = "blackbox-live-test-secret"
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = self._scaffold_active_scheme()

    def tearDown(self) -> None:
        if self._previous_secret is None:
            os.environ.pop("HARNESS_AUTH_SECRET", None)
        else:
            os.environ["HARNESS_AUTH_SECRET"] = self._previous_secret
        self._tmp.cleanup()

    def _scaffold_active_scheme(self):
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
        config_path = scheme_dir / "config.yaml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            .replace("status: paused", "status: active")
            .replace("version_status: draft", "version_status: active"),
            encoding="utf-8",
        )
        return load_scheme_config(config_path)

    def _ctx(self, token: str | None) -> GateContext:
        return GateContext(
            scheme_id=self.cfg.scheme_id,
            predict_date="2026-07-20",
            project_root=self.root,
            report_dir=self.root / "reports" / "live",
            config=self.cfg,
            authorization=token,
            prediction_phase="gray_live",
            engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
        )

    def _token(
        self,
        *,
        scheme_version: str | None = None,
        harness_run_id: str = "hr_latest",
        ttl_seconds: int = 60,
    ) -> str:
        from harness.authorization import issue_token

        return issue_token(
            self.cfg.scheme_id,
            "live_write",
            "2026-07-20",
            scheme_version=scheme_version or self.cfg.scheme_version,
            harness_run_id=harness_run_id,
            ttl_seconds=ttl_seconds,
            issued_by="live-operator",
        )

    @staticmethod
    def _passed_run():
        return SimpleNamespace(harness_run_id="hr_latest")

    @staticmethod
    def _approval():
        return SimpleNamespace(executable=True, reason="approved")

    def test_blackbox_live_requires_hmac_signing_before_execution(self) -> None:
        from harness.authorization import issue_token
        from harness.gates.live_gate import LiveGate

        os.environ.pop("HARNESS_AUTH_SECRET", None)
        token = issue_token(
            self.cfg.scheme_id,
            "live_write",
            "2026-07-20",
            scheme_version=self.cfg.scheme_version,
            harness_run_id="hr_latest",
            ttl_seconds=60,
            issued_by="live-operator",
        )
        with (
            patch("harness.gates.live_gate.snapshot_table_counts", return_value={}),
            patch("harness.gates.live_gate.snapshot_scheme_counts", return_value={}),
            patch("harness.gates.live_gate.execute_scheme") as execute,
        ):
            result = LiveGate().run(self._ctx(token))

        self.assertFalse(result.passed)
        self.assertIn("HMAC signing", "\n".join(result.errors))
        execute.assert_not_called()

    def test_blackbox_live_rejects_overlong_authorization_before_execution(self) -> None:
        from harness.gates.live_gate import LiveGate

        with (
            patch("harness.gates.live_gate.snapshot_table_counts", return_value={}),
            patch("harness.gates.live_gate.snapshot_scheme_counts", return_value={}),
            patch("harness.gates.live_gate.execute_scheme") as execute,
        ):
            result = LiveGate().run(self._ctx(self._token(ttl_seconds=901)))

        self.assertFalse(result.passed)
        self.assertIn("900 seconds", "\n".join(result.errors))
        execute.assert_not_called()

    def test_blackbox_live_binds_exact_canonical_version_and_latest_all_run(self) -> None:
        from harness.gates.live_gate import LiveGate

        cases = {
            "version": self._token(scheme_version="wrong-version"),
            "run": self._token(harness_run_id="hr_old"),
        }
        for label, token in cases.items():
            with self.subTest(label=label):
                with (
                    patch(
                        "harness.gates.live_gate._verify_blackbox_passed_all",
                        return_value=self._passed_run(),
                        create=True,
                    ),
                    patch(
                        "harness.gates.live_gate.read_blackbox_execution_approval",
                        return_value=self._approval(),
                        create=True,
                    ),
                    patch("harness.gates.live_gate.snapshot_table_counts", return_value={}),
                    patch("harness.gates.live_gate.snapshot_scheme_counts", return_value={}),
                    patch("harness.gates.live_gate.execute_scheme") as execute,
                ):
                    result = LiveGate().run(self._ctx(token))

                self.assertFalse(result.passed)
                expected = "scheme_version" if label == "version" else "latest passed all-stage"
                self.assertIn(expected, "\n".join(result.errors))
                execute.assert_not_called()

    def test_blackbox_live_consumes_exact_token_once_before_execution(self) -> None:
        from harness.gates.live_gate import LiveGate

        token = self._token()
        run_result = SimpleNamespace(status="success", records_written=1, error_msg=None)
        table_snapshots = [
            {"t_scheme_runs": 0, "t_scheme_predictions": 0, "t_scheme_run_log": 0},
            {"t_scheme_runs": 1, "t_scheme_predictions": 1, "t_scheme_run_log": 1},
        ]
        scheme_snapshots = [
            {"t_scheme_runs": 0, "t_scheme_predictions": 0, "t_scheme_run_log": 0},
            {"t_scheme_runs": 1, "t_scheme_predictions": 1, "t_scheme_run_log": 1},
        ]
        with (
            patch(
                "harness.gates.live_gate._verify_blackbox_passed_all",
                return_value=self._passed_run(),
                create=True,
            ),
            patch(
                "harness.gates.live_gate.read_blackbox_execution_approval",
                return_value=self._approval(),
                create=True,
            ),
            patch("harness.gates.live_gate.snapshot_table_counts", side_effect=table_snapshots),
            patch("harness.gates.live_gate.snapshot_scheme_counts", side_effect=scheme_snapshots),
            patch("harness.gates.live_gate.execute_scheme", return_value=run_result) as execute,
        ):
            first = LiveGate().run(self._ctx(token))

        with patch("harness.gates.live_gate.execute_scheme") as replay_execute:
            second = LiveGate().run(self._ctx(token))

        self.assertTrue(first.passed, first.errors)
        self.assertFalse(second.passed)
        self.assertIn("already used", "\n".join(second.errors))
        execute.assert_called_once()
        replay_execute.assert_not_called()
        evidence = {item.key: item.value for item in first.evidence}
        self.assertEqual(evidence["scheme_version"], self.cfg.scheme_version)
        self.assertEqual(evidence["harness_run_id"], "hr_latest")


if __name__ == "__main__":
    unittest.main()
