from __future__ import annotations

import os
import inspect
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


class DailyRealReplayMySQLLifecycleTests(unittest.TestCase):
    def test_forced_cold_cache_root_is_empty_owner_only_and_single_use(
        self,
    ) -> None:
        from harness.daily_real_replay_mysql import (
            IsolatedReplayMySQL,
            IsolatedReplayMySQLError,
            _ReplayMySQLPaths,
        )

        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            parent.chmod(0o700)
            root = parent / (
                "bfl-real-replay-mysql-0123456789abcdef0123"
            )
            root.mkdir(mode=0o700)
            server = IsolatedReplayMySQL(root_parent=parent)
            server._paths = _ReplayMySQLPaths.from_root(root)
            details = root.lstat()
            server._root_identity = (
                details.st_dev,
                details.st_ino,
            )

            cache_root = server.create_forced_cold_cache_root()

            cache_details = cache_root.lstat()
            self.assertEqual(cache_root.parent, root)
            self.assertTrue(stat.S_ISDIR(cache_details.st_mode))
            self.assertFalse(stat.S_ISLNK(cache_details.st_mode))
            self.assertEqual(cache_details.st_uid, os.getuid())
            self.assertEqual(stat.S_IMODE(cache_details.st_mode), 0o700)
            self.assertEqual(tuple(cache_root.iterdir()), ())
            with self.assertRaises(
                IsolatedReplayMySQLError
            ) as raised:
                server.create_forced_cold_cache_root()
            self.assertEqual(
                raised.exception.code,
                "MYSQL_FORCED_COLD_CACHE_UNSAFE",
            )

    def test_forced_cold_cache_root_rejects_symlink(self) -> None:
        from harness.daily_real_replay_mysql import (
            IsolatedReplayMySQL,
            IsolatedReplayMySQLError,
            _ReplayMySQLPaths,
        )

        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            parent.chmod(0o700)
            root = parent / (
                "bfl-real-replay-mysql-0123456789abcdef0123"
            )
            root.mkdir(mode=0o700)
            external = parent / "external-cache"
            external.mkdir()
            (root / "liwei-phase-a-cache").symlink_to(
                external,
                target_is_directory=True,
            )
            server = IsolatedReplayMySQL(root_parent=parent)
            server._paths = _ReplayMySQLPaths.from_root(root)
            details = root.lstat()
            server._root_identity = (
                details.st_dev,
                details.st_ino,
            )

            with self.assertRaises(
                IsolatedReplayMySQLError
            ) as raised:
                server.create_forced_cold_cache_root()

            self.assertEqual(
                raised.exception.code,
                "MYSQL_FORCED_COLD_CACHE_UNSAFE",
            )
            self.assertTrue(external.is_dir())

    def test_server_command_is_loopback_private_and_non_production(
        self,
    ) -> None:
        from harness.daily_real_replay_mysql import (
            _ReplayMySQLPaths,
            _build_server_command,
        )

        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            root = parent / "bfl-real-replay-mysql-unit"
            paths = _ReplayMySQLPaths.from_root(root)
            command = _build_server_command(
                paths=paths,
                port=13306,
            )

        joined = "\n".join(command)
        self.assertIn("--bind-address=127.0.0.1", command)
        self.assertIn("--port=13306", command)
        self.assertIn("--skip-log-bin", command)
        self.assertIn("--local-infile=OFF", command)
        self.assertIn("--mysqlx=OFF", command)
        self.assertIn("--skip-symbolic-links", command)
        self.assertIn("--default-time-zone=+00:00", command)
        self.assertIn("--transaction-isolation=REPEATABLE-READ", command)
        self.assertIn(str(paths.datadir), joined)
        self.assertIn(str(paths.socket_path), joined)
        self.assertNotIn("bond_db", joined)
        self.assertNotIn("password", joined.casefold())
        self.assertNotIn("--port=3306", command)

    def test_server_command_rejects_production_or_invalid_port(
        self,
    ) -> None:
        from harness.daily_real_replay_mysql import (
            IsolatedReplayMySQLError,
            _ReplayMySQLPaths,
            _build_server_command,
        )

        paths = _ReplayMySQLPaths.from_root(
            Path(
                "/private/tmp/"
                "bfl-real-replay-mysql-0123456789abcdef0123"
            )
        )
        for port in (0, 3306, 65536):
            with self.subTest(port=port):
                with self.assertRaises(
                    IsolatedReplayMySQLError
                ) as raised:
                    _build_server_command(paths=paths, port=port)
                self.assertEqual(
                    raised.exception.code,
                    "MYSQL_PORT_UNSAFE",
                )

    def test_lifecycle_has_no_reverse_dependency_on_scripts(self) -> None:
        import harness.daily_real_replay_mysql as lifecycle

        source = inspect.getsource(lifecycle)
        self.assertNotIn("from scripts", source)
        self.assertNotIn("import scripts", source)
        self.assertNotIn("create_engine_from_env", source)

    def test_cleanup_refuses_replaced_root_without_deleting_it(
        self,
    ) -> None:
        from harness.daily_real_replay_mysql import (
            IsolatedReplayMySQLError,
            _remove_verified_root,
        )

        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            root = (
                parent
                / "bfl-real-replay-mysql-0123456789abcdef0123"
            )
            root.mkdir(mode=0o700)
            details = root.stat()
            identity = (details.st_dev, details.st_ino)
            original = root.with_name(root.name + "-original")
            root.rename(original)
            root.mkdir(mode=0o700)

            with self.assertRaises(
                IsolatedReplayMySQLError
            ) as raised:
                _remove_verified_root(
                    root,
                    root_parent=parent,
                    expected_identity=identity,
                    process=None,
                )

            self.assertTrue(root.is_dir())
            self.assertTrue(original.is_dir())

        self.assertEqual(raised.exception.code, "MYSQL_CLEANUP_UNSAFE")

    def test_live_process_prevents_directory_cleanup(self) -> None:
        from harness.daily_real_replay_mysql import (
            IsolatedReplayMySQLError,
            _remove_verified_root,
        )

        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            root = (
                parent
                / "bfl-real-replay-mysql-fedcba98765432100123"
            )
            root.mkdir(mode=0o700)
            details = root.stat()
            process = Mock()
            process.poll.return_value = None

            with self.assertRaises(
                IsolatedReplayMySQLError
            ) as raised:
                _remove_verified_root(
                    root,
                    root_parent=parent,
                    expected_identity=(
                        details.st_dev,
                        details.st_ino,
                    ),
                    process=process,
                )

            self.assertTrue(root.is_dir())

        self.assertEqual(
            raised.exception.code,
            "MYSQL_CLEANUP_INCOMPLETE",
        )

    def test_cleanup_never_deletes_directory_swapped_after_check(
        self,
    ) -> None:
        from harness.daily_real_replay_mysql import (
            IsolatedReplayMySQLError,
            _remove_verified_root,
        )

        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            root = (
                parent
                / "bfl-real-replay-mysql-11112222333344445555"
            )
            root.mkdir(mode=0o700)
            (root / "owned").write_text("owned", encoding="utf-8")
            details = root.stat()
            original = parent / "original-root"
            victim = parent / "victim-root"
            victim.mkdir(mode=0o700)
            sentinel = victim / "must-survive"
            sentinel.write_text("sentinel", encoding="utf-8")
            real_clear = None
            swapped = False

            def swap_before_clear(directory_fd):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    os.rename(root, original)
                    os.rename(victim, root)
                assert real_clear is not None
                return real_clear(directory_fd)

            from harness import daily_real_replay_mysql as lifecycle

            real_clear = lifecycle._clear_directory_fd

            with (
                patch(
                    "harness.daily_real_replay_mysql."
                    "_clear_directory_fd",
                    side_effect=swap_before_clear,
                ),
                self.assertRaises(
                    IsolatedReplayMySQLError
                ) as raised,
            ):
                _remove_verified_root(
                    root,
                    root_parent=parent,
                    expected_identity=(
                        details.st_dev,
                        details.st_ino,
                    ),
                    process=None,
                )

            surviving = root / "must-survive"
            self.assertTrue(surviving.is_file())
            self.assertEqual(
                surviving.read_text(encoding="utf-8"),
                "sentinel",
            )
            self.assertEqual(
                tuple(
                    parent.glob(
                        ".bfl-real-replay-mysql-cleanup-*"
                    )
                ),
                (),
            )

        self.assertEqual(
            raised.exception.code,
            "MYSQL_CLEANUP_UNSAFE",
        )

    def test_start_failure_removes_initialized_private_root(
        self,
    ) -> None:
        from harness.daily_real_replay_mysql import (
            IsolatedReplayMySQL,
            IsolatedReplayMySQLError,
        )

        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            server = IsolatedReplayMySQL(root_parent=parent)
            with (
                patch.object(server, "_validate_binaries"),
                patch(
                    "harness.daily_real_replay_mysql."
                    "_reserve_loopback_port",
                    return_value=13306,
                ),
                patch.object(
                    server,
                    "_initialize_datadir",
                    side_effect=IsolatedReplayMySQLError(
                        "MYSQL_INITIALIZE_FAILED"
                    ),
                ),
                self.assertRaises(
                    IsolatedReplayMySQLError
                ) as raised,
            ):
                server._start()

            self.assertEqual(tuple(parent.iterdir()), ())

        self.assertEqual(
            raised.exception.code,
            "MYSQL_INITIALIZE_FAILED",
        )

    def test_cleanup_failure_supersedes_and_chains_start_failure(
        self,
    ) -> None:
        from harness.daily_real_replay_mysql import (
            IsolatedReplayMySQL,
            IsolatedReplayMySQLError,
        )

        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            server = IsolatedReplayMySQL(root_parent=parent)
            process = Mock()
            process.poll.return_value = None
            with (
                patch.object(server, "_validate_binaries"),
                patch(
                    "harness.daily_real_replay_mysql."
                    "_reserve_loopback_port",
                    return_value=13306,
                ),
                patch.object(server, "_initialize_datadir"),
                patch.object(server, "_read_expected_server_uuid"),
                patch.object(server, "_write_private_credentials"),
                patch(
                    "harness.daily_real_replay_mysql.subprocess.Popen",
                    return_value=process,
                ),
                patch.object(
                    server,
                    "_wait_until_ready",
                    side_effect=IsolatedReplayMySQLError(
                        "MYSQL_START_FAILED"
                    ),
                ),
                patch.object(
                    server,
                    "_stop_process",
                    side_effect=IsolatedReplayMySQLError(
                        "MYSQL_CLEANUP_INCOMPLETE"
                    ),
                ),
                self.assertRaises(
                    IsolatedReplayMySQLError
                ) as raised,
            ):
                server._start()

            self.assertTrue(server.root.is_dir())

        self.assertEqual(
            raised.exception.code,
            "MYSQL_CLEANUP_INCOMPLETE",
        )
        self.assertIsInstance(
            raised.exception.__cause__,
            IsolatedReplayMySQLError,
        )
        self.assertEqual(
            raised.exception.__cause__.code,
            "MYSQL_START_FAILED",
        )

    def test_close_disposes_engine_before_process_and_root(
        self,
    ) -> None:
        from harness.daily_real_replay_mysql import (
            IsolatedReplayMySQL,
            _ReplayMySQLPaths,
        )

        events: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            root = (
                parent
                / "bfl-real-replay-mysql-aabbccddeeff00112233"
            )
            root.mkdir(mode=0o700)
            details = root.stat()
            process = Mock()
            process.poll.return_value = 0
            engine = Mock()
            engine.dispose.side_effect = lambda: events.append(
                "engine"
            )
            server = IsolatedReplayMySQL(root_parent=parent)
            server._paths = _ReplayMySQLPaths.from_root(root)
            server._root_identity = (
                details.st_dev,
                details.st_ino,
            )
            server._process = process
            server._engines.append(engine)
            with (
                patch.object(
                    server,
                    "_stop_process",
                    side_effect=lambda _process: events.append(
                        "process"
                    ),
                ),
                patch(
                    "harness.daily_real_replay_mysql."
                    "_remove_verified_root",
                    side_effect=lambda *_args, **_kwargs: (
                        events.append("root")
                    ),
                ),
            ):
                server._close()

        self.assertEqual(events, ["engine", "process", "root"])

    def test_dispose_failure_still_stops_process_and_cleans_root(
        self,
    ) -> None:
        from harness.daily_real_replay_mysql import (
            IsolatedReplayMySQL,
            IsolatedReplayMySQLError,
            _ReplayMySQLPaths,
        )

        events: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            root = (
                parent
                / "bfl-real-replay-mysql-abcdefabcdefabcdefab"
            )
            root.mkdir(mode=0o700)
            details = root.stat()
            process = Mock()
            process.poll.return_value = 0
            engine = Mock()

            def fail_dispose():
                events.append("engine")
                raise RuntimeError("dispose-secret")

            engine.dispose.side_effect = fail_dispose
            server = IsolatedReplayMySQL(root_parent=parent)
            server._paths = _ReplayMySQLPaths.from_root(root)
            server._root_identity = (
                details.st_dev,
                details.st_ino,
            )
            server._process = process
            server._engines.append(engine)
            with (
                patch.object(
                    server,
                    "_stop_process",
                    side_effect=lambda _process: events.append(
                        "process"
                    ),
                ),
                patch(
                    "harness.daily_real_replay_mysql."
                    "_remove_verified_root",
                    side_effect=lambda *_args, **_kwargs: (
                        events.append("root")
                    ),
                ),
                self.assertRaises(
                    IsolatedReplayMySQLError
                ) as raised,
            ):
                server._close()

        self.assertEqual(events, ["engine", "process", "root"])
        self.assertEqual(
            raised.exception.code,
            "MYSQL_CLEANUP_INCOMPLETE",
        )
        self.assertNotIn("dispose-secret", repr(raised.exception))

    def test_database_creation_failure_never_exposes_password(
        self,
    ) -> None:
        from harness.daily_real_replay_mysql import (
            IsolatedReplayMySQL,
            IsolatedReplayMySQLError,
            IsolatedReplayMySQLIdentity,
        )

        secret = "b" * 32 + "c" * 32
        connection = Mock()
        count_result = Mock()
        count_result.scalar_one.return_value = 0
        connection.execute.return_value = count_result
        connection.exec_driver_sql.side_effect = (
            None,
            RuntimeError(secret),
        )
        transaction = Mock()
        transaction.__enter__ = Mock(return_value=connection)
        transaction.__exit__ = Mock(return_value=False)
        admin = Mock()
        admin.begin.return_value = transaction

        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            server = IsolatedReplayMySQL(root_parent=parent)
            server._port = 13306
            server._identity = IsolatedReplayMySQLIdentity(
                version="8.0.45",
                server_uuid=(
                    "11111111-1111-1111-1111-111111111111"
                ),
                port=13306,
                socket_path=parent / "mysql.sock",
                datadir=parent / "data",
            )
            with (
                patch.object(
                    server,
                    "_admin_engine",
                    return_value=admin,
                ),
                patch(
                    "harness.daily_real_replay_mysql.uuid.uuid4",
                    side_effect=(
                        SimpleNamespace(hex="a" * 32),
                        SimpleNamespace(hex="b" * 32),
                        SimpleNamespace(hex="c" * 32),
                    ),
                ),
                self.assertRaises(
                    IsolatedReplayMySQLError
                ) as raised,
            ):
                server.create_replay_database()

        self.assertEqual(
            raised.exception.code,
            "MYSQL_DATABASE_CREATE_FAILED",
        )
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, repr(raised.exception))
        self.assertTrue(
            raised.exception.__cause__ is None
            or secret not in repr(raised.exception.__cause__)
        )


