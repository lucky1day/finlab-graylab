from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from scheduler.discovery import discover_schemes


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMES_ROOT = PROJECT_ROOT / "schemes"


class ActiveSchemeContractTests(unittest.TestCase):
    def test_invalid_config_fails_closed_instead_of_being_skipped(self) -> None:
        for content in ("scheme_id: broken\n", "- not-a-mapping\n"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmpdir:
                schemes_root = Path(tmpdir)
                config = schemes_root / "broken" / "config.yaml"
                config.parent.mkdir()
                config.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    discover_schemes(schemes_root)

    def test_all_active_scheme_configs_have_runnable_platform_contracts(self) -> None:
        configs = [
            config
            for config in discover_schemes(SCHEMES_ROOT)
            if config.status == "active"
        ]
        active_dirs = {
            path.parent.name
            for path in SCHEMES_ROOT.glob("*/config.yaml")
            if yaml.safe_load(path.read_text(encoding="utf-8")).get("status")
            == "active"
        }

        self.assertEqual({config.scheme_id for config in configs}, active_dirs)

        for config in configs:
            with self.subTest(scheme_id=config.scheme_id):
                self.assertIn(config.runtime_type, {"native_adapter", "blackbox_v2"})
                self.assertTrue(config.scheme_version)
                self.assertTrue(config.tenors)
                self.assertGreater(config.horizon, 0)
                if config.runtime_type == "blackbox_v2":
                    self.assertIsNotNone(config.delivery_script)
                    self.assertIsNotNone(config.delivery_metadata)
                    self.assertTrue(config.delivery_script.is_file())
                    self.assertTrue(config.delivery_metadata.is_file())
                    self.assertEqual(config.contract_version, "1.0")


def test_native_inventory_matches_fixed_w4_policy() -> None:
    from harness.contracts.onboarding_policy import load_onboarding_policy
    from shared.scheme_config_loader import load_yaml_mapping

    native_ids = {
        path.parent.name
        for path in SCHEMES_ROOT.glob("*/config.yaml")
        if load_yaml_mapping(path).get("runtime_type", "native_adapter") == "native_adapter"
    }
    assert set(load_onboarding_policy(PROJECT_ROOT).legacy_native_scheme_ids) == native_ids
