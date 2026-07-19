from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.context import GateContext


class BlackboxApiGateTests(unittest.TestCase):
    _INSTANCE = {
        "fingerprint_version": "1",
        "fingerprint": "f" * 64,
        "database_identity_sha256": "d" * 64,
        "code_commit": "a" * 40,
        "runtime_profile": "blackbox-v2-v1",
        "instance_nonce_sha256": "n" * 64,
    }

    def _scaffold(self, root: Path):
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        delivery = root / "incoming"
        delivery.mkdir()
        (delivery / "weekly_trial.py").write_text("import argparse\n", encoding="utf-8")
        (delivery / "weekly_trial.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "scheme_id": "weekly_trial",
                    "name": "Weekly Trial",
                    "algorithm_version": "1.0.0",
                    "target_tenor": "10Y",
                    "task_type": "weekly_point",
                    "horizon": 1,
                    "target_rule": "target_week_end_yield_vs_feature_week_end_yield",
                }
            ),
            encoding="utf-8",
        )
        scheme_dir = intake_delivery(delivery, schemes_root=root / "schemes")
        config_path = scheme_dir / "config.yaml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            .replace("status: paused", "status: active")
            .replace("version_status: draft", "version_status: active"),
            encoding="utf-8",
        )
        return load_scheme_config(config_path)

    def test_registry_dispatches_active_blackbox_to_blackbox_api_gate(self) -> None:
        from harness.blackbox_v2.api_gate import BlackboxApiGate
        from harness.registry import gate_for_name

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = self._scaffold(root)
            ctx = GateContext(
                cfg.scheme_id,
                "api",
                root,
                root / "reports",
                config=cfg,
            )

            gate = gate_for_name("api", ctx=ctx)

        self.assertIsInstance(gate, BlackboxApiGate)

    def test_blackbox_api_gate_requires_registry_schemes_metrics_and_backtest_probes(self) -> None:
        from harness.blackbox_v2.api_gate import BlackboxApiGate

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = self._scaffold(root)
            registry_id = f"{cfg.scheme_id}__h1__10Y"
            ctx = GateContext(
                cfg.scheme_id,
                "api",
                root,
                root / "reports",
                config=cfg,
                engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
                api_base_url="http://127.0.0.1:18100",
                api_instance_nonce="cert-instance-1",
            )
            registry_row = {
                "scheme_id": registry_id,
                "base_scheme_id": cfg.scheme_id,
                "runtime_type": "blackbox_v2",
                "status": "active",
                "target_tenor": "10Y",
                "task_type": "weekly_point",
                "horizon": 1,
            }
            schemes_row = dict(registry_row)
            schemes_row.pop("runtime_type")
            schemes_payload = {"schemes": [schemes_row]}
            metrics_payload = {
                "scheme_id": registry_id,
                "base_scheme_id": cfg.scheme_id,
                "target_tenor": "10Y",
                "task_type": "weekly_point",
                "summary": {"total": 1, "metric_samples": 1, "accuracy": 100.0},
                "monthly_metrics": [
                    {"month": "2026-07", "samples": 1, "metric_samples": 1, "accuracy": 100.0}
                ],
                "daily_rows": [
                    {
                        "scheme_id": registry_id,
                        "base_scheme_id": cfg.scheme_id,
                        "target_tenor": "10Y",
                        "horizon": 1,
                        "prediction_phase": "gray_live",
                        "scheme_version": cfg.scheme_version,
                        "request_id": "req-live-1",
                        "data_snapshot_id": "snapshot-live-1",
                        "predicted_direction": 1,
                        "actual_direction": 1,
                        "is_correct": True,
                    }
                ],
            }
            backtest_payload = {
                "data_source": "blackbox_v2_current_snapshot_as_of",
                "schemes": [
                    {
                        "scheme_id": registry_id,
                        "base_scheme_id": cfg.scheme_id,
                        "target_tenor": "10Y",
                        "task_type": "weekly_point",
                        "horizon": 1,
                        "data_source": "blackbox_v2_current_snapshot_as_of",
                        "monthly_metrics": [
                            {"month": "2026-07", "samples": 1, "metric_samples": 1, "accuracy": 100.0}
                        ],
                        "daily_rows": [
                            {"predicted_direction": 1, "actual_direction": 1, "is_correct": True}
                        ],
                    }
                ],
            }

            def fetch(url: str, timeout_sec: int, max_response_bytes: int):
                if url.endswith("/api/health"):
                    return {"status": "ok", "service_instance": self._INSTANCE}, 200
                if url.endswith("/api/schemes"):
                    return schemes_payload, 200
                if url.endswith(f"/api/metrics/{registry_id}"):
                    return metrics_payload, 200
                if "data_source=blackbox_v2_current_snapshot_as_of" in url:
                    return backtest_payload, 200
                raise AssertionError(f"unexpected URL: {url}")

            with (
                patch.dict(
                    os.environ,
                    {"BOND_FACTOR_LAB_API_BASE_URL": "http://wrong-environment:9999"},
                ),
                patch(
                    "harness.blackbox_v2.api_gate._read_active_registry",
                    return_value=registry_row,
                ),
                patch(
                    "harness.blackbox_v2.api_gate.build_service_instance_identity",
                    return_value=self._INSTANCE,
                    create=True,
                ),
                patch("harness.blackbox_v2.api_gate.fetch_json", side_effect=fetch) as fetch_mock,
            ):
                result = BlackboxApiGate().run(ctx)

        self.assertTrue(result.passed, result.errors)
        self.assertEqual(fetch_mock.call_count, 4)
        self.assertTrue(
            all(
                call.args[0].startswith("http://127.0.0.1:18100/")
                for call in fetch_mock.call_args_list
            )
        )
        evidence = {item.key: item.value for item in result.evidence}
        self.assertEqual(evidence["target_tenor"], "10Y")
        self.assertEqual(evidence["task_type"], "weekly_point")
        self.assertEqual(evidence["horizon"], 1)
        self.assertTrue(evidence["registry_active"])
        self.assertTrue(evidence["schemes_visible"])
        self.assertTrue(evidence["metrics_visible"])
        self.assertTrue(evidence["backtest_visible"])
        self.assertEqual(evidence["effective_api_base_url"], "http://127.0.0.1:18100")
        self.assertEqual(evidence["expected_service_fingerprint"], "f" * 64)
        self.assertEqual(evidence["actual_service_fingerprint"], "f" * 64)
        self.assertEqual(evidence["live_rows"], 1)
        self.assertEqual(evidence["matched_actual_rows"], 1)

    def test_blackbox_api_gate_fails_closed_when_any_probe_is_missing(self) -> None:
        from harness.blackbox_v2.api_gate import BlackboxApiGate

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = self._scaffold(root)
            registry_id = f"{cfg.scheme_id}__h1__10Y"
            ctx = GateContext(
                cfg.scheme_id,
                "api",
                root,
                root / "reports",
                config=cfg,
                engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
                api_instance_nonce="cert-instance-1",
            )
            registry_row = {
                "scheme_id": registry_id,
                "base_scheme_id": cfg.scheme_id,
                "runtime_type": "blackbox_v2",
                "status": "active",
                "target_tenor": "10Y",
                "task_type": "weekly_point",
                "horizon": 1,
            }

            with (
                patch(
                    "harness.blackbox_v2.api_gate._read_active_registry",
                    return_value=registry_row,
                ),
                patch(
                    "harness.blackbox_v2.api_gate.fetch_json",
                    side_effect=[
                        ({"status": "ok", "service_instance": self._INSTANCE}, 200),
                        ({"schemes": [registry_row]}, 200),
                        ({"scheme_id": registry_id, "daily_rows": [], "monthly_metrics": []}, 200),
                        ({"schemes": []}, 200),
                    ],
                ),
                patch(
                    "harness.blackbox_v2.api_gate.build_service_instance_identity",
                    return_value=self._INSTANCE,
                    create=True,
                ),
            ):
                result = BlackboxApiGate().run(ctx)

        self.assertFalse(result.passed)
        errors = "\n".join(result.errors).lower()
        self.assertIn("backtest", errors)
        self.assertIn("daily_rows", errors)
        self.assertIn("monthly_metrics", errors)

    def test_blackbox_api_gate_rejects_wrong_service_instance_and_missing_live_contract(self) -> None:
        from harness.blackbox_v2.api_gate import BlackboxApiGate

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = self._scaffold(root)
            registry_id = f"{cfg.scheme_id}__h1__10Y"
            ctx = GateContext(
                cfg.scheme_id,
                "api",
                root,
                root / "reports",
                config=cfg,
                engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
                api_instance_nonce="cert-instance-1",
            )
            registry_row = {
                "scheme_id": registry_id,
                "base_scheme_id": cfg.scheme_id,
                "runtime_type": "blackbox_v2",
                "status": "active",
                "target_tenor": "10Y",
                "task_type": "weekly_point",
                "horizon": 1,
            }
            wrong_instance = {**self._INSTANCE, "code_commit": "b" * 40}
            metrics = {
                "scheme_id": registry_id,
                "base_scheme_id": cfg.scheme_id,
                "target_tenor": "10Y",
                "monthly_metrics": [{"month": "2026-07", "samples": 1, "metric_samples": 1}],
                "daily_rows": [
                    {
                        "target_tenor": "10Y",
                        "horizon": 1,
                        "prediction_phase": "gray_live",
                        "scheme_version": cfg.scheme_version,
                        "request_id": "",
                        "data_snapshot_id": "",
                        "predicted_direction": 1,
                        "actual_direction": None,
                    }
                ],
                "summary": {"total": 1, "metric_samples": 0},
            }
            backtest = {
                "schemes": [
                    {
                        **registry_row,
                        "data_source": "blackbox_v2_current_snapshot_as_of",
                        "monthly_metrics": [{"month": "2026-07", "samples": 1, "metric_samples": 1}],
                        "daily_rows": [{"actual_direction": 1}],
                    }
                ]
            }
            with (
                patch("harness.blackbox_v2.api_gate._read_active_registry", return_value=registry_row),
                patch(
                    "harness.blackbox_v2.api_gate.build_service_instance_identity",
                    return_value=self._INSTANCE,
                    create=True,
                ),
                patch(
                    "harness.blackbox_v2.api_gate.fetch_json",
                    side_effect=[
                        ({"status": "ok", "service_instance": wrong_instance}, 200),
                        ({"schemes": [registry_row]}, 200),
                        (metrics, 200),
                        (backtest, 200),
                    ],
                ),
            ):
                result = BlackboxApiGate().run(ctx)

        self.assertFalse(result.passed)
        errors = "\n".join(result.errors)
        self.assertIn("service instance identity", errors)
        self.assertIn("request_id", errors)
        self.assertIn("data_snapshot_id", errors)
        self.assertIn("actual", errors)

    def test_fetch_json_rejects_response_larger_than_limit(self) -> None:
        from harness.probes.api_probe import ApiProbeError, fetch_json

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, size: int):
                return b"x" * size

        with (
            patch("harness.probes.api_probe.urlopen", return_value=Response()),
            self.assertRaisesRegex(ApiProbeError, "exceeds"),
        ):
            fetch_json("http://127.0.0.1/api/health", max_response_bytes=32)

    def test_fetch_preserves_http_status_and_bounded_error_summary(self) -> None:
        from harness.blackbox_v2.api_gate import _fetch
        from harness.probes.api_probe import ApiProbeError

        with patch(
            "harness.blackbox_v2.api_gate.fetch_json",
            side_effect=ApiProbeError(
                "service unavailable",
                status_code=503,
                error_summary="x" * 900,
            ),
        ):
            payload, status, summary = _fetch(
                "http://127.0.0.1/api/health",
                timeout_sec=1,
            )

        self.assertIsNone(payload)
        self.assertEqual(status, 503)
        self.assertEqual(len(summary or ""), 512)


if __name__ == "__main__":
    unittest.main()
