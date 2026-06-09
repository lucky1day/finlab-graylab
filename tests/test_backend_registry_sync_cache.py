from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class BackendRegistrySyncCacheTests(unittest.TestCase):
    def test_registry_sync_skips_when_scheme_configs_are_unchanged(self) -> None:
        from backend import services

        with tempfile.TemporaryDirectory() as tmpdir:
            schemes_root = Path(tmpdir)
            config_path = _write_scheme(schemes_root, "demo_daily", "Demo")
            engine = object()

            services.reset_registry_sync_cache()
            with patch("backend.services.sync_scheme_registry") as sync_mock:
                self.assertTrue(services.sync_registry_from_configs(engine, schemes_root=schemes_root))
                self.assertFalse(services.sync_registry_from_configs(engine, schemes_root=schemes_root))

                now = time.time() + 2
                config_path.write_text(config_path.read_text(encoding="utf-8").replace("Demo", "Demo v2"), encoding="utf-8")
                os.utime(config_path, (now, now))

                self.assertTrue(services.sync_registry_from_configs(engine, schemes_root=schemes_root))

            self.assertEqual(sync_mock.call_count, 2)


def _write_scheme(root: Path, scheme_id: str, name: str) -> Path:
    scheme_dir = root / scheme_id
    scheme_dir.mkdir(parents=True)
    config_path = scheme_dir / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                f"scheme_id: {scheme_id}",
                f"name: {name}",
                "description: Demo",
                "horizon: 1",
                'tenors: ["10Y"]',
                "frequency: daily",
                "schedule:",
                '  cron: "25 9 * * 1-5"',
                '  timezone: "Asia/Shanghai"',
                "entry_point: predict.run",
                "status: active",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


if __name__ == "__main__":
    unittest.main()
