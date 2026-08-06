from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shared.runtime_paths import resolve_runtime_artifact_root


class RuntimePathsTests(unittest.TestCase):
    def test_runtime_artifact_root_uses_service_home(self) -> None:
        with patch(
            "shared.runtime_paths.pwd.getpwuid",
            return_value=SimpleNamespace(pw_dir="/tmp/bfl-service-home"),
        ):
            self.assertEqual(
                resolve_runtime_artifact_root(service_uid=501),
                Path(
                    "/tmp/bfl-service-home/Library/Application Support/"
                    "BondFactorLab/daily-runtime-v1"
                ).resolve(strict=False),
            )


if __name__ == "__main__":
    unittest.main()