@unittest.skipUnless(
    os.getenv("BFL_REAL_REPLAY_MYSQL_LIFECYCLE") == "1",
    "set BFL_REAL_REPLAY_MYSQL_LIFECYCLE=1 for isolated MySQL",
)
class DailyRealReplayMySQLLifecycleIntegrationTests(
    unittest.TestCase
):
    def test_starts_verified_schema_and_removes_exact_root(self) -> None:
        from sqlalchemy import text

        from harness.daily_real_replay_mysql import (
            IsolatedReplayMySQLError,
            isolated_replay_mysql,
        )
        from shared.daily_coordinator_mode import (
            recheck_verified_isolated_daily_database,
        )

        retained_root: Path | None = None
        process = None
        with isolated_replay_mysql() as server:
            retained_root = server.root
            process = server.process
            self.assertIsNotNone(process)
            self.assertNotEqual(server.port, 3306)
            self.assertEqual(
                server.identity.server_uuid,
                server.expected_server_uuid,
            )
            schema, engine = server.create_replay_database()
            isolation = server.database_isolation
            recheck_verified_isolated_daily_database(
                engine,
                isolation,
            )
            self.assertRegex(
                schema,
                r"^bfl_real_replay_[0-9a-f]{20}$",
            )
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        """
                        SELECT DATABASE() AS database_name,
                               @@server_uuid AS server_uuid,
                               @@port AS server_port
                        """
                    )
                ).mappings().one()
            self.assertEqual(row["database_name"], schema)
            self.assertEqual(
                str(row["server_uuid"]),
                server.identity.server_uuid,
            )
            self.assertEqual(int(row["server_port"]), server.port)
            with self.assertRaises(
                IsolatedReplayMySQLError
            ) as raised:
                server.create_replay_database()
            self.assertEqual(
                raised.exception.code,
                "MYSQL_DATABASE_ALREADY_CREATED",
            )

        self.assertIsNotNone(retained_root)
        self.assertFalse(retained_root.exists())
        self.assertIsNotNone(process)
        self.assertIsNotNone(process.poll())

    def test_body_failure_still_stops_and_removes_exact_root(
        self,
    ) -> None:
        from harness.daily_real_replay_mysql import (
            isolated_replay_mysql,
        )

        retained_root: Path | None = None
        process = None
        with self.assertRaisesRegex(RuntimeError, "body-failed"):
            with isolated_replay_mysql() as server:
                retained_root = server.root
                process = server.process
                raise RuntimeError("body-failed")

        self.assertIsNotNone(retained_root)
        self.assertFalse(retained_root.exists())
        self.assertIsNotNone(process)
        self.assertIsNotNone(process.poll())
