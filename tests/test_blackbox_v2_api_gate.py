from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.context import GateContext


class BlackboxApiGateTests(unittest.TestCase):
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
                "monthly_metrics": [],
                "daily_rows": [{"predicted_direction": 1}],
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
                        "monthly_metrics": [],
                        "daily_rows": [{"predicted_direction": 1}],
                    }
                ],
            }

            def fetch(url: str, timeout_sec: int):
                if url.endswith("/api/schemes"):
                    return schemes_payload, 200
                if url.endswith(f"/api/metrics/{registry_id}"):
                    return metrics_payload, 200
                if "data_source=blackbox_v2_current_snapshot_as_of" in url:
                    return backtest_payload, 200
                raise AssertionError(f"unexpected URL: {url}")

            with (
                patch(
                    "harness.blackbox_v2.api_gate._read_active_registry",
                    return_value=registry_row,
                ),
                patch("harness.blackbox_v2.api_gate.fetch_json", side_effect=fetch) as fetch_mock,
            ):
                result = BlackboxApiGate().run(ctx)

        self.assertTrue(result.passed, result.errors)
        self.assertEqual(fetch_mock.call_count, 3)
        evidence = {item.key: item.value for item in result.evidence}
        self.assertEqual(evidence["target_tenor"], "10Y")
        self.assertEqual(evidence["task_type"], "weekly_point")
        self.assertEqual(evidence["horizon"], 1)
        self.assertTrue(evidence["registry_active"])
        self.assertTrue(evidence["schemes_visible"])
        self.assertTrue(evidence["metrics_visible"])
        self.assertTrue(evidence["backtest_visible"])

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
                        ({"schemes": [registry_row]}, 200),
                        ({"scheme_id": registry_id, "daily_rows": []}, 200),
                        ({"schemes": []}, 200),
                    ],
                ),
            ):
                result = BlackboxApiGate().run(ctx)

        self.assertFalse(result.passed)
        self.assertIn("backtest", "\n".join(result.errors).lower())


if __name__ == "__main__":
    unittest.main()
