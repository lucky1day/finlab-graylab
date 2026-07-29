from __future__ import annotations

import unittest
from unittest.mock import patch

from migrations.runner import (
    MigrationPreflightError,
    preflight_schedule_run_started_at_nullable,
)


DATABASE_NAME = "bfl_preflight_check"
SERVER_UUID = "12345678-1234-4abc-8def-123456789abc"
SOURCE_017_ROW = {
    "column_type": "datetime",
    "is_nullable": "NO",
    "column_default": "CURRENT_TIMESTAMP",
    "extra": "DEFAULT_GENERATED",
}
TARGET_018_ROW = {
    "column_type": "datetime(6)",
    "is_nullable": "YES",
    "column_default": "CURRENT_TIMESTAMP(6)",
    "extra": "DEFAULT_GENERATED",
}


class _MappingsResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, object]]:
        return [dict(row) for row in self._rows]

    def one(self) -> dict[str, object]:
        if len(self._rows) != 1:
            raise RuntimeError("expected exactly one row")
        return dict(self._rows[0])


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _MappingsResult:
        return _MappingsResult(self._rows)


class _FakeConnection:
    def __init__(self, column_rows: list[dict[str, object]]) -> None:
        self.column_rows = column_rows
        self.statements: list[str] = []
        self.closed = False

    def execute(self, statement: object) -> _Result:
        sql = str(statement).lower()
        self.statements.append(sql)
        if "information_schema.columns" in sql:
            return _Result(self.column_rows)
        if "@@server_uuid" in sql:
            return _Result(
                [
                    {
                        "database_name": DATABASE_NAME,
                        "server_uuid": SERVER_UUID,
                    }
                ]
            )
        raise AssertionError(f"unexpected SQL: {statement}")

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True


class _FakeEngine:
    def __init__(self, column_rows: list[dict[str, object]]) -> None:
        self.connection = _FakeConnection(column_rows)

    def connect(self) -> _FakeConnection:
        return self.connection


class ScheduleRunStartedAtPreflightTests(unittest.TestCase):
    def test_017_shape_is_rejected_with_actionable_guidance(self) -> None:
        engine = _FakeEngine([SOURCE_017_ROW])

        with self.assertRaises(MigrationPreflightError) as caught:
            preflight_schedule_run_started_at_nullable(engine)

        message = str(caught.exception)
        self.assertIn("started_at = NULL", message)
        self.assertIn(
            "current=datetime NOT NULL DEFAULT CURRENT_TIMESTAMP",
            message,
        )
        self.assertIn(
            "expected=datetime(6) NULL DEFAULT CURRENT_TIMESTAMP(6)",
            message,
        )
        self.assertIn("scripts/apply_migrations.py --apply", message)
        self.assertIn("--expected-database-name <database-name>", message)
        self.assertIn("--expected-server-uuid <server-uuid>", message)
        self.assertIn("controlled read-only inspect", message)
        self.assertTrue(engine.connection.closed)

    def test_failure_does_not_read_or_expose_database_identity(self) -> None:
        """runtime 日志不得回显生产 database name 或 server UUID。"""
        engine = _FakeEngine([SOURCE_017_ROW])

        with self.assertRaises(MigrationPreflightError) as caught:
            preflight_schedule_run_started_at_nullable(engine)

        message = str(caught.exception)
        self.assertEqual(len(engine.connection.statements), 1)
        self.assertNotIn(DATABASE_NAME, message)
        self.assertNotIn(SERVER_UUID, message)
        self.assertIn("--expected-database-name <database-name>", message)
        self.assertIn("--expected-server-uuid <server-uuid>", message)

    def test_018_shape_passes_with_a_single_read(self) -> None:
        engine = _FakeEngine([TARGET_018_ROW])

        preflight_schedule_run_started_at_nullable(engine)

        self.assertEqual(len(engine.connection.statements), 1)
        self.assertIn(
            "information_schema.columns",
            engine.connection.statements[0],
        )
        self.assertTrue(engine.connection.closed)

    def test_missing_column_is_rejected(self) -> None:
        engine = _FakeEngine([])

        with self.assertRaises(MigrationPreflightError) as caught:
            preflight_schedule_run_started_at_nullable(engine)

        self.assertIn("<column is missing>", str(caught.exception))

    def test_partially_migrated_shape_is_rejected(self) -> None:
        engine = _FakeEngine(
            [{**TARGET_018_ROW, "is_nullable": "NO"}]
        )

        with self.assertRaises(MigrationPreflightError) as caught:
            preflight_schedule_run_started_at_nullable(engine)

        self.assertIn(
            "current=datetime(6) NOT NULL",
            str(caught.exception),
        )


class LedgerEntryPreflightWiringTests(unittest.TestCase):
    """018 结构预检必须先于会读取 started_at 的容量候选校验。"""

    def test_current_authority_preflights_before_capacity_candidate(self) -> None:
        from scheduler import daily_runtime

        calls: list[str] = []
        engine = object()
        with (
            patch.object(
                daily_runtime,
                "preflight_schedule_run_started_at_nullable",
                side_effect=lambda value: calls.append("preflight"),
            ),
            patch(
                "shared.daily_coordinator_mode."
                "bootstrap_deployment_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch(
                "scheduler.capacity_runtime_admission."
                "require_current_capacity_admission",
                side_effect=lambda *a, **k: (
                    calls.append("capacity")
                    or {"status": "ADMITTED"}
                ),
            ),
        ):
            daily_runtime._require_production_entry_authority(
                engine=engine,
                verify_current=True,
            )

        self.assertEqual(["preflight", "capacity"], calls)


if __name__ == "__main__":
    unittest.main()
