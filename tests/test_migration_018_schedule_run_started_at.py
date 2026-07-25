from __future__ import annotations

import hashlib
import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import apply_migrations as migration_runner
from scripts.apply_migrations import split_sql_statements


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "018_schedule_run_started_at_nullable.sql"
)
MIGRATIONS_DIR = MIGRATION.parent


class ScheduleRunStartedAtMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = MIGRATION.read_text(encoding="utf-8")
        self.normalized = re.sub(r"\s+", " ", self.sql).lower()

    def test_guards_the_only_supported_source_and_target_shapes(self) -> None:
        self.assertIn(
            "information_schema.columns",
            self.normalized,
        )
        self.assertIn(
            "unexpected t_scheme_runs.started_at definition",
            self.normalized,
        )
        self.assertIn(
            "column_type = 'datetime'",
            self.normalized,
        )
        self.assertIn(
            "upper(column_default) = 'current_timestamp'",
            self.normalized,
        )
        self.assertIn(
            "column_type = 'datetime(6)'",
            self.normalized,
        )
        self.assertIn(
            "upper(column_default) = 'current_timestamp(6)'",
            self.normalized,
        )
        self.assertIn("lower(is_nullable) = 'no'", self.normalized)
        self.assertIn("lower(is_nullable) = 'yes'", self.normalized)

    def test_makes_started_at_nullable_without_changing_legacy_default(
        self,
    ) -> None:
        self.assertIn(
            "alter table t_scheme_runs modify column started_at "
            "datetime(6) null default current_timestamp(6)",
            self.normalized,
        )

    def test_guard_executes_before_the_single_direct_ddl(self) -> None:
        statements = split_sql_statements(self.sql)
        alter_indexes = [
            index
            for index, statement in enumerate(statements)
            if statement.lstrip().lower().startswith("alter table")
        ]

        self.assertEqual(len(alter_indexes), 1)
        self.assertGreater(alter_indexes[0], 3)
        self.assertTrue(
            any(
                statement.lstrip().lower().startswith("execute ")
                for statement in statements[: alter_indexes[0]]
            )
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
                    "APPLYING"
                    if migration.version == 18
                    else "APPLIED"
                ),
                "baseline_bootstrap": 0,
            }
            for migration in manifest
        ]
        return manifest, history

    def _inspection(self, shape):
        manifest, history = self._applying_history()
        return migration_runner.build_applying_018_inspection(
            manifest=manifest,
            history=history,
            database_identity={
                "database_name": "bfl_step6_recovery",
                "server_uuid": "step6-server",
            },
            started_at_shape=shape,
        )

    def test_applying_018_inspection_accepts_only_source_or_target(
        self,
    ) -> None:
        source = {
            "exists": True,
            "column_type": "datetime",
            "is_nullable": "no",
            "column_default": "current_timestamp",
            "extra": "default_generated",
        }
        target = {
            "exists": True,
            "column_type": "datetime(6)",
            "is_nullable": "yes",
            "column_default": "current_timestamp(6)",
            "extra": "default_generated",
        }
        bad = {**target, "column_type": "datetime(3)"}

        self.assertEqual(
            "COMPATIBLE_PARTIAL",
            self._inspection(source)["classification"],
        )
        self.assertEqual(
            "COMPLETE",
            self._inspection(target)["classification"],
        )
        unsafe = self._inspection(bad)
        self.assertEqual("UNSAFE", unsafe["classification"])
        self.assertIn("unexpected", str(unsafe["reason"]))

    def test_applying_018_identity_and_history_are_exact(self) -> None:
        self.assertEqual(
            hashlib.sha256(MIGRATION.read_bytes()).hexdigest(),
            migration_runner.SCHEDULE_RUN_STARTED_AT_MIGRATION_SHA256,
        )
        manifest, history = self._applying_history()
        history[-1]["sha256"] = "0" * 64
        inspection = migration_runner.build_applying_018_inspection(
            manifest=manifest,
            history=history,
            database_identity={
                "database_name": "bfl_step6_recovery",
                "server_uuid": "step6-server",
            },
            started_at_shape={
                "exists": True,
                "column_type": "datetime(6)",
                "is_nullable": "yes",
                "column_default": "current_timestamp(6)",
                "extra": "default_generated",
            },
        )
        self.assertEqual("UNSAFE", inspection["classification"])
        self.assertIn("checksum", str(inspection["reason"]))

        _, mixed_history = self._applying_history()
        mixed_history[0]["baseline_bootstrap"] = 1
        mixed = migration_runner.build_applying_018_inspection(
            manifest=manifest,
            history=mixed_history,
            database_identity={
                "database_name": "bfl_step6_recovery",
                "server_uuid": "step6-server",
            },
            started_at_shape={
                "exists": True,
                "column_type": "datetime(6)",
                "is_nullable": "yes",
                "column_default": "current_timestamp(6)",
                "extra": "default_generated",
            },
        )
        self.assertEqual("UNSAFE", mixed["classification"])
        self.assertIn("bootstrap pattern", str(mixed["reason"]))

        _, legacy_history = self._applying_history()
        for row in legacy_history[:16]:
            row["baseline_bootstrap"] = 1
        legacy = migration_runner.build_applying_018_inspection(
            manifest=manifest,
            history=legacy_history,
            database_identity={
                "database_name": "bfl_step6_recovery",
                "server_uuid": "step6-server",
            },
            started_at_shape={
                "exists": True,
                "column_type": "datetime(6)",
                "is_nullable": "yes",
                "column_default": "current_timestamp(6)",
                "extra": "default_generated",
            },
        )
        self.assertEqual("COMPLETE", legacy["classification"])

    def test_applying_018_recovery_is_digest_fenced(self) -> None:
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
                "_read_applying_018_inspection",
                return_value={
                    "classification": "COMPLETE",
                    "state_digest": "a" * 64,
                    "reason": None,
                },
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "state digest"):
                migration_runner.recover_applying_migration_018(
                    Mock(),
                    sorted(MIGRATIONS_DIR.glob("*.sql")),
                    expected_state_digest="b" * 64,
                )

    def test_complete_018_recovery_marks_without_replay(self) -> None:
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
                "_read_applying_018_inspection",
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
            result = migration_runner.recover_applying_migration_018(
                engine,
                sorted(MIGRATIONS_DIR.glob("*.sql")),
                expected_state_digest="a" * 64,
            )

        self.assertEqual("APPLIED", result["recovery_outcome"])
        replay.assert_not_called()
        mark_applied.assert_called_once()

    def test_partial_018_recovery_replays_before_marking(self) -> None:
        engine = Mock()
        owner = Mock()
        owner_lock = Mock()
        owner_lock.__enter__ = Mock(return_value=owner)
        owner_lock.__exit__ = Mock(return_value=False)
        with (
            patch.object(
                migration_runner,
                "_migration_owner_connection",
                return_value=owner_lock,
            ),
            patch.object(
                migration_runner,
                "_read_applying_018_inspection",
                side_effect=(
                    {
                        "classification": "COMPATIBLE_PARTIAL",
                        "state_digest": "a" * 64,
                        "reason": None,
                    },
                    {
                        "classification": "COMPLETE",
                        "state_digest": "b" * 64,
                        "reason": None,
                    },
                ),
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
            result = migration_runner.recover_applying_migration_018(
                engine,
                sorted(MIGRATIONS_DIR.glob("*.sql")),
                expected_state_digest="a" * 64,
            )

        self.assertEqual(
            "COMPATIBLE_PARTIAL",
            result["initial_classification"],
        )
        replay.assert_called_once()
        mark_applied.assert_called_once()

    def test_failed_partial_018_recovery_does_not_mark(self) -> None:
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
                "_read_applying_018_inspection",
                return_value={
                    "classification": "COMPATIBLE_PARTIAL",
                    "state_digest": "a" * 64,
                    "reason": None,
                },
            ),
            patch.object(
                migration_runner,
                "_execute_prepared_migration_files",
                side_effect=RuntimeError("injected replay failure"),
            ),
            patch.object(
                migration_runner,
                "_mark_migration_applied",
            ) as mark_applied,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected replay failure",
            ):
                migration_runner.recover_applying_migration_018(
                    engine,
                    sorted(MIGRATIONS_DIR.glob("*.sql")),
                    expected_state_digest="a" * 64,
                )

        mark_applied.assert_not_called()

    def test_018_cli_authorization_modes(self) -> None:
        inspect_args = migration_runner._parse_args(
            ["--inspect-applying-018"]
        )
        self.assertTrue(inspect_args.inspect_applying_018)
        recover_args = migration_runner._parse_args(
            [
                "--recover-applying-018",
                "--apply",
                "--state-digest",
                "a" * 64,
            ]
        )
        self.assertTrue(recover_args.recover_applying_018)
        with self.assertRaises(SystemExit):
            migration_runner._parse_args(
                ["--recover-applying-018", "--apply"]
            )
        with self.assertRaises(SystemExit):
            migration_runner._parse_args(
                ["--inspect-applying-018", "--apply"]
            )


if __name__ == "__main__":
    unittest.main()
