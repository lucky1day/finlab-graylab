from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sqlalchemy import text

from harness.daily_real_replay_mysql import isolated_replay_mysql
from migrations import runner as migration_runner


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = tuple(
    sorted((PROJECT_ROOT / "migrations").glob("*.sql"))
)


class _BreakBefore017DDL(RuntimeError):
    pass


class _BreakAfter017DDL(RuntimeError):
    pass


class _BreakBefore018DDL(RuntimeError):
    pass


class _BreakAfter018DDL(RuntimeError):
    pass


def _release_manifest():
    return migration_runner.validate_release_migration_manifest(
        MIGRATIONS
    )


def _execute_legacy_016_without_history(engine) -> None:
    """执行受 release manifest 校验的 001..016 schema。"""
    manifest = _release_manifest()
    migration_runner._execute_prepared_migration_files(
        engine,
        [
            (migration.path, migration.statements)
            for migration in manifest
            if migration.version <= 16
        ],
    )


def _apply_until_before_017_ddl(engine) -> None:
    """通过公开 runner 在 history=APPLYING 后、017 DDL 前中断。"""
    _execute_legacy_016_without_history(engine)
    original_execute = (
        migration_runner._execute_prepared_migration_files
    )

    def break_before_017(candidate_engine, prepared):
        prepared_files = [
            (path, tuple(statements))
            for path, statements in prepared
        ]
        if any(
            path.name.startswith("017_")
            for path, _ in prepared_files
        ):
            raise _BreakBefore017DDL(
                "break before migration 017 DDL"
            )
        return original_execute(candidate_engine, prepared_files)

    with (
        patch.object(
            migration_runner,
            "_execute_prepared_migration_files",
            side_effect=break_before_017,
        ),
        unittest.TestCase().assertRaises(_BreakBefore017DDL),
    ):
        migration_runner.apply_migration_files(engine, MIGRATIONS)


def _apply_until_after_017_ddl(engine) -> None:
    """通过公开 runner 在 017 DDL 完成、history mark 前中断。"""
    _execute_legacy_016_without_history(engine)
    original_mark = migration_runner._mark_migration_applied

    def break_after_017(candidate_engine, migration):
        if migration.version == 17:
            raise _BreakAfter017DDL(
                "break after migration 017 DDL before history mark"
            )
        return original_mark(candidate_engine, migration)

    with (
        patch.object(
            migration_runner,
            "_mark_migration_applied",
            side_effect=break_after_017,
        ),
        unittest.TestCase().assertRaises(_BreakAfter017DDL),
    ):
        migration_runner.apply_migration_files(engine, MIGRATIONS)


def _apply_until_mid_017_ddl(engine) -> None:
    """真实提交前两个 017 DDL，再由 definition postcondition 拒绝。"""
    _execute_legacy_016_without_history(engine)
    original_execute = (
        migration_runner._execute_prepared_migration_files
    )

    def break_mid_017(candidate_engine, prepared):
        prepared_files = [
            (path, tuple(statements))
            for path, statements in prepared
        ]
        if len(prepared_files) != 1:
            return original_execute(candidate_engine, prepared_files)
        path, statements = prepared_files[0]
        if not path.name.startswith("017_"):
            return original_execute(candidate_engine, prepared_files)
        ddl_indexes = [
            index
            for index, statement in enumerate(statements)
            if migration_runner._is_ddl_statement(statement)
        ]
        if len(ddl_indexes) < 2:
            raise AssertionError(
                "migration 017 must expose at least two DDL statements"
            )
        return original_execute(
            candidate_engine,
            [(path, statements[: ddl_indexes[1] + 1])],
        )

    with (
        patch.object(
            migration_runner,
            "_execute_prepared_migration_files",
            side_effect=break_mid_017,
        ),
        unittest.TestCase().assertRaises(
            migration_runner.MigrationPartialApplyError
        ),
    ):
        migration_runner.apply_migration_files(engine, MIGRATIONS)


