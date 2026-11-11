from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scheduler.discovery import blackbox_deliveries, discover_schemes


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMES_ROOT = PROJECT_ROOT / "schemes"
MATRIX_PATH = PROJECT_ROOT / "deploy" / "scheme_deployment_matrix_v1.json"


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

    def test_all_matrix_scheme_configs_have_runnable_platform_contracts(self) -> None:
        # Registry（而非 config.status）决定是否在线，故按部署矩阵成员覆盖全部方案目录。
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        matrix_ids = set(matrix["schemes"])
        configs = discover_schemes(SCHEMES_ROOT)
        self.assertEqual({config.scheme_id for config in configs}, matrix_ids)

        for config in configs:
            with self.subTest(scheme_id=config.scheme_id):
                self.assertIn(config.runtime_type, {"native_adapter", "blackbox_v2"})
                self.assertTrue(config.scheme_version)
                self.assertTrue(config.tenors)
                self.assertGreater(config.horizon, 0)
                if config.runtime_type == "blackbox_v2":
                    deliveries = blackbox_deliveries(config)
                    self.assertTrue(deliveries)
                    for delivery in deliveries:
                        self.assertTrue(delivery.script_path.is_file())
                        self.assertTrue(delivery.metadata_path.is_file())
                    self.assertEqual(config.contract_version, "1.0")


def test_native_inventory_matches_fixed_w4_policy() -> None:
    from scheduler.discovery import load_scheme_config
    from shared.task_specs import load_legacy_native_scheme_ids

    native_ids = {
        path.parent.name
        for path in SCHEMES_ROOT.glob("*/config.yaml")
        if load_scheme_config(path).runtime_type == "native_adapter"
    }
    assert set(load_legacy_native_scheme_ids(PROJECT_ROOT)) == native_ids
