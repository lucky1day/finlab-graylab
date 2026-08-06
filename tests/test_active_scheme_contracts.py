from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from scheduler.discovery import active_schemes


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMES_ROOT = PROJECT_ROOT / "schemes"


class ActiveSchemeContractTests(unittest.TestCase):
    def test_all_active_scheme_configs_have_runnable_platform_contracts(self) -> None:
        configs = active_schemes(SCHEMES_ROOT)
        active_dirs = {
            path.parent.name
            for path in SCHEMES_ROOT.glob("*/config.yaml")
            if yaml.safe_load(path.read_text(encoding="utf-8")).get("status")
            == "active"
        }

        self.assertEqual({config.scheme_id for config in configs}, active_dirs)
        self.assertEqual(len(configs), len(active_dirs))

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
                else:
                    self.assertTrue(config.entry_point)


if __name__ == "__main__":
    unittest.main()