def _apply_until_before_018_ddl(engine) -> None:
    """通过公开 runner 让 018 history=APPLYING 且尚未执行 DDL。"""
    _execute_legacy_016_without_history(engine)
    original_execute = (
        migration_runner._execute_prepared_migration_files
    )

    def break_before_018(candidate_engine, prepared):
        prepared_files = [
            (path, tuple(statements))
            for path, statements in prepared
        ]
        if any(
            path.name.startswith("018_")
            for path, _ in prepared_files
        ):
            raise _BreakBefore018DDL(
                "break before migration 018 DDL"
            )
        return original_execute(candidate_engine, prepared_files)

    with (
        patch.object(
            migration_runner,
            "_execute_prepared_migration_files",
            side_effect=break_before_018,
        ),
        unittest.TestCase().assertRaises(_BreakBefore018DDL),
    ):
        migration_runner.apply_migration_files(engine, MIGRATIONS)


def _apply_until_after_018_ddl(engine) -> None:
    """通过公开 runner 在 018 DDL 完成、history mark 前中断。"""
    _execute_legacy_016_without_history(engine)
    original_mark = migration_runner._mark_migration_applied

    def break_after_018(candidate_engine, migration):
        if migration.version == 18:
            raise _BreakAfter018DDL(
                "break after migration 018 DDL before history mark"
            )
        return original_mark(candidate_engine, migration)

    with (
        patch.object(
            migration_runner,
            "_mark_migration_applied",
            side_effect=break_after_018,
        ),
        unittest.TestCase().assertRaises(_BreakAfter018DDL),
    ):
        migration_runner.apply_migration_files(engine, MIGRATIONS)


def _history_rows(engine):
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    """
                    SELECT version AS version,
                           filename AS filename,
                           sha256 AS sha256,
                           state AS state,
                           baseline_bootstrap AS baseline_bootstrap,
                           applied_at AS applied_at
                    FROM t_schema_migrations
                    ORDER BY version
                    """
                )
            ).mappings().all()
        )


def _assert_exact_applying_017_history(
    testcase: unittest.TestCase,
    engine,
) -> None:
    manifest = _release_manifest()
    rows = _history_rows(engine)
    testcase.assertEqual(
        list(range(1, 18)),
        [int(row["version"]) for row in rows],
    )
    for migration, row in zip(manifest[:17], rows):
        testcase.assertEqual(migration.path.name, row["filename"])
        testcase.assertEqual(migration.sha256, row["sha256"])
        if migration.version < 17:
            testcase.assertEqual(
                1,
                int(row["baseline_bootstrap"]),
            )
            testcase.assertEqual("APPLIED", row["state"])
            testcase.assertIsNotNone(row["applied_at"])
        else:
            testcase.assertEqual(
                0,
                int(row["baseline_bootstrap"]),
            )
            testcase.assertEqual("APPLYING", row["state"])
            testcase.assertIsNone(row["applied_at"])


def _assert_017_applied(
    testcase: unittest.TestCase,
    engine,
) -> None:
    rows = _history_rows(engine)
    testcase.assertEqual(17, len(rows))
    testcase.assertEqual("APPLIED", rows[-1]["state"])
    testcase.assertEqual(
        0,
        int(rows[-1]["baseline_bootstrap"]),
    )
    testcase.assertIsNotNone(rows[-1]["applied_at"])


