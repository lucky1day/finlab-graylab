from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from harness.context import GateContext


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SchemeConfigLoaderTests(unittest.TestCase):
    def test_load_yaml_mapping_uses_pyyaml_for_nested_mapping_and_scalars(self) -> None:
        from shared.scheme_config_loader import load_yaml_mapping

        expected = {
            "scheme": {
                "enabled": True,
                "horizon": 5,
                "name": "demo",
                "tenors": ["5Y", "10Y"],
            }
        }
        yaml_module = Mock()
        yaml_module.safe_load.return_value = expected
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("scheme: demo\n", encoding="utf-8")

            with patch(
                "shared.scheme_config_loader.importlib.import_module",
                return_value=yaml_module,
            ):
                actual = load_yaml_mapping(config_path)

        self.assertEqual(actual, expected)
        yaml_module.safe_load.assert_called_once_with("scheme: demo\n")

    def test_load_yaml_mapping_falls_back_without_pyyaml(self) -> None:
        from shared.scheme_config_loader import load_yaml_mapping

        text = "\n".join(
            [
                "scheme:",
                "  enabled: true",
                "  horizon: 5",
                "  name: demo",
                "  targets:",
                "    - tenor: 5Y",
                "      active: false",
                "    - tenor: 10Y",
                "      active: true",
            ]
        ) + "\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(text, encoding="utf-8")
            missing_yaml = ModuleNotFoundError(
                "No module named 'yaml'",
                name="yaml",
            )

            with patch(
                "shared.scheme_config_loader.importlib.import_module",
                side_effect=missing_yaml,
            ):
                actual = load_yaml_mapping(config_path)

        self.assertEqual(
            actual,
            {
                "scheme": {
                    "enabled": True,
                    "horizon": 5,
                    "name": "demo",
                    "targets": [
                        {"tenor": "5Y", "active": False},
                        {"tenor": "10Y", "active": True},
                    ],
                }
            },
        )

    def test_load_yaml_mapping_propagates_pyyaml_dependency_import_error(self) -> None:
        from shared.scheme_config_loader import load_yaml_mapping

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("scheme_id: demo\n", encoding="utf-8")
            dependency_error = ModuleNotFoundError(
                "No module named '_yaml'",
                name="_yaml",
            )

            with (
                patch(
                    "shared.scheme_config_loader.importlib.import_module",
                    side_effect=dependency_error,
                ),
                self.assertRaises(ModuleNotFoundError) as caught,
            ):
                load_yaml_mapping(config_path)

        self.assertIs(caught.exception, dependency_error)

    def test_load_yaml_mapping_rejects_non_mapping_from_pyyaml(self) -> None:
        from shared.scheme_config_loader import load_yaml_mapping

        yaml_module = Mock()
        yaml_module.safe_load.return_value = ["not", "a", "mapping"]
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

            with (
                patch(
                    "shared.scheme_config_loader.importlib.import_module",
                    return_value=yaml_module,
                ),
                self.assertRaisesRegex(ValueError, "must contain a YAML mapping"),
            ):
                load_yaml_mapping(config_path)

    def test_load_yaml_mapping_rejects_non_mapping_without_pyyaml(self) -> None:
        from shared.scheme_config_loader import load_yaml_mapping

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
            missing_yaml = ModuleNotFoundError(
                "No module named 'yaml'",
                name="yaml",
            )

            with (
                patch(
                    "shared.scheme_config_loader.importlib.import_module",
                    side_effect=missing_yaml,
                ),
                self.assertRaisesRegex(ValueError, "must contain a mapping"),
            ):
                load_yaml_mapping(config_path)

    def test_load_yaml_mapping_propagates_invalid_yaml(self) -> None:
        from shared.scheme_config_loader import load_yaml_mapping

        import yaml

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("scheme: [unterminated\n", encoding="utf-8")

            with self.assertRaises(yaml.YAMLError):
                load_yaml_mapping(config_path)


