from __future__ import annotations

import copy
import hashlib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from migrations import runner as migration_runner
from scripts import apply_migrations as migration_cli


MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
MIGRATION = MIGRATIONS_DIR / "019_retire_scheme_serving_pointer.sql"
EXPECTED_DATABASE_NAME = "bfl_019_identity"
EXPECTED_SERVER_UUID = "12345678-1234-4abc-8def-123456789abc"
EXPECTED_IDENTITY_ARGS = (
    "--expected-database-name",
    EXPECTED_DATABASE_NAME,
    "--expected-server-uuid",
    EXPECTED_SERVER_UUID,
)


class _ExecutionConnection:
    dialect = SimpleNamespace(name="mysql")

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement, params=None) -> Mock:
        self.statements.append(str(statement))
        return Mock()


class _ExecutionContext:
    def __init__(self, connection: _ExecutionConnection) -> None:
        self._connection = connection

    def __enter__(self) -> _ExecutionConnection:
        return self._connection

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


class _ExecutionEngine:
    def __init__(self) -> None:
        self.connection = _ExecutionConnection()

    def begin(self) -> _ExecutionContext:
        return _ExecutionContext(self.connection)


class ServingPointerRetirementMigrationTests(unittest.TestCase):
    @staticmethod
    def _identity_engine(
        *,
        database_name: str = EXPECTED_DATABASE_NAME,
        server_uuid: str = EXPECTED_SERVER_UUID,
    ) -> tuple[MagicMock, Mock]:
        engine = MagicMock()
        connection = Mock()
        connection.execute.return_value.one.return_value = (
            database_name,
            server_uuid,
        )
        engine.connect.return_value.__enter__.return_value = connection
        return engine, connection

    @staticmethod
    def _complete_target_state() -> dict[str, object]:
        source = migration_runner._expected_serving_pointer_retirement_state()
        return {
            "exists": False,
            "dependent_objects": copy.deepcopy(
                source["dependent_objects"]
            ),
            "schedule_run_started_at_shape": copy.deepcopy(
                source["schedule_run_started_at_shape"]
            ),
        }

    def _require_019_contract(self) -> None:
        self.assertTrue(
            MIGRATION.is_file(),
            "migration 019 forward-retirement file is missing",
        )
        for name in (
            "SERVING_POINTER_RETIREMENT_MIGRATION_VERSION",
            "SERVING_POINTER_RETIREMENT_MIGRATION_FILENAME",
            "SERVING_POINTER_RETIREMENT_MIGRATION_SHA256",
            "build_applying_019_inspection",
            "recover_applying_migration_019",
        ):
            self.assertTrue(
                hasattr(migration_runner, name),
                f"migration 019 runner contract is missing {name}",
            )

    @staticmethod
    def _applying_history():
        manifest = migration_runner.validate_release_migration_manifest(
            sorted(MIGRATIONS_DIR.glob("*.sql"))
        )
        history = [
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
                "state": (
                    "APPLYING" if migration.version == 19 else "APPLIED"
                ),
                "baseline_bootstrap": 1 if migration.version <= 16 else 0,
            }
            for migration in manifest
        ]
        return manifest, history

    def _inspection(self, pointer_state: dict[str, object]):
        self._require_019_contract()
        manifest, history = self._applying_history()
        return migration_runner.build_applying_019_inspection(
            manifest=manifest,
            history=history,
            database_identity={
                "database_name": "bfl_019_recovery",
                "server_uuid": "019-server",
            },
            pointer_state=pointer_state,
        )

    def test_forward_migration_is_strict_drop(self) -> None:
        self._require_019_contract()
        normalized = " ".join(
            MIGRATION.read_text(encoding="utf-8").lower().split()
        )

        self.assertIn("drop table t_scheme_serving_pointer", normalized)
        self.assertNotIn(
            "drop table if exists t_scheme_serving_pointer",
            normalized,
        )
        self.assertEqual(
            19,
            migration_runner.SERVING_POINTER_RETIREMENT_MIGRATION_VERSION,
        )
        self.assertEqual(
            MIGRATION.name,
            migration_runner.SERVING_POINTER_RETIREMENT_MIGRATION_FILENAME,
        )
        self.assertEqual(
            hashlib.sha256(MIGRATION.read_bytes()).hexdigest(),
            migration_runner.SERVING_POINTER_RETIREMENT_MIGRATION_SHA256,
        )

    def test_applying_019_accepts_only_source_or_absent_target(self) -> None:
        self._require_019_contract()
        source = {
            **migration_runner._expected_serving_pointer_retirement_state(),
            "row_count": 100,
        }
        target = self._complete_target_state()
        unsafe = copy.deepcopy(source)
        unsafe["inbound_foreign_keys"] = {
            "fk_external_pointer": {
                "table": "external_consumer",
                "columns": ("pointer_id",),
                "referenced_table": "t_scheme_serving_pointer",
                "referenced_columns": ("id",),
                "delete_rule": "restrict",
                "update_rule": "restrict",
            }
        }

        self.assertEqual(
            "COMPATIBLE_PARTIAL",
            self._inspection(source)["classification"],
        )
        self.assertEqual(
            "COMPLETE",
            self._inspection(target)["classification"],
        )
        rejected = self._inspection(unsafe)
        self.assertEqual("UNSAFE", rejected["classification"])
        self.assertIn("inbound", str(rejected["reason"]))

    def test_absent_019_target_requires_clean_018_state(self) -> None:
        self._require_019_contract()
        target = self._complete_target_state()
        incomplete_target = {"exists": False}
        dependency_drift = copy.deepcopy(target)
        dependency_drift["dependent_objects"]["routines"] = (
            "bfl_019_recovery.procedure:p_pointer_consumer",
        )
        started_at_drift = copy.deepcopy(target)
        started_at_drift["schedule_run_started_at_shape"] = {
            "exists": True,
            "column_type": "datetime",
            "is_nullable": "no",
            "column_default": "current_timestamp",
            "extra": "default_generated",
        }

        self.assertEqual("COMPLETE", self._inspection(target)["classification"])
        self.assertEqual(
            "UNSAFE",
            self._inspection(incomplete_target)["classification"],
        )
        self.assertEqual(
            "UNSAFE",
            self._inspection(dependency_drift)["classification"],
        )
        self.assertEqual(
            "UNSAFE",
            self._inspection(started_at_drift)["classification"],
        )

    def test_019_source_state_binds_external_dependencies_and_018_target(
        self,
    ) -> None:
        self._require_019_contract()
        source = migration_runner._expected_serving_pointer_retirement_state()

        self.assertIn("dependent_objects", source)
        self.assertEqual(
            {
                "views": (),
                "triggers": (),
                "routines": (),
                "events": (),
            },
            source["dependent_objects"],
        )
        self.assertIn("schedule_run_started_at_shape", source)
        self.assertEqual(
            "COMPLETE",
            migration_runner._classify_schedule_run_started_at_shape(
                source["schedule_run_started_at_shape"]
            ),
        )

    def test_applying_019_rejects_external_dependency_or_018_drift(
        self,
    ) -> None:
        self._require_019_contract()
        source = {
            **migration_runner._expected_serving_pointer_retirement_state(),
            "row_count": 100,
        }
        dependency_drift = copy.deepcopy(source)
        dependency_drift["dependent_objects"]["views"] = (
            "v_pointer_consumer",
        )
        timestamp_drift = copy.deepcopy(source)
        timestamp_drift["schedule_run_started_at_shape"] = {
            "exists": True,
            "column_type": "datetime",
            "is_nullable": "no",
            "column_default": "current_timestamp",
            "extra": "default_generated",
        }

        rejected_dependency = self._inspection(dependency_drift)
        self.assertEqual("UNSAFE", rejected_dependency["classification"])
        self.assertIn(
            "dependent",
            str(rejected_dependency["reason"]),
        )
        rejected_timestamp = self._inspection(timestamp_drift)
        self.assertEqual("UNSAFE", rejected_timestamp["classification"])
        self.assertIn(
            "started_at",
            str(rejected_timestamp["reason"]),
        )

    def test_applying_019_rejects_history_drift(self) -> None:
        self._require_019_contract()
        manifest, history = self._applying_history()
        history[-1]["sha256"] = "0" * 64
        source = {
            **migration_runner._expected_serving_pointer_retirement_state(),
            "row_count": 100,
        }

        inspection = migration_runner.build_applying_019_inspection(
            manifest=manifest,
            history=history,
            database_identity={
                "database_name": "bfl_019_recovery",
                "server_uuid": "019-server",
            },
            pointer_state=source,
        )

        self.assertEqual("UNSAFE", inspection["classification"])
        self.assertIn("checksum", str(inspection["reason"]))

    def test_complete_019_recovery_marks_history_without_replay(self) -> None:
        self._require_019_contract()
        engine = Mock()
        owner_lock = Mock()
        owner_lock.__enter__ = Mock(return_value=Mock())
        owner_lock.__exit__ = Mock(return_value=False)
        with (
            patch.object(
                migration_runner,
                "_migration_owner_connection",
                return_value=owner_lock,
            ),
            patch.object(
                migration_runner,
                "_read_applying_019_inspection",
                return_value={
                    "classification": "COMPLETE",
                    "state_digest": "a" * 64,
                    "reason": None,
                },
            ),
            patch.object(
                migration_runner,
                "_execute_prepared_migration_files",
            ) as replay,
            patch.object(
                migration_runner,
                "_mark_migration_applied",
            ) as mark_applied,
        ):
            result = migration_runner.recover_applying_migration_019(
                engine,
                sorted(MIGRATIONS_DIR.glob("*.sql")),
                expected_state_digest="a" * 64,
            )

        self.assertEqual("APPLIED", result["recovery_outcome"])
        self.assertEqual("COMPLETE", result["initial_classification"])
        replay.assert_not_called()
        mark_applied.assert_called_once()

    def test_normal_019_execution_requires_source_then_absent_target(
        self,
    ) -> None:
        self._require_019_contract()
        engine = _ExecutionEngine()
        source = {
            **migration_runner._expected_serving_pointer_retirement_state(),
            "row_count": 100,
        }
        target = self._complete_target_state()
        manifest, _history = self._applying_history()
        migration = manifest[-1]

        with (
            patch.object(migration_runner, "preflight_migration_session"),
            patch.object(
                migration_runner,
                "_read_serving_pointer_retirement_state",
                side_effect=(source, target),
            ),
        ):
            migration_runner._execute_prepared_migration_files(
                engine,
                [(migration.path, migration.statements)],
            )

        self.assertTrue(
            any(
                "DROP TABLE t_scheme_serving_pointer" in statement
                for statement in engine.connection.statements
            )
        )

    def test_019_cli_supports_read_only_inspect_and_fenced_recovery(self) -> None:
        self._require_019_contract()
        inspect_args = migration_cli._parse_args(["--inspect-applying-019"])
        self.assertTrue(inspect_args.inspect_applying_019)
        recovery_args = migration_cli._parse_args(
            [
                "--recover-applying-019",
                "--apply",
                "--state-digest",
                "a" * 64,
                "--expected-database-name",
                "bfl_019_recovery",
                "--expected-server-uuid",
                "12345678-1234-4abc-8def-123456789abc",
            ]
        )
        self.assertTrue(recovery_args.recover_applying_019)

    def test_019_cli_main_inspect_is_read_only_without_identity(
        self,
    ) -> None:
        self._require_019_contract()
        engine = Mock()
        inspection = {
            "classification": "COMPATIBLE_PARTIAL",
            "state_digest": "a" * 64,
            "reason": None,
        }
        with (
            patch.object(
                migration_cli,
                "create_engine_from_env",
                return_value=engine,
            ),
            patch.object(
                migration_cli,
                "inspect_applying_migration_019",
                return_value=inspection,
            ) as inspect,
            patch.object(
                migration_cli,
                "recover_applying_migration_019",
            ) as recover,
            patch.object(
                migration_cli,
                "apply_pending_migration_files",
            ) as apply_pending,
            patch("builtins.print"),
        ):
            migration_cli.main(["--inspect-applying-019"])

        inspect.assert_called_once()
        self.assertIs(engine, inspect.call_args.args[0])
        engine.connect.assert_not_called()
        recover.assert_not_called()
        apply_pending.assert_not_called()
        engine.dispose.assert_called_once_with()

    def test_019_cli_main_routes_fenced_recovery_after_identity(
        self,
    ) -> None:
        self._require_019_contract()
        engine, connection = self._identity_engine()
        digest = "a" * 64
        recovery = {
            "recovery_outcome": "APPLIED",
            "initial_classification": "COMPLETE",
        }
        with (
            patch.object(
                migration_cli,
                "create_engine_from_env",
                return_value=engine,
            ),
            patch.object(
                migration_cli,
                "inspect_applying_migration_019",
            ) as inspect,
            patch.object(
                migration_cli,
                "recover_applying_migration_019",
                return_value=recovery,
            ) as recover,
            patch.object(
                migration_cli,
                "apply_pending_migration_files",
            ) as apply_pending,
            patch("builtins.print"),
        ):
            migration_cli.main(
                [
                    "--recover-applying-019",
                    "--apply",
                    "--state-digest",
                    digest,
                    *EXPECTED_IDENTITY_ARGS,
                ]
            )

        connection.execute.assert_called_once()
        self.assertEqual(
            "SELECT DATABASE(), @@server_uuid",
            " ".join(
                str(connection.execute.call_args.args[0]).split()
            ),
        )
        inspect.assert_not_called()
        recover.assert_called_once()
        self.assertIs(engine, recover.call_args.args[0])
        self.assertEqual(
            digest,
            recover.call_args.kwargs["expected_state_digest"],
        )
        apply_pending.assert_not_called()
        engine.dispose.assert_called_once_with()

    def test_019_cli_recovery_identity_mismatch_stops_before_recovery(
        self,
    ) -> None:
        self._require_019_contract()
        engine, connection = self._identity_engine(
            database_name="bfl_wrong_target",
        )
        with (
            patch.object(
                migration_cli,
                "create_engine_from_env",
                return_value=engine,
            ),
            patch.object(
                migration_cli,
                "recover_applying_migration_019",
            ) as recover,
            patch.object(
                migration_cli,
                "apply_pending_migration_files",
            ) as apply_pending,
            self.assertRaisesRegex(
                RuntimeError,
                "database identity",
            ),
        ):
            migration_cli.main(
                [
                    "--recover-applying-019",
                    "--apply",
                    "--state-digest",
                    "a" * 64,
                    *EXPECTED_IDENTITY_ARGS,
                ]
            )

        connection.execute.assert_called_once()
        recover.assert_not_called()
        apply_pending.assert_not_called()
        engine.dispose.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