def _assert_no_017_business_ddl(
    testcase: unittest.TestCase,
    engine,
) -> None:
    """证明 namespace preflight 在首个 017 业务 DDL 前拒绝。"""
    with engine.connect() as connection:
        shape = connection.execute(
            text(
                """
                SELECT
                    (SELECT COUNT(*)
                     FROM information_schema.tables
                     WHERE table_schema = DATABASE()
                       AND table_name IN (
                           't_input_generations',
                           't_schedule_occurrences',
                           't_schedule_items',
                           't_schedule_item_targets',
                           't_scheduler_heartbeat'
                       )) AS ledger_tables,
                    (SELECT COUNT(*)
                     FROM information_schema.columns
                     WHERE table_schema = DATABASE()
                       AND table_name = 't_scheme_runs'
                       AND column_name = 'schedule_item_id')
                        AS schedule_item_column,
                    (SELECT COUNT(DISTINCT index_name)
                     FROM information_schema.statistics
                     WHERE table_schema = DATABASE()
                       AND table_name = 't_scheme_runs'
                       AND index_name =
                           'uk_scheme_runs_schedule_attempt')
                        AS schedule_attempt_index
                """
            )
        ).mappings().one()
    testcase.assertEqual(0, int(shape["ledger_tables"]))
    testcase.assertEqual(
        0,
        int(shape["schedule_item_column"]),
    )
    testcase.assertEqual(
        0,
        int(shape["schedule_attempt_index"]),
    )


def _inspect_017(engine):
    return migration_runner.inspect_applying_migration_017(
        engine,
        MIGRATIONS,
    )


def _recover_017(engine, inspection):
    return migration_runner.recover_applying_migration_017(
        engine,
        MIGRATIONS,
        expected_state_digest=str(inspection["state_digest"]),
    )


def _assert_mysql_cleaned(
    testcase: unittest.TestCase,
    *,
    retained_root,
    process,
) -> None:
    testcase.assertIsNotNone(retained_root)
    testcase.assertFalse(retained_root.exists())
    testcase.assertIsNotNone(process)
    testcase.assertIsNotNone(process.poll())


def _run_isolated_migration_cli(
    engine,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    """仅把临时 replay Engine 的凭据注入 canonical CLI。"""
    url = engine.url
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("BOND_DB_")
    }
    environment.update(
        {
            "BOND_DB_HOST": str(url.host),
            "BOND_DB_PORT": str(url.port),
            "BOND_DB_USER": str(url.username),
            "BOND_DB_PASSWORD": str(url.password),
            "BOND_DB_NAME": str(url.database),
        }
    )
    return subprocess.run(
        (
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "apply_migrations.py"),
            *arguments,
        ),
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )


def _assert_isolated_cli_succeeded(
    testcase: unittest.TestCase,
    result: subprocess.CompletedProcess[str],
) -> None:
    """子进程失败时不把可能含凭据的输出写进测试日志。"""
    testcase.assertEqual(
        0,
        result.returncode,
        "isolated migration CLI failed",
    )


def _read_isolated_cli_json(
    testcase: unittest.TestCase,
    engine,
    *arguments: str,
) -> dict[str, object]:
    result = _run_isolated_migration_cli(engine, *arguments)
    _assert_isolated_cli_succeeded(testcase, result)
    lines = [
        line for line in result.stdout.splitlines() if line.strip()
    ]
    try:
        payload = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise AssertionError(
            "isolated migration CLI did not return JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise AssertionError(
            "isolated migration CLI returned non-object JSON"
        )
    return payload


def _history_and_fingerprint_snapshot(engine):
    with engine.connect() as connection:
        fingerprint = (
            migration_runner.read_daily_ledger_schema_fingerprint(
                connection
            )
        )
    history = tuple(
        (
            int(row["version"]),
            str(row["filename"]),
            str(row["sha256"]),
            str(row["state"]),
            int(row["baseline_bootstrap"]),
        )
        for row in _history_rows(engine)
    )
    return history, fingerprint