class OnboardingPolicyTests(unittest.TestCase):
    def test_repo_policy_lists_all_and_only_native_v1_schemes(self) -> None:
        from harness.contracts.onboarding_policy import load_onboarding_policy
        from shared.scheme_config_loader import load_yaml_mapping

        policy = load_onboarding_policy(PROJECT_ROOT)
        native_ids: set[str] = set()
        blackbox_ids: set[str] = set()
        for config_path in sorted((PROJECT_ROOT / "schemes").glob("*/config.yaml")):
            raw = load_yaml_mapping(config_path)
            runtime_type = str(raw.get("runtime_type", "native_adapter"))
            if runtime_type == "native_adapter":
                native_ids.add(config_path.parent.name)
            elif runtime_type == "blackbox_v2":
                blackbox_ids.add(config_path.parent.name)

        self.assertEqual(set(policy.legacy_native_scheme_ids), native_ids)
        self.assertTrue(blackbox_ids)
        self.assertTrue(set(policy.legacy_native_scheme_ids).isdisjoint(blackbox_ids))

    def test_policy_allows_registered_native_and_all_blackbox_schemes(self) -> None:
        from harness.contracts.onboarding_policy import validate_onboarding_policy

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_policy(project_root, legacy_ids=["existing_native"])

            self.assertEqual(
                validate_onboarding_policy(project_root, "existing_native", "native_adapter"),
                [],
            )
            self.assertEqual(
                validate_onboarding_policy(project_root, "new_blackbox", "blackbox_v2"),
                [],
            )

    def test_policy_rejects_unregistered_native_scheme(self) -> None:
        from harness.contracts.onboarding_policy import validate_onboarding_policy

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_policy(project_root, legacy_ids=["existing_native"])

            errors = validate_onboarding_policy(project_root, "new_native", "native_adapter")

        self.assertEqual(
            errors,
            [
                "native_adapter onboarding is maintenance-only; "
                "scheme_id is not in legacy_native_scheme_ids: new_native"
            ],
        )

    def test_native_static_gate_rejects_unregistered_scheme(self) -> None:
        from harness.gates.static_gate import StaticGate

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_policy(project_root, legacy_ids=["existing_native"])
            _write_native_scheme(project_root, "new_native")

            result = StaticGate().run(_context(project_root, "new_native"))

        self.assertFalse(result.passed)
        self.assertTrue(any("maintenance-only" in error for error in result.errors), result.errors)
        evidence = {item.key: item.value for item in result.evidence}
        self.assertEqual(evidence["onboarding_policy_allowed"], False)

    def test_activation_rejects_unregistered_native_before_gate_history(self) -> None:
        from harness.gates.activate_gate import ActivationGate

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            _write_policy(project_root, legacy_ids=["existing_native"])
            _write_native_scheme(project_root, "new_native")

            with patch(
                "harness.gates.activate_gate._verify_gate_history"
            ) as verify_history:
                result = ActivationGate().run(_context(project_root, "new_native"))

        self.assertFalse(result.passed)
        self.assertTrue(any("maintenance-only" in error for error in result.errors), result.errors)
        verify_history.assert_not_called()


def _write_policy(project_root: Path, *, legacy_ids: list[str]) -> None:
    path = project_root / "deploy" / "onboarding_policy_v1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "policy_version": "1.0",
                "new_scheme_runtime_type": "blackbox_v2",
                "native_v1_mode": "maintenance_only",
                "legacy_native_scheme_ids": legacy_ids,
            }
        ),
        encoding="utf-8",
    )


def _write_native_scheme(project_root: Path, scheme_id: str) -> None:
    scheme_dir = project_root / "schemes" / scheme_id
    (scheme_dir / "core").mkdir(parents=True)
    (scheme_dir / "__init__.py").write_text("", encoding="utf-8")
    (scheme_dir / "core" / "__init__.py").write_text("", encoding="utf-8")
    (scheme_dir / "predict.py").write_text(
        "\n".join(
            [
                "from shared.input_artifacts import build_daily_input_artifact",
                f'SCHEME_ID = "{scheme_id}"',
                "def run(predict_date: str):",
                "    return []",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (scheme_dir / "config.yaml").write_text(
        "\n".join(
            [
                f"scheme_id: {scheme_id}",
                "runtime_type: native_adapter",
                "input_source: legacy_db",
                'name: "Demo"',
                'description: "Demo native scheme"',
                "horizon: 1",
                'task_type: "T+1"',
                'tenors: ["10Y"]',
                "frequency: daily",
                "schedule:",
                '  cron: "25 9 * * 1-5"',
                '  timezone: "Asia/Shanghai"',
                "entry_point: predict.run",
                "status: paused",
                "input_spec:",
                "  data_version: shared_data_service_daily.v1",
                '  required_columns: ["date", "TB0YWI0C"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _context(project_root: Path, scheme_id: str) -> GateContext:
    return GateContext(
        scheme_id=scheme_id,
        predict_date="2026-07-19",
        project_root=project_root,
        report_dir=project_root / "reports",
        operation="test-operation",
    )


if __name__ == "__main__":
    unittest.main()
