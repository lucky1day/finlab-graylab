from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class BlackboxV2DiscoveryTests(unittest.TestCase):
    def test_loads_business_identity_from_delivery_metadata(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))

            config = load_scheme_config(scheme_dir / "config.yaml")

        self.assertEqual(config.runtime_type, "blackbox_v2")
        self.assertEqual(config.input_source, "data_bridge_current")
        self.assertEqual(config.version_status, "draft")
        self.assertEqual(config.name, "10Y Trial")
        self.assertEqual(config.algorithm_version, "1.2.3")
        self.assertEqual(config.contract_version, "1.0")
        self.assertEqual(config.target_rule, "target_date_yield_vs_feature_date_yield")
        self.assertEqual(config.horizon, 1)
        self.assertEqual(config.task_type, "T+1")
        self.assertEqual(config.tenors, ["10Y"])
        self.assertEqual(config.frequency, "daily")
        self.assertEqual(config.entry_point, "blackbox_v2")
        self.assertEqual(config.delivery_script, (scheme_dir / "delivery" / "trial_10y.py").resolve())
        self.assertEqual(config.delivery_metadata, (scheme_dir / "delivery" / "trial_10y.json").resolve())

    def test_rejects_metadata_task_combination_not_in_contract(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir), horizon=5)

            with self.assertRaisesRegex(ValueError, "task_type.*horizon.*target_rule"):
                load_scheme_config(scheme_dir / "config.yaml")

    def test_rejects_blackbox_without_explicit_data_bridge_input_source(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "input_source: data_bridge_current\n", ""
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "input_source"):
                load_scheme_config(config_path)

    def test_blackbox_version_ignores_lifecycle_fields(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            first = load_scheme_config(config_path)
            text = config_path.read_text(encoding="utf-8")
            text = text.replace("status: paused", "status: active")
            text = text.replace("version_status: draft", "version_status: active")
            config_path.write_text(text, encoding="utf-8")

            second = load_scheme_config(config_path)

        self.assertEqual(first.config_hash, second.config_hash)
        self.assertEqual(first.scheme_version, second.scheme_version)

    def test_blackbox_version_changes_when_schedule_timeout_changes(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            first = load_scheme_config(config_path)
            text = config_path.read_text(encoding="utf-8").replace(
                "  timezone: Asia/Shanghai\n",
                "  timezone: Asia/Shanghai\n  timeout_sec: 300\n",
            )
            config_path.write_text(text, encoding="utf-8")

            second = load_scheme_config(config_path)

        self.assertNotEqual(first.config_hash, second.config_hash)
        self.assertNotEqual(first.scheme_version, second.scheme_version)

    def test_blackbox_version_changes_when_script_bytes_change(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            first = load_scheme_config(config_path)
            script_path = scheme_dir / "delivery" / "trial_10y.py"
            script_path.write_bytes(script_path.read_bytes() + b"# changed\n")

            second = load_scheme_config(config_path)

        self.assertNotEqual(first.code_hash, second.code_hash)
        self.assertNotEqual(first.scheme_version, second.scheme_version)

    def test_blackbox_version_changes_when_metadata_bytes_change(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            first = load_scheme_config(scheme_dir / "config.yaml")
            metadata_path = scheme_dir / "delivery" / "trial_10y.json"
            metadata_path.write_bytes(metadata_path.read_bytes() + b"\n")

            second = load_scheme_config(scheme_dir / "config.yaml")

        self.assertNotEqual(first.manifest_hash, second.manifest_hash)
        self.assertNotEqual(first.scheme_version, second.scheme_version)

    def test_native_version_keeps_raw_config_hash_behavior(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_native_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            first = load_scheme_config(config_path)
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "status: active", "status: paused"
                ),
                encoding="utf-8",
            )

            second = load_scheme_config(config_path)

        self.assertEqual(first.code_hash, second.code_hash)
        self.assertNotEqual(first.config_hash, second.config_hash)
        self.assertNotEqual(first.scheme_version, second.scheme_version)


def _write_blackbox_scheme(root: Path, *, horizon: int = 1) -> Path:
    scheme_dir = root / "trial_10y"
    delivery_dir = scheme_dir / "delivery"
    delivery_dir.mkdir(parents=True)
    (scheme_dir / "config.yaml").write_text(
        "\n".join(
            [
                "scheme_id: trial_10y",
                "runtime_type: blackbox_v2",
                "input_source: data_bridge_current",
                "runtime_profile: blackbox-v2-v1",
                "data_schema_version: data-bridge-v1",
                "status: paused",
                "version_status: draft",
                "schedule:",
                "  cron: '3 7 * * 1-5'",
                "  timezone: Asia/Shanghai",
                "delivery:",
                "  script: delivery/trial_10y.py",
                "  metadata: delivery/trial_10y.json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (delivery_dir / "trial_10y.py").write_text("print('trial')\n", encoding="utf-8")
    (delivery_dir / "trial_10y.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scheme_id": "trial_10y",
                "name": "10Y Trial",
                "algorithm_version": "1.2.3",
                "target_tenor": "10Y",
                "task_type": "T+1",
                "horizon": horizon,
                "target_rule": "target_date_yield_vs_feature_date_yield",
            }
        ),
        encoding="utf-8",
    )
    return scheme_dir


def _write_native_scheme(root: Path) -> Path:
    scheme_dir = root / "native_daily"
    scheme_dir.mkdir(parents=True)
    (scheme_dir / "config.yaml").write_text(
        "\n".join(
            [
                "scheme_id: native_daily",
                "runtime_type: native_adapter",
                "name: Native Daily",
                "description: Native fixture",
                "horizon: 1",
                "task_type: T+1",
                'tenors: ["10Y"]',
                "frequency: daily",
                "schedule:",
                "  cron: '3 7 * * 1-5'",
                "  timezone: Asia/Shanghai",
                "entry_point: predict.run",
                "status: active",
                "input_spec:",
                "  data_version: shared_data_service_daily.v1",
                '  required_columns: ["date", "TB0YWI0C"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (scheme_dir / "predict.py").write_text(
        "def run(predict_date):\n    return []\n", encoding="utf-8"
    )
    return scheme_dir


if __name__ == "__main__":
    unittest.main()
