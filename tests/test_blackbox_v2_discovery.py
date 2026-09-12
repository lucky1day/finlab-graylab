from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class BlackboxV2DiscoveryTests(unittest.TestCase):
    def test_native_attachment_declarations_are_rejected(self) -> None:
        import yaml
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.versioning import canonical_platform_config
        from shared.scheme_config_schema import validate_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme = _write_blackbox_scheme(Path(tmpdir))
            config_path = scheme / "config.yaml"
            raw = yaml.safe_load(config_path.read_text())
            for value in ({}, {"predict.py": "a" * 64}, None):
                with self.subTest(value=value):
                    candidate = raw | {"native_attachments": value}
                    self.assertIn("native_attachments is not supported", validate_config(candidate, scheme.name))
                    with self.assertRaisesRegex(ValueError, "native_attachments"):
                        canonical_platform_config(candidate)
                    config_path.write_text(yaml.safe_dump(candidate))
                    with self.assertRaisesRegex(ValueError, "native_attachments"):
                        load_scheme_config(config_path)

    def test_fact_horizon_preserves_execution_contract_and_changes_version(self) -> None:
        from scheduler.discovery import load_scheme_config
        from shared.task_specs import TASK_COMBINATIONS

        for scheme_id, task_type, fact_horizon in (
            ("weekly_5y_direct_0529", "weekly_point", 6),
            ("weekly_avg_5y_lgbm_0529", "weekly_average", 6),
            ("monthly_5y_knn_top20_0629", "monthly", 30),
        ):
            with self.subTest(task_type=task_type), tempfile.TemporaryDirectory() as tmpdir:
                old_dir = _write_blackbox_scheme(Path(tmpdir))
                scheme_dir = old_dir.with_name(scheme_id)
                old_dir.rename(scheme_dir)
                config_path = scheme_dir / "config.yaml"
                config_path.write_text(config_path.read_text().replace("trial_10y", scheme_id))
                for suffix in ("py", "json"):
                    (scheme_dir / "delivery" / f"trial_10y.{suffix}").rename(
                        scheme_dir / "delivery" / f"{scheme_id}.{suffix}"
                    )
                metadata_path = scheme_dir / "delivery" / f"{scheme_id}.json"
                metadata = json.loads(metadata_path.read_text())
                metadata.update(scheme_id=scheme_id, task_type=task_type,
                                target_rule=TASK_COMBINATIONS[task_type][1])
                metadata_path.write_text(json.dumps(metadata))
                original = load_scheme_config(config_path)
                config_path.write_text(config_path.read_text() + f"\nfact_horizon: {fact_horizon}\n")
                projected = load_scheme_config(config_path)
                self.assertEqual(projected.horizon, fact_horizon)
                self.assertEqual(projected.blackbox_metadata.horizon, 1)
                self.assertEqual(original.manifest_hash, projected.manifest_hash)
                self.assertNotEqual(original.scheme_version, projected.scheme_version)
                metadata.update(task_type="T+1",
                                target_rule=TASK_COMBINATIONS["T+1"][1])
                metadata_path.write_text(json.dumps(metadata))
                with self.assertRaisesRegex(ValueError, "fact_horizon"):
                    load_scheme_config(config_path)

    def test_fact_horizon_rejects_unapproved_identity_and_value(self) -> None:
        from shared.scheme_config_schema import validate_config

        for scheme_id, horizon in (("trial_10y", 6),
                                   ("weekly_5y_direct_0529", 30),
                                   ("weekly_5y_direct_0529", True),
                                   ("weekly_5y_direct_0529", 6.0)):
            raw = {**_canonical_raw_config(), "scheme_id": scheme_id,
                   "fact_horizon": horizon}
            self.assertTrue(any("fact_horizon" in error
                                for error in validate_config(raw, scheme_id)))
        native = {**_canonical_raw_config(), "scheme_id": "weekly_5y_direct_0529",
                  "runtime_type": "native_adapter", "fact_horizon": 6}
        self.assertTrue(any("fact_horizon" in error for error in
                            validate_config(native, native["scheme_id"])))

    def test_loads_business_identity_from_delivery_metadata(self) -> None:
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

        self.assertEqual(config.runtime_type, "blackbox_v2")
        self.assertIs(config.incremental_state, False)
        self.assertEqual(config.input_source, "data_bridge_current")
        self.assertEqual(config.version_status, "draft")
        self.assertEqual(config.name, "10Y Trial")
        self.assertEqual(config.owner, "ALGO-A")
        self.assertEqual(
            config.description,
            "使用期限利差和滚动分类模型形成方向信号。",
        )
        self.assertEqual(config.algorithm_version, "1.2.3")
        self.assertEqual(config.contract_version, "1.0")
        self.assertEqual(config.target_rule, "target_date_yield_vs_feature_date_yield")
        self.assertEqual(config.horizon, 1)
        self.assertEqual(config.task_type, "T+1")
        self.assertEqual(config.tenors, ["10Y"])
        self.assertEqual(config.frequency, "daily")
        self.assertEqual(config.schedule.timeout_sec, 3600)
        self.assertEqual(config.delivery_script, (scheme_dir / "delivery" / "trial_10y.py").resolve())
        self.assertEqual(config.delivery_metadata, (scheme_dir / "delivery" / "trial_10y.json").resolve())
        self.assertIsNotNone(config.blackbox_metadata)
        self.assertEqual(config.blackbox_metadata.scheme_id, "trial_10y")

    def test_incremental_state_opt_in_changes_only_config_identity(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            original = load_scheme_config(config_path)
            config_path.write_text(
                config_path.read_text(encoding="utf-8")
                + "incremental_state: true\n",
                encoding="utf-8",
            )
            declared = load_scheme_config(config_path)

        self.assertIs(declared.incremental_state, True)
        self.assertEqual(original.code_hash, declared.code_hash)
        self.assertEqual(original.manifest_hash, declared.manifest_hash)
        self.assertNotEqual(original.config_hash, declared.config_hash)
        self.assertNotEqual(original.scheme_version, declared.scheme_version)

    def test_legacy_platform_input_field_only_preserves_version_hash(self) -> None:
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

        self.assertFalse(hasattr(declared, "platform_inputs"))
        self.assertNotEqual(original.config_hash, declared.config_hash)
        self.assertNotEqual(original.scheme_version, declared.scheme_version)

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

    def test_rejects_additional_blackbox_delivery_file(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            (scheme_dir / "delivery" / "model.pkl").write_bytes(b"model")

            with self.assertRaisesRegex(ValueError, "exactly two regular files"):
                load_scheme_config(scheme_dir / "config.yaml")

    def test_rejects_additional_blackbox_scheme_root_file(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            (scheme_dir / "model.pkl").write_bytes(b"model")

            with self.assertRaisesRegex(ValueError, "exactly config.yaml and delivery"):
                load_scheme_config(scheme_dir / "config.yaml")

    def test_rejects_symlinked_blackbox_delivery_directory(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = _write_blackbox_scheme(root)
            real_delivery = root / "real-delivery"
            (scheme_dir / "delivery").rename(real_delivery)
            (scheme_dir / "delivery").symlink_to(
                real_delivery,
                target_is_directory=True,
            )

            with self.assertRaisesRegex(ValueError, "regular directory"):
                load_scheme_config(scheme_dir / "config.yaml")

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


    def test_blackbox_version_tracks_each_canonical_input(self) -> None:
        from scheduler.discovery import load_scheme_config

        for changed_input, hash_field in (
            ("data_schema", "config_hash"),
            ("script", "code_hash"),
            ("metadata", "manifest_hash"),
        ):
            with self.subTest(changed_input=changed_input), tempfile.TemporaryDirectory() as tmpdir:
                scheme_dir = _write_blackbox_scheme(Path(tmpdir))
                config_path = scheme_dir / "config.yaml"
                first = load_scheme_config(config_path)
                if changed_input == "data_schema":
                    config_path.write_text(
                        config_path.read_text(encoding="utf-8").replace(
                            "data_schema_version: data-bridge-v1",
                            "data_schema_version: data-bridge-v2",
                        ),
                        encoding="utf-8",
                    )
                else:
                    suffix = "py" if changed_input == "script" else "json"
                    path = scheme_dir / "delivery" / f"trial_10y.{suffix}"
                    path.write_bytes(path.read_bytes() + b"\n")

                second = load_scheme_config(config_path)

                self.assertNotEqual(
                    getattr(first, hash_field),
                    getattr(second, hash_field),
                )
                self.assertNotEqual(first.scheme_version, second.scheme_version)

    def test_metadata_identity_and_hash_use_the_same_bytes(self) -> None:
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.contracts import load_metadata_bytes

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_blackbox_scheme(Path(tmpdir))
            metadata_path = scheme_dir / "delivery" / "trial_10y.json"
            original_payload = metadata_path.read_bytes()
            replacement = json.loads(original_payload)
            replacement["algorithm_version"] = "2.0.0"
            replacement_payload = json.dumps(replacement).encode("utf-8")

            def replace_after_read(payload: bytes, *, source: str):
                metadata_path.write_bytes(replacement_payload)
                return load_metadata_bytes(payload, source=source)

            with patch(
                "shared.blackbox_v2.contracts.load_metadata_bytes",
                side_effect=replace_after_read,
            ):
                first = load_scheme_config(scheme_dir / "config.yaml")

            second = load_scheme_config(scheme_dir / "config.yaml")

        self.assertEqual(first.algorithm_version, "1.2.3")
        self.assertEqual(
            first.manifest_hash,
            hashlib.sha256(original_payload).hexdigest(),
        )
        self.assertEqual(second.algorithm_version, "2.0.0")
        self.assertEqual(
            second.manifest_hash,
            hashlib.sha256(replacement_payload).hexdigest(),
        )


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

    def test_blackbox_factor_input_mode_changes_canonical_hash(self) -> None:
        from shared.blackbox_v2.versioning import compute_blackbox_config_hash

        legacy = _canonical_raw_config()
        explicit_legacy = {**legacy, "factor_input_mode": "legacy_v1"}
        algorithm_managed = {
            **legacy,
            "factor_input_mode": "algorithm_managed",
        }

        self.assertNotEqual(
            compute_blackbox_config_hash(legacy),
            compute_blackbox_config_hash(explicit_legacy),
        )
        self.assertNotEqual(
            compute_blackbox_config_hash(explicit_legacy),
            compute_blackbox_config_hash(algorithm_managed),
        )

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
