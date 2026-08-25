from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SOURCE_DATABASE_CONFIG = {
    "version": "source-runtime-database-v1",
    "user": "source_reader",
    "password": "injected-source-secret",
    "host": "127.0.0.1",
    "port": 43306,
    "database": "bfl_source_test",
    "charset": "utf8mb4",
}


def _write_packaged_config(path: Path) -> None:
    path.write_text(
        "from __future__ import annotations\n"
        "from typing import Any\n"
        "DB_CONFIG: dict[str, Any] = {\n"
        '    "user": "packaged-user",\n'
        '    "password": "packaged-password-must-not-run",\n'
        '    "host": "packaged.invalid",\n'
        '    "port": 3306,\n'
        '    "database": "packaged_database",\n'
        '    "charset": "utf8mb4",\n'
        "}\n",
        encoding="utf-8",
    )


def _write_private_binding(path: Path) -> None:
    path.write_text(
        json.dumps(
            SOURCE_DATABASE_CONFIG,
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


class SourceRunnerDatabaseIsolationTests(unittest.TestCase):
    def test_missing_dedicated_configuration_does_not_fall_back_to_service_database(
        self,
    ) -> None:
        from shared.source_runtime_database import (
            load_source_runtime_database_config,
        )

        legacy_environment = {
            "BOND_DB_USER": "legacy-user",
            "BOND_DB_PASSWORD": "legacy-password",
            "BOND_DB_HOST": "127.0.0.1",
            "BOND_DB_PORT": "3306",
            "BOND_DB_NAME": "bond_db",
        }
        with (
            patch.dict(os.environ, legacy_environment, clear=True),
            self.assertRaisesRegex(
                RuntimeError,
                "source runner database injection is incomplete",
            ),
        ):
            load_source_runtime_database_config()

    def test_daily_runtime_validates_original_then_rewrites_only_private_copy(
        self,
    ) -> None:
        from shared.daily_0629_source_evidence import (
            source_package_tree_sha256,
        )
        from shared.daily_0629_source_runner import _source_runtime

        with tempfile.TemporaryDirectory() as tmpdir:
            source_root = Path(tmpdir) / "source"
            source_root.mkdir()
            original_config = source_root / "db_config.py"
            _write_packaged_config(original_config)
            (source_root / "runner.bin").write_bytes(b"frozen-source")
            pycache = source_root / "__pycache__"
            pycache.mkdir()
            (pycache / "db_config.cpython-312.pyc").write_bytes(
                b"unchecked-stale-bytecode"
            )
            binding_path = Path(tmpdir) / "source-db.json"
            _write_private_binding(binding_path)
            original_bytes = original_config.read_bytes()
            original_hash = source_package_tree_sha256(source_root)

            with (
                patch.dict(
                    os.environ,
                    {
                        "BFL_SOURCE_DB_CONFIG_ROOT":
                            str(binding_path.parent.resolve()),
                        "BFL_SOURCE_DB_CONFIG_PATH":
                            str(binding_path.resolve()),
                    },
                    clear=True,
                ),
                _source_runtime(source_root, original_hash) as runtime_root,
            ):
                runtime_config = runtime_root / "db_config.py"
                namespace: dict[str, object] = {}
                exec(
                    runtime_config.read_text(encoding="utf-8"),
                    namespace,
                )
                injected = namespace["DB_CONFIG"]
                self.assertIsInstance(injected, dict)
                self.assertEqual(
                    injected["password"],
                    SOURCE_DATABASE_CONFIG["password"],
                )
                self.assertEqual(
                    stat.S_IMODE(runtime_config.stat().st_mode),
                    0o600,
                )
                self.assertNotIn(
                    "packaged-password-must-not-run",
                    runtime_config.read_text(encoding="utf-8"),
                )
                self.assertEqual(
                    list(runtime_root.rglob("*.pyc")),
                    [],
                )
                self.assertFalse(
                    any(
                        child.name == "__pycache__"
                        for child in runtime_root.rglob("*")
                    )
                )

            self.assertEqual(original_config.read_bytes(), original_bytes)
            self.assertEqual(
                source_package_tree_sha256(source_root),
                original_hash,
            )


    def test_source_subprocess_environment_drops_all_database_credentials(
        self,
    ) -> None:
        from shared.source_runtime_database import (
            source_subprocess_environment,
        )

        environment = {
            "BFL_SOURCE_DB_CONFIG_ROOT": "/private",
            "BFL_SOURCE_DB_CONFIG_PATH": "/private/source-db.json",
            "BOND_DB_USER": "service-user",
            "BOND_DB_PASSWORD": "service-secret",
            "BOND_DB_HOST": "production.invalid",
            "BOND_DB_PORT": "3306",
            "BOND_DB_NAME": "bond_db",
            "BOND_DB_CHARSET": "utf8mb4",
            "MYSQL_PWD": "mysql-secret",
            "DATABASE_URL": "mysql://user:password@production/bond_db",
            "KEEP_ME": "safe-value",
        }
        child = source_subprocess_environment(environment)

        self.assertEqual(child["KEEP_ME"], "safe-value")
        self.assertEqual(
            child["PYTHONDONTWRITEBYTECODE"],
            "1",
        )
        for key in environment:
            if (
                key.startswith("BFL_SOURCE_DB_")
                or key.startswith("BOND_DB_")
                or key in {"MYSQL_PWD", "DATABASE_URL"}
            ):
                self.assertNotIn(key, child)
