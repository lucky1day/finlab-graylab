from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
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


def _tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = child.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(child.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


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
    def test_database_preflight_reads_exact_native_source_tables(
        self,
    ) -> None:
        from shared.data_contract import (
            CALENDAR_SOURCE_TABLES,
            FACTOR_SOURCE_TABLES,
            METADATA_SOURCE_TABLE,
        )
        from shared.source_runtime_database import (
            SourceRuntimeDatabaseConfig,
            preflight_source_runtime_database_access,
        )

        required_tables = (
            *FACTOR_SOURCE_TABLES,
            METADATA_SOURCE_TABLE,
            *CALENDAR_SOURCE_TABLES,
        )
        executed: list[str] = []

        class _Cursor:
            current_query = ""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, query):
                self.current_query = str(query)
                executed.append(self.current_query)

            def fetchone(self):
                if "DATABASE()" in self.current_query:
                    return ("bfl_source_test", "source_reader@127.0.0.1")
                raise AssertionError(self.current_query)

            def fetchall(self):
                if "SHOW GRANTS" in self.current_query:
                    return (
                        (
                            "GRANT USAGE ON *.* TO "
                            "`source_reader`@`127.0.0.1`",
                        ),
                        (
                            "GRANT SELECT ON `bfl_source_test`.* TO "
                            "`source_reader`@`127.0.0.1`",
                        ),
                    )
                raise AssertionError(self.current_query)

        class _Connection:
            def cursor(self):
                return _Cursor()

            def close(self):
                executed.append("CLOSE")

        config = SourceRuntimeDatabaseConfig(
            user="source_reader",
            password="source-test-secret",
            host="127.0.0.1",
            port=43306,
            database="bfl_source_test",
            charset="utf8mb4",
            config_path=Path("/private/source-db.json"),
        )
        with patch(
            "shared.source_runtime_database.pymysql.connect",
            return_value=_Connection(),
        ) as connect:
            result = preflight_source_runtime_database_access(config)

        self.assertEqual(result.database, config.database)
        self.assertEqual(result.tables, required_tables)
        connect.assert_called_once()
        connect_kwargs = connect.call_args.kwargs
        self.assertEqual(connect_kwargs["user"], config.user)
        self.assertEqual(connect_kwargs["database"], config.database)
        self.assertEqual(connect_kwargs["password"], config.password)
        self.assertEqual(
            tuple(
                query
                for query in executed
                if query.startswith("SELECT * FROM")
            ),
            tuple(
                f"SELECT * FROM `{table}` LIMIT 0"
                for table in required_tables
            ),
        )
        self.assertEqual(executed[-1], "CLOSE")

    def test_database_preflight_rejects_write_or_role_grants(
        self,
    ) -> None:
        from shared.source_runtime_database import (
            SourceRuntimeDatabaseConfig,
            SourceRuntimeDatabasePreflightError,
            preflight_source_runtime_database_access,
        )

        class _Cursor:
            current_query = ""

            def __init__(self, grant: str):
                self.grant = grant

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, query):
                self.current_query = str(query)

            def fetchone(self):
                return ("bfl_source_test", "source_reader@127.0.0.1")

            def fetchall(self):
                return ((self.grant,),)

        class _Connection:
            def __init__(self, grant: str):
                self.grant = grant

            def cursor(self):
                return _Cursor(self.grant)

            def close(self):
                return None

        config = SourceRuntimeDatabaseConfig(
            user="source_reader",
            password="source-test-secret",
            host="127.0.0.1",
            port=43306,
            database="bfl_source_test",
            charset="utf8mb4",
            config_path=Path("/private/source-db.json"),
        )
        unsafe_grants = (
            "GRANT SELECT, INSERT ON `bfl_source_test`.* TO "
            "`source_reader`@`127.0.0.1`",
            "GRANT `source_writer`@`%` TO "
            "`source_reader`@`127.0.0.1`",
            "GRANT SELECT ON `bfl_source_test`.* TO "
            "`source_reader`@`127.0.0.1` WITH GRANT OPTION",
            "GRANT PROXY ON `writer`@`%` TO "
            "`source_reader`@`127.0.0.1`",
            "GRANT SELECT ON *.* TO "
            "`source_reader`@`127.0.0.1`",
            "GRANT BACKUP_ADMIN ON *.* TO "
            "`source_reader`@`127.0.0.1`",
            "GRANT UNKNOWN SOURCE CAPABILITY TO "
            "`source_reader`@`127.0.0.1`",
        )
        for grant in unsafe_grants:
            with (
                self.subTest(grant=grant.split(" TO ", 1)[0]),
                patch(
                    "shared.source_runtime_database.pymysql.connect",
                    return_value=_Connection(grant),
                ),
                self.assertRaises(
                    SourceRuntimeDatabasePreflightError
                ) as caught,
            ):
                preflight_source_runtime_database_access(config)
            self.assertEqual(caught.exception.code, "SOURCE_DB_GRANT_UNSAFE")
            self.assertNotIn(config.password, str(caught.exception))
            self.assertNotIn(grant, str(caught.exception))



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

    def test_all_source_runtimes_make_only_private_directories_writable(
        self,
    ) -> None:
        from shared.daily_0629_source_evidence import (
            source_package_tree_sha256 as daily_tree_sha256,
        )
        from shared.daily_0629_source_runner import (
            _source_runtime as daily_runtime,
        )
        from shared.monthly_source_evidence import (
            source_package_tree_sha256 as monthly_tree_sha256,
        )
        from shared.monthly_source_runner import (
            _source_runtime as monthly_runtime,
        )
        from shared.source_runtime_database import (
            SourceRuntimeDatabaseConfig,
        )
        from shared.weekly_average_lgbm_source_runner import (
            _source_runtime as weekly_runtime,
        )
        from shared.weekly_average_source_evidence import (
            source_package_tree_sha256 as weekly_tree_sha256,
        )

        runtimes = (
            (daily_runtime, daily_tree_sha256),
            (monthly_runtime, monthly_tree_sha256),
            (weekly_runtime, weekly_tree_sha256),
        )
        for runtime, tree_sha256 in runtimes:
            with (
                self.subTest(runtime=runtime.__module__),
                tempfile.TemporaryDirectory() as tmpdir,
            ):
                source_root = Path(tmpdir) / "source"
                nested = source_root / "daily_project" / "nested"
                nested.mkdir(parents=True)
                original_config = source_root / "db_config.py"
                _write_packaged_config(original_config)
                (nested / "frozen.txt").write_bytes(b"frozen-source")
                original_bytes = original_config.read_bytes()
                original_hash = tree_sha256(source_root)
                config = SourceRuntimeDatabaseConfig(
                    user="source_reader",
                    password="source-secret",
                    host="127.0.0.1",
                    port=43306,
                    database="bfl_source_test",
                    charset="utf8mb4",
                    config_path=Path(tmpdir) / "source-db.json",
                )
                frozen_directories = (
                    source_root,
                    source_root / "daily_project",
                    nested,
                )
                for directory in frozen_directories:
                    directory.chmod(0o555)
                try:
                    with runtime(
                        source_root,
                        original_hash,
                        database_config=config,
                    ) as runtime_root:
                        self.assertEqual(
                            stat.S_IMODE(runtime_root.stat().st_mode),
                            0o700,
                        )
                        self.assertEqual(
                            stat.S_IMODE(
                                (runtime_root / "daily_project").stat().st_mode
                            ),
                            0o700,
                        )
                        self.assertEqual(
                            stat.S_IMODE(
                                (
                                    runtime_root
                                    / "daily_project"
                                    / "nested"
                                ).stat().st_mode
                            ),
                            0o700,
                        )
                        (runtime_root / "daily_project" / "output").mkdir()
                        self.assertEqual(
                            stat.S_IMODE(
                                (runtime_root / "db_config.py").stat().st_mode
                            ),
                            0o600,
                        )
                finally:
                    for directory in reversed(frozen_directories):
                        self.assertEqual(
                            stat.S_IMODE(directory.stat().st_mode),
                            0o555,
                        )
                        directory.chmod(0o755)

                self.assertEqual(original_config.read_bytes(), original_bytes)
                self.assertEqual(tree_sha256(source_root), original_hash)

    def test_monthly_and_weekly_runtime_reject_copied_package_drift(
        self,
    ) -> None:
        from shared.monthly_source_runner import (
            _source_runtime as monthly_runtime,
        )
        from shared.weekly_average_lgbm_source_runner import (
            _source_runtime as weekly_runtime,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            source_root = Path(tmpdir) / "source"
            source_root.mkdir()
            _write_packaged_config(source_root / "db_config.py")
            binding_path = Path(tmpdir) / "source-db.json"
            _write_private_binding(binding_path)
            for runtime in (monthly_runtime, weekly_runtime):
                with (
                    self.subTest(runtime=runtime.__module__),
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
                    self.assertRaisesRegex(
                        RuntimeError,
                        "copied source package hash",
                    ),
                ):
                    with runtime(source_root, "0" * 64):
                        self.fail(
                            "mismatched private source copy was accepted"
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


    def test_all_source_runners_redact_password_from_subprocess_failures(
        self,
    ) -> None:
        from shared.daily_0629_source_runner import _run_source_command
        from shared.monthly_source_runner import _run_monthly_module
        from shared.weekly_average_lgbm_source_runner import (
            _run_python_module,
        )

        secret = str(SOURCE_DATABASE_CONFIG["password"])
        failed = subprocess.CompletedProcess(
            args=["fake"],
            returncode=1,
            stdout=f"connection failed for {secret}",
            stderr=f"password={secret}",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            binding_path = root / "source-db.json"
            _write_private_binding(binding_path)
            source_environment = {
                "BFL_SOURCE_DB_CONFIG_ROOT":
                    str(binding_path.parent.resolve()),
                "BFL_SOURCE_DB_CONFIG_PATH":
                    str(binding_path.resolve()),
            }
            cases = (
                (
                    lambda: _run_source_command(
                        ["fake-daily"],
                        root,
                    ),
                    (
                        "shared.daily_0629_source_runner."
                        "run_source_subprocess"
                    ),
                ),
                (
                    lambda: _run_monthly_module(
                        "fake.monthly",
                        "2026-07-25",
                        source_root=root,
                        monthly_root=root,
                    ),
                    (
                        "shared.monthly_source_runner."
                        "run_source_subprocess"
                    ),
                ),
                (
                    lambda: _run_python_module(
                        "fake.weekly",
                        [],
                        source_root=root,
                        weekly_root=root,
                    ),
                    (
                        "shared.weekly_average_lgbm_source_runner."
                        "run_source_subprocess"
                    ),
                ),
            )
            for case, patch_target in cases:
                with (
                    self.subTest(case=case),
                    patch.dict(
                        os.environ,
                        source_environment,
                        clear=True,
                    ),
                    patch(
                        patch_target,
                        return_value=failed,
                    ) as run,
                    self.assertRaises(RuntimeError) as raised,
                ):
                    case()
                message = str(raised.exception)
                self.assertNotIn(secret, message)
                self.assertIn("<redacted>", message)
                child_environment = run.call_args.kwargs["env"]
                self.assertNotIn(
                    "BFL_SOURCE_DB_CONFIG_PATH",
                    child_environment,
                )
                self.assertNotIn("BOND_DB_PASSWORD", child_environment)



    def test_binding_path_must_stay_inside_approved_private_root(
        self,
    ) -> None:
        from shared.source_runtime_database import (
            load_source_runtime_database_config,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            approved_root = root / "private"
            approved_root.mkdir(mode=0o700)
            outside_root = root / "outside"
            outside_root.mkdir(mode=0o700)
            binding_path = outside_root / "source-db.json"
            _write_private_binding(binding_path)

            with (
                self.assertRaisesRegex(
                    RuntimeError,
                    "approved private root",
                ),
                patch.dict(
                    os.environ,
                    {
                        "BFL_SOURCE_DB_CONFIG_ROOT":
                            str(approved_root),
                        "BFL_SOURCE_DB_CONFIG_PATH":
                            str(binding_path),
                    },
                    clear=True,
                ),
            ):
                load_source_runtime_database_config()



if __name__ == "__main__":
    unittest.main()
