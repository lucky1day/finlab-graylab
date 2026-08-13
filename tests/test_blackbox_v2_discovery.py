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
        self.assertEqual(config.description, "")
        self.assertIsNone(config.owner)
        self.assertEqual(config.algorithm_version, "1.2.3")
        self.assertEqual(config.contract_version, "1.0")
        self.assertEqual(config.target_rule, "target_date_yield_vs_feature_date_yield")
        self.assertEqual(config.horizon, 1)
        self.assertEqual(config.task_type, "T+1")
        self.assertEqual(config.tenors, ["10Y"])
        self.assertEqual(config.frequency, "daily")
        self.assertEqual(config.entry_point, "blackbox_v2")
        self.assertEqual(config.schedule.timeout_sec, 3600)
        self.assertEqual(config.platform_inputs, ())
        self.assertEqual(config.delivery_script, (scheme_dir / "delivery" / "trial_10y.py").resolve())
        self.assertEqual(config.delivery_metadata, (scheme_dir / "delivery" / "trial_10y.json").resolve())

    def test_blackbox_platform_inputs_are_normalized_and_version_bound(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            original = load_scheme_config(config_path)
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "status: paused\n",
                    "platform_inputs:\n"
                    "  - api-wind-date-v1\n"
                    "status: paused\n",
                ),
                encoding="utf-8",
            )

            declared = load_scheme_config(config_path)

        self.assertEqual(
            declared.platform_inputs,
            ("api-wind-date-v1",),
        )
        self.assertNotEqual(original.config_hash, declared.config_hash)
        self.assertNotEqual(original.scheme_version, declared.scheme_version)

    def test_blackbox_rejects_invalid_explicit_platform_inputs(self) -> None:
        from scheduler.discovery import load_scheme_config

        replacements = (
            "platform_inputs: []\n",
            "platform_inputs:\n  - unknown-input-v1\n",
            (
                "platform_inputs:\n"
                "  - api-wind-date-v1\n"
                "  - api-wind-date-v1\n"
            ),
        )
        for replacement in replacements:
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as tmpdir:
                scheme_dir = _write_blackbox_scheme(Path(tmpdir))
                config_path = scheme_dir / "config.yaml"
                config_path.write_text(
                    config_path.read_text(encoding="utf-8").replace(
                        "status: paused\n",
                        replacement + "status: paused\n",
                    ),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ValueError, "platform_inputs"):
                    load_scheme_config(config_path)

    def test_native_rejects_platform_inputs(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_native_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "status: active\n",
                    "platform_inputs:\n"
                    "  - api-wind-date-v1\n"
                    "status: active\n",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "platform_inputs"):
                load_scheme_config(config_path)

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

    def test_blackbox_display_name_overrides_metadata_without_changing_version(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            original = load_scheme_config(config_path)
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "scheme_id: trial_10y\n",
                    "scheme_id: trial_10y\ndisplay_name: LIQ_EXCESS_A\n",
                ),
                encoding="utf-8",
            )

            displayed = load_scheme_config(config_path)

        self.assertEqual(original.name, "10Y Trial")
        self.assertEqual(displayed.name, "LIQ_EXCESS_A")
        self.assertEqual(displayed.description, "")
        self.assertEqual(original.config_hash, displayed.config_hash)
        self.assertEqual(original.scheme_version, displayed.scheme_version)

    def test_new_blackbox_metadata_owner_is_mapped_and_name_is_authoritative(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            metadata_path = scheme_dir / "delivery" / "trial_10y.json"
            raw = json.loads(metadata_path.read_text(encoding="utf-8"))
            raw.update(
                owner="ALGO-A",
                description="使用期限利差和滚动分类模型形成方向信号。",
            )
            metadata_path.write_text(
                json.dumps(raw, ensure_ascii=False),
                encoding="utf-8",
            )

            config = load_scheme_config(scheme_dir / "config.yaml")

        self.assertEqual(config.name, "10Y Trial")
        self.assertEqual(config.owner, "ALGO-A")
        self.assertEqual(
            config.description,
            "使用期限利差和滚动分类模型形成方向信号。",
        )

    def test_new_blackbox_rejects_display_name_override(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            metadata_path = scheme_dir / "delivery" / "trial_10y.json"
            raw = json.loads(metadata_path.read_text(encoding="utf-8"))
            raw.update(
                owner="ALGO-A",
                description="使用期限利差和滚动分类模型形成方向信号。",
            )
            metadata_path.write_text(
                json.dumps(raw, ensure_ascii=False),
                encoding="utf-8",
            )
            config_path = scheme_dir / "config.yaml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "scheme_id: trial_10y\n",
                    "scheme_id: trial_10y\ndisplay_name: forbidden\n",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "display_name"):
                load_scheme_config(config_path)

    def test_blackbox_description_is_mapped_and_version_bound(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            metadata_path = scheme_dir / "delivery" / "trial_10y.json"
            original = load_scheme_config(config_path)
            raw = json.loads(metadata_path.read_text(encoding="utf-8"))
            raw["description"] = "使用期限利差和滚动分类模型形成方向信号。"
            metadata_path.write_text(
                json.dumps(raw, ensure_ascii=False),
                encoding="utf-8",
            )

            described = load_scheme_config(config_path)

        self.assertEqual(
            described.description,
            "使用期限利差和滚动分类模型形成方向信号。",
        )
        self.assertNotEqual(original.manifest_hash, described.manifest_hash)
        self.assertNotEqual(original.scheme_version, described.scheme_version)

    def test_blackbox_display_name_must_be_non_empty_when_present(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "scheme_id: trial_10y\n",
                    "scheme_id: trial_10y\ndisplay_name: '   '\n",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "display_name"):
                load_scheme_config(config_path)

    def test_blackbox_version_changes_when_runtime_profile_changes(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            first = load_scheme_config(config_path)
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "runtime_profile: blackbox-v2-v1",
                    "runtime_profile: blackbox-v2-v2",
                ),
                encoding="utf-8",
            )

            second = load_scheme_config(config_path)

        self.assertNotEqual(first.config_hash, second.config_hash)
        self.assertNotEqual(first.scheme_version, second.scheme_version)

    def test_blackbox_version_changes_when_data_schema_version_changes(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            first = load_scheme_config(config_path)
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "data_schema_version: data-bridge-v1",
                    "data_schema_version: data-bridge-v2",
                ),
                encoding="utf-8",
            )

            second = load_scheme_config(config_path)

        self.assertNotEqual(first.config_hash, second.config_hash)
        self.assertNotEqual(first.scheme_version, second.scheme_version)

    def test_blackbox_version_changes_when_schedule_cron_changes(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            first = load_scheme_config(config_path)
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "  cron: '3 7 * * 1-5'", "  cron: '8 7 * * 1-5'"
                ),
                encoding="utf-8",
            )

            second = load_scheme_config(config_path)

        self.assertNotEqual(first.config_hash, second.config_hash)
        self.assertNotEqual(first.scheme_version, second.scheme_version)

    def test_blackbox_version_changes_when_schedule_timezone_changes(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            first = load_scheme_config(config_path)
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "  timezone: Asia/Shanghai", "  timezone: UTC"
                ),
                encoding="utf-8",
            )

            second = load_scheme_config(config_path)

        self.assertNotEqual(first.config_hash, second.config_hash)
        self.assertNotEqual(first.scheme_version, second.scheme_version)

    def test_blackbox_version_changes_when_schedule_timeout_changes(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            first = load_scheme_config(config_path)
            text = config_path.read_text(encoding="utf-8").replace(
                "  timeout_sec: 3600\n",
                "  timeout_sec: 300\n",
            )
            config_path.write_text(text, encoding="utf-8")

            second = load_scheme_config(config_path)

        self.assertNotEqual(first.config_hash, second.config_hash)
        self.assertNotEqual(first.scheme_version, second.scheme_version)

    def test_repository_blackbox_configs_declare_3600_timeout(self) -> None:
        import yaml

        project_root = Path(__file__).resolve().parents[1]
        blackbox_paths = []
        for path in sorted((project_root / "schemes").glob("*/config.yaml")):
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            if raw.get("runtime_type") == "blackbox_v2":
                blackbox_paths.append(path)
                self.assertEqual(raw["schedule"]["timeout_sec"], 3600, str(path))
        self.assertEqual(len(blackbox_paths), 39)

    def test_blackbox_version_changes_when_delivery_script_path_changes(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            first = load_scheme_config(config_path)
            alternate_dir = scheme_dir / "alternate"
            alternate_dir.mkdir()
            original_path = scheme_dir / "delivery" / "trial_10y.py"
            (alternate_dir / "trial_10y.py").write_bytes(original_path.read_bytes())
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "delivery/trial_10y.py", "alternate/trial_10y.py"
                ),
                encoding="utf-8",
            )

            second = load_scheme_config(config_path)

        self.assertEqual(first.code_hash, second.code_hash)
        self.assertNotEqual(first.config_hash, second.config_hash)
        self.assertNotEqual(first.scheme_version, second.scheme_version)

    def test_blackbox_version_changes_when_delivery_metadata_path_changes(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            first = load_scheme_config(config_path)
            alternate_dir = scheme_dir / "alternate"
            alternate_dir.mkdir()
            original_path = scheme_dir / "delivery" / "trial_10y.json"
            (alternate_dir / "trial_10y.json").write_bytes(original_path.read_bytes())
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "delivery/trial_10y.json", "alternate/trial_10y.json"
                ),
                encoding="utf-8",
            )

            second = load_scheme_config(config_path)

        self.assertEqual(first.manifest_hash, second.manifest_hash)
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

    def test_blackbox_version_ignores_unknown_nested_config_keys(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            first = load_scheme_config(config_path)
            text = config_path.read_text(encoding="utf-8")
            text = text.replace(
                "  timezone: Asia/Shanghai\n",
                "  timezone: Asia/Shanghai\n  audit_note: ignored\n",
            )
            text = text.replace(
                "  metadata: delivery/trial_10y.json\n",
                "  metadata: delivery/trial_10y.json\n  evidence_id: ignored\n",
            )
            config_path.write_text(text, encoding="utf-8")

            second = load_scheme_config(config_path)

        self.assertEqual(first.config_hash, second.config_hash)
        self.assertEqual(first.scheme_version, second.scheme_version)

    def test_blackbox_canonical_hash_is_stable_across_key_order(self) -> None:
        from shared.blackbox_v2.versioning import compute_blackbox_config_hash

        first = _canonical_raw_config()
        reordered = {
            "delivery": {
                "metadata": "delivery/trial_10y.json",
                "script": "delivery/trial_10y.py",
            },
            "schedule": {
                "timeout_sec": 3600,
                "timezone": "Asia/Shanghai",
                "cron": "3 7 * * 1-5",
            },
            "data_schema_version": "data-bridge-v1",
            "runtime_profile": "blackbox-v2-v1",
            "input_source": "data_bridge_current",
            "runtime_type": "blackbox_v2",
        }

        self.assertEqual(
            compute_blackbox_config_hash(first),
            compute_blackbox_config_hash(reordered),
        )

    def test_blackbox_legacy_canonical_hash_is_frozen(self) -> None:
        from shared.blackbox_v2.versioning import compute_blackbox_config_hash

        self.assertEqual(
            compute_blackbox_config_hash(_canonical_raw_config()),
            "7f90072e90291b9e89f80244e0779dc03ee496099e7b3015968fc422dccef072",
        )

    def test_blackbox_canonical_config_rejects_invalid_platform_inputs(self) -> None:
        from shared.blackbox_v2.versioning import canonical_platform_config

        for value in ([], ["unknown-input-v1"], ["api-wind-date-v1"] * 2):
            with self.subTest(value=value):
                raw = _canonical_raw_config()
                raw["platform_inputs"] = value

                with self.assertRaisesRegex(ValueError, "platform_inputs"):
                    canonical_platform_config(raw)

    def test_blackbox_canonical_config_is_isolated_from_nested_raw_maps(self) -> None:
        from shared.blackbox_v2.versioning import canonical_platform_config

        raw = _canonical_raw_config()
        canonical = canonical_platform_config(raw)

        self.assertIsNot(canonical["schedule"], raw["schedule"])
        self.assertIsNot(canonical["delivery"], raw["delivery"])
        raw["schedule"]["cron"] = "8 7 * * 1-5"
        raw["delivery"]["script"] = "alternate/trial_10y.py"

        self.assertEqual(canonical["schedule"]["cron"], "3 7 * * 1-5")
        self.assertEqual(canonical["delivery"]["script"], "delivery/trial_10y.py")

    def test_blackbox_canonical_config_rejects_missing_required_fields(self) -> None:
        from shared.blackbox_v2.versioning import canonical_platform_config

        required_paths = (
            "runtime_type",
            "input_source",
            "runtime_profile",
            "data_schema_version",
            "schedule",
            "schedule.cron",
            "delivery",
            "delivery.script",
            "delivery.metadata",
        )
        for path in required_paths:
            with self.subTest(path=path):
                raw = _canonical_raw_config()
                parent = raw
                parts = path.split(".")
                for part in parts[:-1]:
                    parent = parent[part]
                parent.pop(parts[-1])

                with self.assertRaises(ValueError) as context:
                    canonical_platform_config(raw)
                self.assertEqual(str(context.exception), f"{path} is required")

    def test_blackbox_default_timezone_hash_matches_explicit_default(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            explicit = load_scheme_config(config_path)
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "  timezone: Asia/Shanghai\n", ""
                ),
                encoding="utf-8",
            )

            omitted = load_scheme_config(config_path)

        self.assertEqual(omitted.schedule.timezone, "Asia/Shanghai")
        self.assertEqual(explicit.config_hash, omitted.config_hash)
        self.assertEqual(explicit.scheme_version, omitted.scheme_version)

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
                "  timeout_sec: 3600",
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


def _canonical_raw_config() -> dict:
    return {
        "runtime_type": "blackbox_v2",
        "input_source": "data_bridge_current",
        "runtime_profile": "blackbox-v2-v1",
        "data_schema_version": "data-bridge-v1",
        "schedule": {
            "cron": "3 7 * * 1-5",
            "timezone": "Asia/Shanghai",
            "timeout_sec": 3600,
        },
        "delivery": {
            "script": "delivery/trial_10y.py",
            "metadata": "delivery/trial_10y.json",
        },
    }


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
