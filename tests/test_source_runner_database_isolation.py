from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
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

    def test_all_archived_source_manifests_pin_package_identity(
        self,
    ) -> None:
        from shared.daily_0629_source_evidence import (
            require_daily_0629_source_evidence,
        )
        from shared.monthly_source_evidence import (
            require_monthly_source_evidence,
        )
        from shared.weekly_average_source_evidence import (
            require_weekly_average_source_evidence,
        )

        cases = (
            (
                require_daily_0629_source_evidence,
                "daily_1y_xgb_1y13_0629",
            ),
            (
                require_monthly_source_evidence,
                "monthly_1y_rf_top30_0629",
            ),
            (
                require_weekly_average_source_evidence,
                "weekly_avg_1y_lgbm_0529",
            ),
        )
        for loader, scheme_id in cases:
            with self.subTest(scheme_id=scheme_id):
                evidence = loader(scheme_id)
                manifest = json.loads(
                    evidence.manifest_path.read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    manifest["source_package_sha256"],
                    evidence.source_package_hash,
                )

    def test_evidence_loaders_reject_package_bytes_outside_manifest_pin(
        self,
    ) -> None:
        from shared.daily_0629_source_evidence import (
            require_daily_0629_source_evidence,
        )
        from shared.monthly_source_evidence import (
            require_monthly_source_evidence,
        )
        from shared.weekly_average_source_evidence import (
            require_weekly_average_source_evidence,
        )

        cases = (
            (
                require_daily_0629_source_evidence,
                "daily_1y_xgb_1y13_0629",
                "shared.daily_0629_source_evidence."
                "source_package_tree_sha256",
            ),
            (
                require_monthly_source_evidence,
                "monthly_1y_rf_top30_0629",
                "shared.monthly_source_evidence."
                "source_package_tree_sha256",
            ),
            (
                require_weekly_average_source_evidence,
                "weekly_avg_1y_lgbm_0529",
                "shared.weekly_average_source_evidence."
                "source_package_tree_sha256",
            ),
        )
        for loader, scheme_id, digest_target in cases:
            with (
                self.subTest(scheme_id=scheme_id),
                patch(digest_target, return_value="f" * 64),
                self.assertRaisesRegex(
                    RuntimeError,
                    "manifest source_package_sha256",
                ),
            ):
                loader(scheme_id)

    def test_database_config_path_is_limited_to_archived_source_runners(
        self,
    ) -> None:
        from shared.source_runtime_database import (
            SOURCE_RUNTIME_SCHEME_IDS,
        )

        self.assertEqual(
            SOURCE_RUNTIME_SCHEME_IDS,
            {
                "daily_1y_xgb_1y13_0629",
                "daily_5y_lgbm_5y10_0629",
                "daily_10y_lgbm_10y04_0629",
                "monthly_1y_rf_top30_0629",
                "monthly_5y_knn_top20_0629",
                "monthly_10y_rf_top5_0629",
                "weekly_avg_1y_lgbm_0529",
                "weekly_avg_5y_lgbm_0529",
                "weekly_avg_10y_lgbm_0529",
            },
        )

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

    def test_source_package_rejects_process_escape_primitives(
        self,
    ) -> None:
        from shared.source_runtime_database import (
            assert_source_package_tree_safe,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            source_root = Path(tmpdir)
            (source_root / "escape.py").write_text(
                "import os\nos.setsid()\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "process isolation",
            ):
                assert_source_package_tree_safe(source_root)

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

    def test_database_binding_identity_and_repr_never_contain_password(
        self,
    ) -> None:
        from shared.source_runtime_database import (
            load_source_runtime_database_config,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            binding_path = Path(tmpdir) / "source-db.json"
            _write_private_binding(binding_path)
            config = load_source_runtime_database_config(
                {
                    "BFL_SOURCE_DB_CONFIG_ROOT":
                        str(binding_path.parent.resolve()),
                    "BFL_SOURCE_DB_CONFIG_PATH":
                        str(binding_path.resolve()),
                }
            )

        secret = str(SOURCE_DATABASE_CONFIG["password"])
        self.assertNotIn(secret, repr(config))
        self.assertNotIn(secret, config.cache_identity)

    def test_cache_payload_check_handles_escaped_password_characters(
        self,
    ) -> None:
        from shared.source_runtime_database import (
            SourceRuntimeDatabaseConfig,
            assert_source_runtime_payload_safe,
        )

        password = 'quote"and\\slash-secret'
        config = SourceRuntimeDatabaseConfig(
            user="source_reader",
            password=password,
            host="127.0.0.1",
            port=43306,
            database="bfl_source_test",
            charset="utf8mb4",
            config_path=Path("/private/source-db.json"),
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "contains database credentials",
        ):
            assert_source_runtime_payload_safe(
                {"nested": [{"password": password}]},
                config,
            )

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

    def test_all_source_runners_bound_and_redact_timeouts(
        self,
    ) -> None:
        from shared.daily_0629_source_runner import _run_source_command
        from shared.monthly_source_runner import _run_monthly_module
        from shared.weekly_average_lgbm_source_runner import (
            _run_python_module,
        )

        secret = '密钥"\\source-secret'
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            binding_path = root / "source-db.json"
            payload = dict(SOURCE_DATABASE_CONFIG)
            payload["password"] = secret
            binding_path.write_text(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            binding_path.chmod(0o600)
            environment = {
                "BFL_SOURCE_DB_CONFIG_ROOT":
                    str(binding_path.parent.resolve()),
                "BFL_SOURCE_DB_CONFIG_PATH":
                    str(binding_path.resolve()),
                "DAILY_0629_SOURCE_TIMEOUT_SEC": "7",
                "MONTHLY_SOURCE_TIMEOUT_SEC": "7",
                "WEEKLY_AVERAGE_SOURCE_TIMEOUT_SEC": "7",
            }
            cases = (
                (
                    lambda: _run_source_command(["daily"], root),
                    (
                        "shared.daily_0629_source_runner."
                        "run_source_subprocess"
                    ),
                ),
                (
                    lambda: _run_monthly_module(
                        "monthly.module",
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
                        "weekly.module",
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
                timeout = subprocess.TimeoutExpired(
                    cmd=["source"],
                    timeout=7,
                    output=f"stdout {secret}".encode("utf-8"),
                    stderr=f"stderr {secret}".encode("utf-8"),
                )
                with (
                    self.subTest(case=case),
                    patch.dict(
                        os.environ,
                        environment,
                        clear=True,
                    ),
                    patch(
                        patch_target,
                        side_effect=timeout,
                    ) as run,
                    self.assertRaisesRegex(
                        RuntimeError,
                        "timed out",
                    ) as raised,
                ):
                    case()
                self.assertNotIn(secret, str(raised.exception))
                self.assertIn("<redacted>", str(raised.exception))
                self.assertEqual(
                    run.call_args.kwargs["timeout"],
                    7,
                )

    def test_source_subprocess_timeout_kills_descendants(
        self,
    ) -> None:
        from shared.source_runtime_database import (
            run_source_subprocess,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            child_pid_path = root / "child.pid"
            child_script = (
                "import signal,time;"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                "time.sleep(60)"
            )
            parent_script = (
                "import pathlib,subprocess,sys,time;"
                f"child=subprocess.Popen([sys.executable,'-c',{child_script!r}]);"
                f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid));"
                "time.sleep(60)"
            )
            child_pid: int | None = None
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    run_source_subprocess(
                        [sys.executable, "-c", parent_script],
                        cwd=root,
                        env={"PATH": os.environ.get("PATH", "")},
                        timeout=1,
                    )
                child_pid = int(
                    child_pid_path.read_text(encoding="utf-8")
                )
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.02)
                else:
                    self.fail(
                        "source subprocess descendant survived timeout"
                    )
            finally:
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_source_subprocess_dies_when_runner_parent_is_killed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            child_pid_path = root / "child.pid"
            child_script = (
                "import pathlib,signal,time;"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                f"pathlib.Path({str(child_pid_path)!r}).write_text(str(__import__('os').getpid()));"
                "time.sleep(60)"
            )
            parent_script = (
                "import os,sys;"
                "from pathlib import Path;"
                "from shared.source_runtime_database import "
                "run_source_subprocess;"
                f"run_source_subprocess([sys.executable,'-c',{child_script!r}],"
                f"cwd=Path({str(root)!r}),env=dict(os.environ),timeout=60)"
            )
            environment = dict(os.environ)
            project_root = Path(__file__).resolve().parents[1]
            environment["PYTHONPATH"] = os.pathsep.join(
                [
                    str(project_root),
                    environment.get("PYTHONPATH", ""),
                ]
            )
            parent = subprocess.Popen(
                [sys.executable, "-c", parent_script],
                cwd=project_root,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            child_pid: int | None = None
            try:
                deadline = time.monotonic() + 3
                while (
                    time.monotonic() < deadline
                    and not child_pid_path.exists()
                ):
                    time.sleep(0.02)
                self.assertTrue(
                    child_pid_path.exists(),
                    "source child did not start",
                )
                child_pid = int(
                    child_pid_path.read_text(encoding="utf-8")
                )
                parent.kill()
                parent.wait(timeout=2)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.02)
                else:
                    self.fail(
                        "source child survived runner parent death"
                    )
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=2)
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_source_subprocess_never_uses_unbounded_final_drain(
        self,
    ) -> None:
        from shared.source_runtime_database import (
            run_source_subprocess,
        )

        class FakePipe:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        class FakeProcess:
            pid = 77123
            returncode = -signal.SIGKILL

            def __init__(self) -> None:
                self.stdout = FakePipe()
                self.stderr = FakePipe()
                self.communicate_calls = 0

            def communicate(
                self,
                timeout: float,
            ) -> tuple[str, str]:
                self.communicate_calls += 1
                if self.communicate_calls <= 2:
                    raise subprocess.TimeoutExpired(
                        cmd=["source"],
                        timeout=timeout,
                        output=b"partial stdout",
                        stderr=b"partial stderr",
                    )
                raise AssertionError(
                    "source output drain became unbounded"
                )

            def wait(self, timeout: float) -> int:
                return self.returncode

        process = FakeProcess()
        with (
            patch(
                "shared.source_runtime_database.subprocess.Popen",
                return_value=process,
            ),
            patch(
                "shared.source_runtime_database."
                "_terminate_source_process_group",
            ),
            patch(
                "shared.source_runtime_database."
                "_signal_source_process_group",
            ),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            run_source_subprocess(
                ["source"],
                cwd=Path("/private"),
                env={},
                timeout=1,
            )
        self.assertEqual(process.communicate_calls, 2)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_source_process_group_cleanup_must_be_confirmed(
        self,
    ) -> None:
        from shared.source_runtime_database import (
            _terminate_source_process_group,
        )

        class FakeProcess:
            pid = 77124

            @staticmethod
            def poll() -> None:
                return None

            @staticmethod
            def wait(timeout: float) -> int:
                raise subprocess.TimeoutExpired(
                    cmd=["source"],
                    timeout=timeout,
                )

        with (
            patch(
                "shared.source_runtime_database."
                "_SOURCE_PROCESS_TERMINATE_GRACE_SECONDS",
                0,
            ),
            patch(
                "shared.source_runtime_database."
                "_source_process_group_exists",
                return_value=True,
            ),
            patch(
                "shared.source_runtime_database."
                "_signal_source_process_group",
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "cleanup could not be confirmed",
            ),
        ):
            _terminate_source_process_group(FakeProcess())

    def test_binding_path_must_be_absolute_regular_owner_only_file(
        self,
    ) -> None:
        from shared.source_runtime_database import (
            load_source_runtime_database_config,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            binding_path = root / "source-db.json"
            _write_private_binding(binding_path)
            binding_path.chmod(0o640)
            with (
                patch.dict(
                    os.environ,
                    {
                        "BFL_SOURCE_DB_CONFIG_ROOT":
                            str(root.resolve()),
                        "BFL_SOURCE_DB_CONFIG_PATH":
                            str(binding_path),
                    },
                    clear=True,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "owner-only mode 0600",
                ),
            ):
                load_source_runtime_database_config()

            binding_path.chmod(0o600)
            link_path = root / "source-db-link.json"
            link_path.symlink_to(binding_path)
            with (
                patch.dict(
                    os.environ,
                    {
                        "BFL_SOURCE_DB_CONFIG_ROOT":
                            str(root.resolve()),
                        "BFL_SOURCE_DB_CONFIG_PATH":
                            str(link_path.resolve().parent / link_path.name),
                    },
                    clear=True,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "regular non-symlink",
                ),
            ):
                load_source_runtime_database_config()

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

    def test_approved_private_root_rejects_unsafe_ancestry(
        self,
    ) -> None:
        from shared.source_runtime_database import (
            load_source_runtime_database_config,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            unsafe_parent = root / "unsafe"
            unsafe_parent.mkdir(mode=0o700)
            approved_root = unsafe_parent / "private"
            approved_root.mkdir(mode=0o700)
            binding_path = approved_root / "source-db.json"
            _write_private_binding(binding_path)

            cases = (
                (
                    "non-private root",
                    lambda: approved_root.chmod(0o755),
                    "owner-only mode 0700",
                ),
                (
                    "writable parent",
                    lambda: unsafe_parent.chmod(0o777),
                    "ancestry is unsafe",
                ),
            )
            for label, make_unsafe, message in cases:
                approved_root.chmod(0o700)
                unsafe_parent.chmod(0o700)
                make_unsafe()
                with (
                    self.subTest(case=label),
                    self.assertRaisesRegex(RuntimeError, message),
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

            approved_root.chmod(0o700)
            unsafe_parent.chmod(0o700)
            real_root = root / "real-private"
            real_root.mkdir(mode=0o700)
            real_binding = real_root / "source-db.json"
            _write_private_binding(real_binding)
            linked_root = root / "linked-private"
            linked_root.symlink_to(real_root, target_is_directory=True)
            with (
                self.assertRaisesRegex(RuntimeError, "non-symlink"),
                patch.dict(
                    os.environ,
                    {
                        "BFL_SOURCE_DB_CONFIG_ROOT":
                            str(linked_root),
                        "BFL_SOURCE_DB_CONFIG_PATH":
                            str(linked_root / "source-db.json"),
                    },
                    clear=True,
                ),
            ):
                load_source_runtime_database_config()

    def test_private_json_atomic_writer_has_no_shared_temp_name(
        self,
    ) -> None:
        from shared.source_runtime_database import (
            write_private_json_atomic,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.json"
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        write_private_json_atomic,
                        path,
                        {"writer": writer},
                    )
                    for writer in (1, 2)
                ]
                for future in futures:
                    future.result(timeout=5)

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn(payload["writer"], {1, 2})
            self.assertEqual(
                stat.S_IMODE(path.stat().st_mode),
                0o600,
            )
            self.assertEqual(
                list(path.parent.glob("*.tmp")),
                [],
            )


if __name__ == "__main__":
    unittest.main()