@unittest.skipUnless(
    os.environ.get("BFL_MIGRATION_017_MYSQL") == "1",
    "set BFL_MIGRATION_017_MYSQL=1 for migration 017 MySQL recovery",
)
class Migration017MySQLRecoveryTests(unittest.TestCase):
    def test_canonical_cli_apply_is_idempotent_on_legacy_016(self) -> None:
        retained_root = None
        process = None
        with isolated_replay_mysql() as server:
            retained_root = server.root
            process = server.process
            _schema, engine = server.create_replay_database()
            _execute_legacy_016_without_history(engine)

            first_apply = _run_isolated_migration_cli(engine, "--apply")
            _assert_isolated_cli_succeeded(self, first_apply)
            first_history, first_fingerprint = (
                _history_and_fingerprint_snapshot(engine)
            )
            migration_runner.validate_daily_ledger_schema_fingerprint(
                first_fingerprint,
                allow_missing=False,
            )

            second_apply = _run_isolated_migration_cli(engine, "--apply")
            _assert_isolated_cli_succeeded(self, second_apply)
            second_history, second_fingerprint = (
                _history_and_fingerprint_snapshot(engine)
            )

            self.assertEqual("", second_apply.stdout.strip())
            self.assertEqual(first_history, second_history)
            self.assertEqual(first_fingerprint, second_fingerprint)
            self.assertEqual(
                list(range(1, 19)),
                [row[0] for row in second_history],
            )
            self.assertTrue(
                all(row[3] == "APPLIED" for row in second_history)
            )

        _assert_mysql_cleaned(
            self,
            retained_root=retained_root,
            process=process,
        )

    def test_canonical_cli_recovers_mid_017_then_applies_018(self) -> None:
        retained_root = None
        process = None
        with isolated_replay_mysql() as server:
            retained_root = server.root
            process = server.process
            _schema, engine = server.create_replay_database()
            _apply_until_mid_017_ddl(engine)
            _assert_exact_applying_017_history(self, engine)

            inspection = _read_isolated_cli_json(
                self,
                engine,
                "--inspect-applying-017",
            )
            self.assertEqual(
                "COMPATIBLE_PARTIAL",
                inspection["classification"],
            )
            digest = str(inspection["state_digest"])
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            recovery = _read_isolated_cli_json(
                self,
                engine,
                "--recover-applying-017",
                "--apply",
                "--state-digest",
                digest,
            )
            self.assertEqual("APPLIED", recovery["recovery_outcome"])
            self.assertEqual(
                "COMPATIBLE_PARTIAL",
                recovery["initial_classification"],
            )
            _assert_017_applied(self, engine)
            self.assertEqual(
                list(range(1, 18)),
                [int(row["version"]) for row in _history_rows(engine)],
            )

            apply_018 = _run_isolated_migration_cli(engine, "--apply")
            _assert_isolated_cli_succeeded(self, apply_018)
            rows = _history_rows(engine)
            self.assertEqual(
                list(range(1, 19)),
                [int(row["version"]) for row in rows],
            )
            self.assertEqual("APPLIED", rows[-1]["state"])

        _assert_mysql_cleaned(
            self,
            retained_root=retained_root,
            process=process,
        )

    def test_canonical_cli_recovers_pre_ddl_018(self) -> None:
        retained_root = None
        process = None
        with isolated_replay_mysql() as server:
            retained_root = server.root
            process = server.process
            _schema, engine = server.create_replay_database()
            _apply_until_before_018_ddl(engine)

            inspection = _read_isolated_cli_json(
                self,
                engine,
                "--inspect-applying-018",
            )
            self.assertEqual(
                "COMPATIBLE_PARTIAL",
                inspection["classification"],
            )
            recovery = _read_isolated_cli_json(
                self,
                engine,
                "--recover-applying-018",
                "--apply",
                "--state-digest",
                str(inspection["state_digest"]),
            )

            self.assertEqual("APPLIED", recovery["recovery_outcome"])
            self.assertEqual(
                "COMPATIBLE_PARTIAL",
                recovery["initial_classification"],
            )
            rows = _history_rows(engine)
            self.assertEqual(18, len(rows))
            self.assertEqual("APPLIED", rows[-1]["state"])

        _assert_mysql_cleaned(
            self,
            retained_root=retained_root,
            process=process,
        )

    def test_canonical_cli_marks_ddl_complete_018_history(self) -> None:
        retained_root = None
        process = None
        with isolated_replay_mysql() as server:
            retained_root = server.root
            process = server.process
            _schema, engine = server.create_replay_database()
            _apply_until_after_018_ddl(engine)

            inspection = _read_isolated_cli_json(
                self,
                engine,
                "--inspect-applying-018",
            )
            self.assertEqual("COMPLETE", inspection["classification"])
            recovery = _read_isolated_cli_json(
                self,
                engine,
                "--recover-applying-018",
                "--apply",
                "--state-digest",
                str(inspection["state_digest"]),
            )

            self.assertEqual("APPLIED", recovery["recovery_outcome"])
            self.assertEqual("COMPLETE", recovery["initial_classification"])
            rows = _history_rows(engine)
            self.assertEqual(18, len(rows))
            self.assertEqual("APPLIED", rows[-1]["state"])

        _assert_mysql_cleaned(
            self,
            retained_root=retained_root,
            process=process,
        )

    def test_normal_apply_reaches_complete_history(self) -> None:
        retained_root = None
        process = None
        with isolated_replay_mysql() as server:
            retained_root = server.root
            process = server.process
            _schema, engine = server.create_replay_database()
            _execute_legacy_016_without_history(engine)

            migration_runner.apply_migration_files(engine, MIGRATIONS)

            with engine.connect() as connection:
                fingerprint = (
                    migration_runner
                    .read_daily_ledger_schema_fingerprint(connection)
                )
            migration_runner.validate_daily_ledger_schema_fingerprint(
                fingerprint,
                allow_missing=False,
            )
            rows = _history_rows(engine)
            self.assertEqual(
                list(range(1, 19)),
                [int(row["version"]) for row in rows],
            )
            self.assertEqual("APPLIED", rows[16]["state"])
            self.assertEqual(
                "017_daily_schedule_ledger.sql",
                rows[16]["filename"],
            )
            self.assertIsNotNone(rows[16]["applied_at"])

        _assert_mysql_cleaned(
            self,
            retained_root=retained_root,
            process=process,
        )

    def test_complete_schema_marks_history_without_replaying_ddl(
        self,
    ) -> None:
        retained_root = None
        process = None
        with isolated_replay_mysql() as server:
            retained_root = server.root
            process = server.process
            schema, engine = server.create_replay_database()
            self.assertRegex(
                schema,
                r"^bfl_real_replay_[0-9a-f]{20}$",
            )
            _apply_until_after_017_ddl(engine)
            _assert_exact_applying_017_history(self, engine)

            inspection = _inspect_017(engine)
            self.assertEqual(
                "COMPLETE",
                inspection["classification"],
            )
            replay_sentinel = Mock(
                side_effect=AssertionError(
                    "COMPLETE recovery must not replay migration 017"
                )
            )
            with patch.object(
                migration_runner,
                "_execute_prepared_migration_files",
                replay_sentinel,
            ):
                result = _recover_017(engine, inspection)

            replay_sentinel.assert_not_called()
            self.assertEqual(
                "APPLIED",
                result["recovery_outcome"],
            )
            self.assertEqual(
                "COMPLETE",
                result["initial_classification"],
            )
            _assert_017_applied(self, engine)

        _assert_mysql_cleaned(
            self,
            retained_root=retained_root,
            process=process,
        )

    def test_legacy_016_replays_017_to_complete_fingerprint(
        self,
    ) -> None:
        retained_root = None
        process = None
        with isolated_replay_mysql() as server:
            retained_root = server.root
            process = server.process
            _schema, engine = server.create_replay_database()
            _apply_until_before_017_ddl(engine)
            _assert_exact_applying_017_history(self, engine)

            inspection = _inspect_017(engine)
            self.assertEqual(
                "COMPATIBLE_PARTIAL",
                inspection["classification"],
            )
            result = _recover_017(engine, inspection)

            self.assertEqual(
                "APPLIED",
                result["recovery_outcome"],
            )
            self.assertEqual(
                "COMPATIBLE_PARTIAL",
                result["initial_classification"],
            )
            with engine.connect() as connection:
                fingerprint = (
                    migration_runner
                    .read_daily_ledger_schema_fingerprint(connection)
                )
            migration_runner.validate_daily_ledger_schema_fingerprint(
                fingerprint,
                allow_missing=False,
            )
            _assert_017_applied(self, engine)

        _assert_mysql_cleaned(
            self,
            retained_root=retained_root,
            process=process,
        )

    def test_mid_ddl_partial_apply_recovers_to_complete(
        self,
    ) -> None:
        retained_root = None
        process = None
        with isolated_replay_mysql() as server:
            retained_root = server.root
            process = server.process
            _schema, engine = server.create_replay_database()
            _apply_until_mid_017_ddl(engine)
            _assert_exact_applying_017_history(self, engine)
            with engine.connect() as connection:
                committed_tables = int(
                    connection.execute(
                        text(
                            """
                            SELECT COUNT(*)
                            FROM information_schema.tables
                            WHERE table_schema = DATABASE()
                              AND table_name IN (
                                  't_input_generations',
                                  't_schedule_occurrences'
                              )
                            """
                        )
                    ).scalar_one()
                )
            self.assertEqual(2, committed_tables)

            inspection = _inspect_017(engine)
            self.assertEqual(
                "COMPATIBLE_PARTIAL",
                inspection["classification"],
            )
            result = _recover_017(engine, inspection)

            self.assertEqual(
                "APPLIED",
                result["recovery_outcome"],
            )
            self.assertEqual(
                "COMPATIBLE_PARTIAL",
                result["initial_classification"],
            )
            _assert_017_applied(self, engine)

        _assert_mysql_cleaned(
            self,
            retained_root=retained_root,
            process=process,
        )

    def test_unsafe_schema_drift_keeps_history_applying(
        self,
    ) -> None:
        retained_root = None
        process = None
        with isolated_replay_mysql() as server:
            retained_root = server.root
            process = server.process
            _schema, engine = server.create_replay_database()
            _apply_until_after_017_ddl(engine)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        ALTER TABLE t_schedule_occurrences
                        MODIFY COLUMN sla_accepted_target_count
                            BIGINT NULL
                        """
                    )
                )

            inspection = _inspect_017(engine)
            self.assertEqual(
                "UNSAFE",
                inspection["classification"],
            )
            self.assertIn(
                "t_schedule_occurrences.sla_accepted_target_count",
                str(inspection["reason"]),
            )
            with self.assertRaisesRegex(
                migration_runner.MigrationHistoryError,
                "refused unsafe state",
            ):
                _recover_017(engine, inspection)
            _assert_exact_applying_017_history(self, engine)

        _assert_mysql_cleaned(
            self,
            retained_root=retained_root,
            process=process,
        )

    def test_namespace_collision_fails_before_ddl_and_fences_recovery(
        self,
    ) -> None:
        retained_root = None
        process = None
        with isolated_replay_mysql() as server:
            retained_root = server.root
            process = server.process
            _schema, engine = server.create_replay_database()
            _execute_legacy_016_without_history(engine)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        CREATE TABLE bfl_fk_conflict_parent (
                            id BIGINT NOT NULL PRIMARY KEY
                        ) ENGINE=InnoDB
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE bfl_fk_conflict_child (
                            id BIGINT NOT NULL PRIMARY KEY,
                            parent_id BIGINT NULL,
                            CONSTRAINT FK_SCHEME_RUN_SCHEDULE_ITEM
                                FOREIGN KEY (parent_id)
                                REFERENCES bfl_fk_conflict_parent(id)
                                ON DELETE RESTRICT
                        ) ENGINE=InnoDB
                        """
                    )
                )

            with self.assertRaisesRegex(
                migration_runner.MigrationPreflightError,
                "constraint namespace",
            ):
                migration_runner.apply_migration_files(
                    engine,
                    MIGRATIONS,
                )
            _assert_exact_applying_017_history(self, engine)
            _assert_no_017_business_ddl(self, engine)

            blocked = _inspect_017(engine)
            self.assertEqual(
                "UNSAFE",
                blocked["classification"],
            )
            with engine.begin() as connection:
                connection.execute(
                    text("DROP TABLE bfl_fk_conflict_child")
                )
                connection.execute(
                    text("DROP TABLE bfl_fk_conflict_parent")
                )
            clean = _inspect_017(engine)
            self.assertEqual(
                "COMPATIBLE_PARTIAL",
                clean["classification"],
            )
            self.assertNotEqual(
                blocked["state_digest"],
                clean["state_digest"],
            )
            with self.assertRaisesRegex(
                migration_runner.MigrationHistoryError,
                "state digest changed",
            ):
                _recover_017(engine, blocked)

            result = _recover_017(engine, clean)
            self.assertEqual(
                "APPLIED",
                result["recovery_outcome"],
            )
            _assert_017_applied(self, engine)

        _assert_mysql_cleaned(
            self,
            retained_root=retained_root,
            process=process,
        )

    def test_uppercase_check_collision_is_unsafe_on_target_mysql(
        self,
    ) -> None:
        retained_root = None
        process = None
        with isolated_replay_mysql() as server:
            retained_root = server.root
            process = server.process
            _schema, engine = server.create_replay_database()
            _execute_legacy_016_without_history(engine)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        CREATE TABLE bfl_check_conflict (
                            id BIGINT NOT NULL,
                            CONSTRAINT
                                CK_SCHEDULE_ITEM_FAILURE_CODE
                                CHECK (id > 0)
                        ) ENGINE=InnoDB
                        """
                    )
                )

            with self.assertRaisesRegex(
                migration_runner.MigrationPreflightError,
                "constraint namespace",
            ):
                migration_runner.apply_migration_files(
                    engine,
                    MIGRATIONS,
                )
            _assert_exact_applying_017_history(self, engine)
            blocked = _inspect_017(engine)
            self.assertEqual(
                "UNSAFE",
                blocked["classification"],
            )
            self.assertIn(
                "CK_SCHEDULE_ITEM_FAILURE_CODE",
                str(blocked["reason"]),
            )
            _assert_no_017_business_ddl(self, engine)
            with engine.begin() as connection:
                connection.execute(
                    text("DROP TABLE bfl_check_conflict")
                )
            clean = _inspect_017(engine)
            self.assertEqual(
                "COMPATIBLE_PARTIAL",
                clean["classification"],
            )
            self.assertNotEqual(
                blocked["state_digest"],
                clean["state_digest"],
            )
            _recover_017(engine, clean)
            _assert_017_applied(self, engine)

        _assert_mysql_cleaned(
            self,
            retained_root=retained_root,
            process=process,
        )

    def test_mysql_8045_namespace_semantics_matrix(
        self,
    ) -> None:
        retained_root = None
        process = None
        with isolated_replay_mysql() as server:
            retained_root = server.root
            process = server.process
            schema, engine = server.create_replay_database()
            _apply_until_before_017_ddl(engine)
            other_schema = f"{schema}_other"
            replay_username = str(engine.url.username)
            self.assertRegex(
                replay_username,
                r"^bfl_rr_[0-9a-f]{20}$",
            )
            admin = server._admin_engine()
            try:
                with admin.begin() as connection:
                    connection.execute(
                        text(
                            f"""
                            CREATE DATABASE `{other_schema}`
                                CHARACTER SET utf8mb4
                                COLLATE utf8mb4_0900_ai_ci
                            """
                        )
                    )
                    connection.execute(
                        text(
                            f"""
                            CREATE TABLE `{other_schema}`.other_check (
                                id BIGINT NOT NULL,
                                CONSTRAINT ck_schedule_item_failure_code
                                    CHECK (id > 0)
                            ) ENGINE=InnoDB
                            """
                        )
                    )
                    connection.execute(
                        text(
                            f"""
                            CREATE TABLE `{other_schema}`.other_parent (
                                id BIGINT NOT NULL PRIMARY KEY
                            ) ENGINE=InnoDB
                            """
                        )
                    )
                    connection.execute(
                        text(
                            f"""
                            CREATE TABLE `{other_schema}`.other_child (
                                id BIGINT NOT NULL PRIMARY KEY,
                                parent_id BIGINT NULL,
                                CONSTRAINT fk_schedule_item_occurrence
                                    FOREIGN KEY (parent_id)
                                    REFERENCES
                                        `{other_schema}`.other_parent(id)
                            ) ENGINE=InnoDB
                            """
                        )
                    )
                    connection.exec_driver_sql(
                        f"GRANT SELECT ON `{other_schema}`.* "
                        f"TO '{replay_username}'@'127.0.0.1'"
                    )
            finally:
                admin.dispose()
            with engine.begin() as connection:
                facts = connection.execute(
                    text(
                        """
                        SELECT VERSION() AS server_version,
                               @@lower_case_table_names
                                   AS lower_case_table_names
                        """
                    )
                ).mappings().one()
                self.assertEqual("8.0.45", facts["server_version"])
                self.assertEqual(
                    2,
                    int(facts["lower_case_table_names"]),
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE allowed_accent_check (
                            id BIGINT NOT NULL,
                            CONSTRAINT
                                `ck_schédule_item_failure_code`
                                CHECK (id > 0)
                        ) ENGINE=InnoDB
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE allowed_cross_type_check (
                            id BIGINT NOT NULL,
                            CONSTRAINT fk_schedule_item_occurrence
                                CHECK (id > 0)
                        ) ENGINE=InnoDB
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE allowed_parent (
                            id BIGINT NOT NULL PRIMARY KEY
                        ) ENGINE=InnoDB
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE allowed_cross_type_fk (
                            id BIGINT NOT NULL PRIMARY KEY,
                            parent_id BIGINT NULL,
                            CONSTRAINT ck_schedule_item_failure_code
                                FOREIGN KEY (parent_id)
                                REFERENCES allowed_parent(id)
                            ) ENGINE=InnoDB
                        """
                    )
                )
                visible_other_constraints = int(
                    connection.execute(
                        text(
                            """
                            SELECT COUNT(*)
                            FROM information_schema.table_constraints
                            WHERE constraint_schema = :other_schema
                              AND constraint_name IN (
                                  'ck_schedule_item_failure_code',
                                  'fk_schedule_item_occurrence'
                              )
                            """
                        ),
                        {"other_schema": other_schema},
                    ).scalar_one()
                )
                self.assertEqual(2, visible_other_constraints)

            inspection = _inspect_017(engine)
            self.assertEqual(
                "COMPATIBLE_PARTIAL",
                inspection["classification"],
            )
            result = _recover_017(engine, inspection)
            self.assertEqual(
                "APPLIED",
                result["recovery_outcome"],
            )
            with engine.connect() as connection:
                state = (
                    migration_runner.read_daily_ledger_upgrade_state(
                        connection
                    )
                )
                target_constraints = (
                    state["schema_global_constraints"]
                )
            self.assertEqual(14, len(target_constraints))
            self.assertNotIn(
                "ck_schédule_item_failure_code",
                {
                    str(row["constraint_name"])
                    for row in target_constraints
                },
            )
            migration_runner.validate_daily_ledger_upgrade_state(state)
            _assert_017_applied(self, engine)

        _assert_mysql_cleaned(
            self,
            retained_root=retained_root,
            process=process,
        )


if __name__ == "__main__":
    unittest.main()
