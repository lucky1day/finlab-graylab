from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from migrations import runner as migration_runner
from migrations.runner import (
    MigrationSQLParseError,
    split_sql_statements,
)
from scripts import apply_migrations as migration_cli


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


class _RowsResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> "_RowsResult":
        return self

    def all(self) -> list[dict[str, object]]:
        return self._rows


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value


class _LegacyBaselineConnection:
    def __init__(
        self,
        expected: dict[str, object],
        *,
        structural_mutator: object | None = None,
        violations: dict[str, int] | None = None,
    ) -> None:
        self._rows = self._build_rows(expected)
        if structural_mutator is not None:
            structural_mutator(self._rows)
        self._violations = violations or {}
        self.data_probes: list[str] = []

    @staticmethod
    def _build_rows(
        expected: dict[str, object],
    ) -> dict[str, list[dict[str, object]]]:
        table_definitions = expected["table_definitions"]
        column_definitions = expected["columns"]
        index_definitions = expected["indexes"]
        foreign_key_definitions = expected["foreign_keys"]
        check_definitions = expected["checks"]
        view_definitions = expected["views"]
        assert isinstance(table_definitions, dict)
        assert isinstance(column_definitions, dict)
        assert isinstance(index_definitions, dict)
        assert isinstance(foreign_key_definitions, dict)
        assert isinstance(check_definitions, dict)
        assert isinstance(view_definitions, dict)

        table_rows = [
            {
                "table_name": table_name,
                "table_type": definition["table_type"],
                "engine": definition["engine"],
                "table_collation": definition["collation"],
            }
            for table_name, definition in table_definitions.items()
        ]
        column_rows: list[dict[str, object]] = []
        for identity, definition in column_definitions.items():
            table_name, column_name = identity.split(".", 1)
            column_rows.append(
                {
                    "table_name": table_name,
                    "column_name": column_name,
                    "column_type": definition["column_type"],
                    "is_nullable": definition["nullable"],
                    "column_default": definition["default"],
                    "extra": definition["extra"],
                    "column_comment": definition["comment"],
                    "character_set_name": definition[
                        "character_set"
                    ],
                    "collation_name": definition["collation"],
                    "ordinal_position": definition["ordinal"],
                }
            )
        index_rows: list[dict[str, object]] = []
        for identity, definition in index_definitions.items():
            table_name, index_name = identity.split(".", 1)
            for position, column_name in enumerate(
                definition["columns"],
                start=1,
            ):
                index_rows.append(
                    {
                        "table_name": table_name,
                        "index_name": index_name,
                        "non_unique": (
                            0 if definition["unique"] else 1
                        ),
                        "seq_in_index": position,
                        "column_name": column_name,
                        "sub_part": definition["sub_parts"][
                            position - 1
                        ],
                        "collation": definition["orders"][
                            position - 1
                        ],
                        "index_type": definition["index_type"],
                        "is_visible": (
                            "YES"
                            if definition["visible"]
                            else "NO"
                        ),
                        "expression": definition["expressions"][
                            position - 1
                        ],
                    }
                )
        foreign_key_rows: list[dict[str, object]] = []
        for name, definition in foreign_key_definitions.items():
            for position, (
                column_name,
                referenced_column_name,
            ) in enumerate(
                zip(
                    definition["columns"],
                    definition["referenced_columns"],
                    strict=True,
                ),
                start=1,
            ):
                foreign_key_rows.append(
                    {
                        "constraint_name": name,
                        "table_name": definition["table"],
                        "column_name": column_name,
                        "ordinal_position": position,
                        "referenced_table_name": definition[
                            "referenced_table"
                        ],
                        "referenced_column_name":
                            referenced_column_name,
                        "update_rule": definition["update_rule"],
                        "delete_rule": definition["delete_rule"],
                    }
                )
        check_rows = [
            {
                "constraint_name": name,
                "table_name": definition["table"],
                "enforced": (
                    "YES" if definition["enforced"] else "NO"
                ),
                "check_clause": definition["clause"],
            }
            for name, definition in check_definitions.items()
        ]
        view_rows = [
            {
                "table_name": name,
                "view_definition": definition["definition"],
                "check_option": definition["check_option"],
                "security_type": definition["security_type"],
                "is_updatable": definition["is_updatable"],
            }
            for name, definition in view_definitions.items()
        ]
        view_column_rows = [
            {
                "table_name": view_name,
                "column_name": column_name,
                "ordinal_position": position,
            }
            for view_name, definition in view_definitions.items()
            for position, column_name in enumerate(
                definition["columns"],
                start=1,
            )
        ]
        return {
            "tables": table_rows,
            "columns": column_rows,
            "indexes": index_rows,
            "foreign_keys": foreign_key_rows,
            "checks": check_rows,
            "views": view_rows,
            "view_columns": view_column_rows,
        }

    def execute(self, statement: object) -> _RowsResult | _ScalarResult:
        sql = " ".join(str(statement).lower().split())
        if sql == "select database()":
            return _ScalarResult("bond_db")
        match = re.search(r"/\* ([a-z0-9_]+) \*/", sql)
        if match is not None:
            name = match.group(1)
            self.data_probes.append(name)
            return _ScalarResult(self._violations.get(name, 0))
        if "from information_schema.views" in sql:
            return _RowsResult(self._rows["views"])
        if "from information_schema.key_column_usage" in sql:
            return _RowsResult(self._rows["foreign_keys"])
        if "from information_schema.table_constraints" in sql:
            return _RowsResult(self._rows["checks"])
        if "from information_schema.statistics" in sql:
            return _RowsResult(self._rows["indexes"])
        if "from information_schema.columns" in sql:
            if "table_name = 'v_latest_backtest_run'" in sql:
                return _RowsResult(self._rows["view_columns"])
            return _RowsResult(self._rows["columns"])
        if "from information_schema.tables" in sql:
            return _RowsResult(self._rows["tables"])
        raise AssertionError(f"unexpected legacy baseline query: {sql}")


class _UpgradeStateConnection:
    def __init__(
        self,
        *,
        table_names: tuple[str, ...],
        columns: dict[str, tuple[str, ...]],
        row_counts: dict[str, int] | None = None,
        violations: dict[str, int] | None = None,
    ) -> None:
        self.dialect = SimpleNamespace(name="mysql")
        self._table_names = table_names
        self._columns = columns
        self._row_counts = row_counts or {}
        self._violations = violations or {}

    def execute(self, statement: object) -> _RowsResult | _ScalarResult:
        sql = " ".join(str(statement).lower().split())
        if "from information_schema.tables" in sql:
            key = (
                "table_name"
                if "table_name as table_name" in sql
                else "TABLE_NAME"
            )
            return _RowsResult(
                [{key: name} for name in self._table_names]
            )
        if "from information_schema.columns" in sql:
            table_key = (
                "table_name"
                if "table_name as table_name" in sql
                else "TABLE_NAME"
            )
            column_key = (
                "column_name"
                if "column_name as column_name" in sql
                else "COLUMN_NAME"
            )
            return _RowsResult(
                [
                    {
                        table_key: table_name,
                        column_key: column_name,
                    }
                    for table_name, column_names in self._columns.items()
                    for column_name in column_names
                ]
            )
        for name, count in self._violations.items():
            if f"/* {name} */" in sql:
                return _ScalarResult(count)
        for table_name, row_count in self._row_counts.items():
            if f"from `{table_name}`" in sql:
                return _ScalarResult(row_count)
        for table_name in self._table_names:
            if f"from `{table_name}`" in sql:
                return _ScalarResult(0)
        raise AssertionError(f"unexpected upgrade-state query: {sql}")


class _FingerprintConnection:
    def __init__(self, expected: dict[str, object]) -> None:
        self.dialect = SimpleNamespace(name="mysql")
        self._rows = self._build_rows(expected)
        self.executed_sql: list[str] = []

    @staticmethod
    def _build_rows(
        expected: dict[str, object],
    ) -> dict[str, list[dict[str, object]]]:
        table_rows = [
            {
                "table_name": table_name,
                "table_type": definition["table_type"],
                "engine": definition["engine"],
                "table_collation": definition["collation"],
            }
            for table_name, definition in expected["tables"].items()
        ]
        column_rows: list[dict[str, object]] = []
        for identity, definition in expected["columns"].items():
            table_name, column_name = identity.split(".", 1)
            column_rows.append(
                {
                    "table_name": table_name,
                    "column_name": column_name,
                    "column_type": definition["column_type"],
                    "is_nullable": definition["nullable"],
                    "column_default": definition["default"],
                    "extra": definition["extra"],
                    "character_set_name": definition[
                        "character_set"
                    ],
                    "collation_name": definition["collation"],
                }
            )
        index_rows: list[dict[str, object]] = []
        for identity, definition in expected["indexes"].items():
            table_name, index_name = identity.split(".", 1)
            for position, column_name in enumerate(
                definition["columns"],
                start=1,
            ):
                index_rows.append(
                    {
                        "table_name": table_name,
                        "index_name": index_name,
                        "non_unique": (
                            0 if definition["unique"] else 1
                        ),
                        "seq_in_index": position,
                        "column_name": column_name,
                        "sub_part": definition["sub_parts"][
                            position - 1
                        ],
                        "collation": definition["orders"][
                            position - 1
                        ],
                        "index_type": definition["index_type"],
                        "is_visible": (
                            "YES"
                            if definition["visible"]
                            else "NO"
                        ),
                        "expression": definition["expressions"][
                            position - 1
                        ],
                    }
                )
        foreign_key_rows: list[dict[str, object]] = []
        for name, definition in expected["foreign_keys"].items():
            for position, (column_name, referenced_column) in enumerate(
                zip(
                    definition["columns"],
                    definition["referenced_columns"],
                    strict=True,
                ),
                start=1,
            ):
                foreign_key_rows.append(
                    {
                        "constraint_name": name,
                        "table_name": definition["table"],
                        "column_name": column_name,
                        "ordinal_position": position,
                        "referenced_table_name":
                            definition["referenced_table"],
                        "referenced_column_name": referenced_column,
                        "update_rule": definition["update_rule"],
                        "delete_rule": definition["delete_rule"],
                    }
                )
        check_rows = [
            {
                "constraint_name": name,
                "table_name": definition["table"],
                "enforced": (
                    "YES" if definition["enforced"] else "NO"
                ),
                "check_clause": definition["clause"],
            }
            for name, definition in expected["checks"].items()
        ]
        return {
            "information_schema.tables": table_rows,
            "information_schema.columns": column_rows,
            "information_schema.statistics": index_rows,
            "information_schema.key_column_usage": foreign_key_rows,
            "information_schema.check_constraints": check_rows,
        }

    def execute(self, statement: object) -> _RowsResult:
        sql = str(statement).lower()
        self.executed_sql.append(sql)
        for marker, rows in self._rows.items():
            if marker in sql:
                driver_rows: list[dict[str, object]] = []
                for row in rows:
                    driver_row: dict[str, object] = {}
                    for key, value in row.items():
                        explicit_alias = re.search(
                            rf"\b(?:[a-z]\.)?{re.escape(key)}\s+"
                            rf"as\s+{re.escape(key)}\b",
                            sql,
                        )
                        driver_row[
                            key if explicit_alias else key.upper()
                        ] = value
                    driver_rows.append(driver_row)
                return _RowsResult(driver_rows)
        raise AssertionError(f"unexpected fingerprint query: {sql}")


class _MigrationConnection:
    def __init__(
        self,
        *,
        dialect_name: str = "mysql",
        fail_on: str | None = None,
    ) -> None:
        self.dialect = SimpleNamespace(name=dialect_name)
        self.fail_on = fail_on
        self.executed: list[str] = []

    def execute(
        self,
        statement: object,
        parameters: object | None = None,
    ) -> _ScalarResult:
        sql = str(statement)
        self.executed.append(sql)
        if self.fail_on is not None and self.fail_on in sql:
            raise RuntimeError(f"injected execution failure: {sql}")
        return _ScalarResult(1)


class _ConnectionContext:
    def __init__(self, connection: _MigrationConnection) -> None:
        self.connection = connection

    def __enter__(self) -> _MigrationConnection:
        return self.connection

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> bool:
        return False


class _MigrationEngine:
    def __init__(self, connection: _MigrationConnection) -> None:
        self.connection = connection
        self.begin_calls = 0

    def begin(self) -> _ConnectionContext:
        self.begin_calls += 1
        return _ConnectionContext(self.connection)


class SQLStatementSplitterTests(unittest.TestCase):
    def test_ignores_semicolons_inside_quotes_identifiers_and_comments(
        self,
    ) -> None:
        sql = r"""
        -- leading comment; must not become a statement
        CREATE TABLE `semi;table` (
            single_value VARCHAR(64) DEFAULT 'a;''b',
            double_value VARCHAR(64) DEFAULT "c;""d",
            escaped_value VARCHAR(64) DEFAULT 'e\';f'
        );
        # hash comment; must not split
        SET @ddl = 'SELECT ''quoted;value''';
        /* block comment; must not split */
        SELECT `semi;identifier`, "semi;value";
        """

        statements = split_sql_statements(sql)

        self.assertEqual(3, len(statements))
        self.assertEqual(
            ["CREATE", "SET", "SELECT"],
            [statement.split(None, 1)[0].upper() for statement in statements],
        )
        self.assertIn("'a;''b'", statements[0])
        self.assertIn("`semi;table`", statements[0])
        self.assertIn("'SELECT ''quoted;value'''", statements[1])

    def test_comment_only_input_and_repeated_delimiters_are_empty(self) -> None:
        sql = """
        -- first; comment
        # second; comment
        /* third; comment */
        ;; ;
        """

        self.assertEqual([], split_sql_statements(sql))

    def test_line_comment_may_end_at_eof(self) -> None:
        self.assertEqual(
            ["SELECT 1"],
            split_sql_statements("SELECT 1; -- trailing; comment"),
        )

    def test_rejects_unterminated_quote_or_block_comment(self) -> None:
        invalid_sql = (
            "SELECT 'unterminated;",
            'SELECT "unterminated;',
            "SELECT `unterminated;",
            "SELECT 1; /* unterminated;",
        )
        for sql in invalid_sql:
            with self.subTest(sql=sql):
                with self.assertRaises(MigrationSQLParseError):
                    split_sql_statements(sql)

    def test_rejects_delimiter_directive_explicitly(self) -> None:
        for sql in (
            "DELIMITER $$\nCREATE PROCEDURE p() SELECT 1$$",
            "-- comment before directive;\n delimiter //\nSELECT 1//",
        ):
            with self.subTest(sql=sql):
                with self.assertRaisesRegex(
                    MigrationSQLParseError,
                    "DELIMITER",
                ):
                    split_sql_statements(sql)

    def test_all_repository_migrations_have_supported_statement_starts(
        self,
    ) -> None:
        allowed_starts = {
            "ALTER",
            "CREATE",
            "DEALLOCATE",
            "DELETE",
            "DROP",
            "EXECUTE",
            "INSERT",
            "PREPARE",
            "SET",
            "UPDATE",
        }
        paths = sorted(MIGRATIONS_DIR.glob("*.sql"))
        migration_numbers = {
            int(path.name.split("_", 1)[0]) for path in paths
        }
        self.assertTrue(set(range(1, 18)).issubset(migration_numbers))

        for path in paths:
            with self.subTest(path=path.name):
                statements = split_sql_statements(
                    path.read_text(encoding="utf-8")
                )
                self.assertTrue(statements)
                for statement in statements:
                    self.assertTrue(statement.strip())
                    first_word = re.match(
                        r"[A-Za-z]+",
                        statement.lstrip(),
                    )
                    self.assertIsNotNone(first_word, statement)
                    self.assertIn(
                        first_word.group(0).upper(),
                        allowed_starts,
                        statement,
                    )

    def test_migration_017_preserves_prepare_execute_deallocate_order(
        self,
    ) -> None:
        path = MIGRATIONS_DIR / "017_daily_schedule_ledger.sql"
        statements = split_sql_statements(path.read_text(encoding="utf-8"))
        relevant = [
            statement
            for statement in statements
            if "daily_ledger_stmt" in statement
        ]

        self.assertTrue(relevant)
        self.assertEqual(0, len(relevant) % 3)
        for offset in range(0, len(relevant), 3):
            self.assertRegex(
                relevant[offset],
                r"(?is)^PREPARE\s+daily_ledger_stmt\s+FROM\s+@ddl$",
            )
            self.assertRegex(
                relevant[offset + 1],
                r"(?is)^EXECUTE\s+daily_ledger_stmt$",
            )
            self.assertRegex(
                    relevant[offset + 2],
                    r"(?is)^DEALLOCATE\s+PREPARE\s+daily_ledger_stmt$",
                )


class MySQLMigrationSafetyTests(unittest.TestCase):
    @staticmethod
    def _valid_session_facts() -> dict[str, object]:
        return {
            "server_version": "8.0.45",
            "version_comment": "MySQL Community Server - GPL",
            "sql_mode": (
                "STRICT_TRANS_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,"
                "NO_ENGINE_SUBSTITUTION"
            ),
            "session_time_zone": "+00:00",
            "foreign_key_checks": 1,
        }

    @staticmethod
    def _valid_upgrade_state() -> dict[str, object]:
        return {
            "tables": {
                "t_scheme_runs": {
                    "row_count": 0,
                    "columns": set(),
                },
                "t_scheme_predictions": {
                    "row_count": 0,
                    "columns": set(),
                },
            },
            "violations": {},
            "fingerprint": {
                "columns": {},
                "indexes": {},
                "foreign_keys": {},
                "checks": {},
            },
        }

    def test_exposes_mysql_session_contract_validator(self) -> None:
        self.assertTrue(
            callable(
                getattr(
                    migration_runner,
                    "validate_mysql_session_contract",
                    None,
                )
            )
        )

    def test_mysql_session_contract_accepts_required_safety_modes(
        self,
    ) -> None:
        migration_runner.validate_mysql_session_contract(
            self._valid_session_facts()
        )

    def test_mysql_session_contract_rejects_unsupported_server(
        self,
    ) -> None:
        invalid = self._valid_session_facts()
        invalid["server_version"] = "8.0.15"

        with self.assertRaisesRegex(RuntimeError, "8\\.0\\.16"):
            migration_runner.validate_mysql_session_contract(invalid)

    def test_mysql_session_contract_rejects_mariadb(self) -> None:
        invalid = self._valid_session_facts()
        invalid["server_version"] = "10.11.8-MariaDB"
        invalid["version_comment"] = "MariaDB Server"

        with self.assertRaisesRegex(RuntimeError, "MySQL"):
            migration_runner.validate_mysql_session_contract(invalid)

    def test_mysql_session_contract_rejects_each_missing_sql_mode(
        self,
    ) -> None:
        invalid_modes = (
            "NO_ZERO_DATE,NO_ZERO_IN_DATE",
            "STRICT_TRANS_TABLES,NO_ZERO_IN_DATE",
            "STRICT_ALL_TABLES,NO_ZERO_DATE",
        )
        for sql_mode in invalid_modes:
            with self.subTest(sql_mode=sql_mode):
                invalid = self._valid_session_facts()
                invalid["sql_mode"] = sql_mode
                with self.assertRaisesRegex(RuntimeError, "sql_mode"):
                    migration_runner.validate_mysql_session_contract(
                        invalid
                    )

    def test_mysql_session_contract_rejects_non_utc_or_disabled_fk(
        self,
    ) -> None:
        invalid_timezone = self._valid_session_facts()
        invalid_timezone["session_time_zone"] = "+08:00"
        with self.assertRaisesRegex(RuntimeError, "UTC"):
            migration_runner.validate_mysql_session_contract(
                invalid_timezone
            )

        invalid_fk = self._valid_session_facts()
        invalid_fk["foreign_key_checks"] = 0
        with self.assertRaisesRegex(RuntimeError, "foreign_key_checks"):
            migration_runner.validate_mysql_session_contract(invalid_fk)

    def test_exposes_connection_session_preflight(self) -> None:
        self.assertTrue(
            callable(
                getattr(
                    migration_runner,
                    "preflight_migration_session",
                    None,
                )
            )
        )

    def test_connection_session_preflight_skips_non_mysql(self) -> None:
        connection = Mock()
        connection.dialect = SimpleNamespace(name="sqlite")

        migration_runner.preflight_migration_session(connection)

        connection.execute.assert_not_called()

    def test_connection_session_preflight_reads_mysql_session_facts(
        self,
    ) -> None:
        connection = Mock()
        connection.dialect = SimpleNamespace(name="mysql")
        connection.execute.return_value.mappings.return_value.one.return_value = (
            self._valid_session_facts()
        )

        migration_runner.preflight_migration_session(connection)

        connection.execute.assert_called_once()

    def test_connection_session_preflight_propagates_unsafe_contract(
        self,
    ) -> None:
        connection = Mock()
        connection.dialect = SimpleNamespace(name="mysql")
        invalid = self._valid_session_facts()
        invalid["foreign_key_checks"] = 0
        connection.execute.return_value.mappings.return_value.one.return_value = (
            invalid
        )

        with self.assertRaisesRegex(RuntimeError, "foreign_key_checks"):
            migration_runner.preflight_migration_session(connection)

    def test_exposes_daily_ledger_schema_fingerprint_contract(
        self,
    ) -> None:
        self.assertTrue(
            callable(
                getattr(
                    migration_runner,
                    "expected_daily_ledger_schema_fingerprint",
                    None,
                )
            )
        )
        self.assertTrue(
            callable(
                getattr(
                    migration_runner,
                    "validate_daily_ledger_schema_fingerprint",
                    None,
                )
            )
        )
        self.assertTrue(
            callable(
                getattr(
                    migration_runner,
                    "read_daily_ledger_schema_fingerprint",
                    None,
                )
            )
        )
        self.assertTrue(
            callable(
                getattr(
                    migration_runner,
                    "preflight_daily_ledger_upgrade",
                    None,
                )
            )
        )
        self.assertTrue(
            callable(
                getattr(
                    migration_runner,
                    "validate_daily_ledger_upgrade_state",
                    None,
                )
            )
        )
        self.assertTrue(
            callable(
                getattr(
                    migration_runner,
                    "read_daily_ledger_upgrade_state",
                    None,
                )
            )
        )

    def test_daily_ledger_schema_fingerprint_accepts_exact_definition(
        self,
    ) -> None:
        expected = (
            migration_runner.expected_daily_ledger_schema_fingerprint()
        )

        migration_runner.validate_daily_ledger_schema_fingerprint(
            expected,
            allow_missing=False,
        )

    def test_schema_fingerprint_covers_incremental_017_objects(
        self,
    ) -> None:
        expected = (
            migration_runner.expected_daily_ledger_schema_fingerprint()
        )

        self.assertTrue(
            {
                "t_schedule_occurrences.sla_accepted_target_count",
                "t_schedule_occurrences.failure_message",
            }.issubset(expected["columns"])
        )
        self.assertTrue(
            {
                "t_input_generations.idx_input_generation_native",
                "t_schedule_item_targets.idx_schedule_target_identity",
            }.issubset(expected["indexes"])
        )

    def test_check_clause_canonicalizer_handles_mysql_charset_escapes(
        self,
    ) -> None:
        mysql_clause = (
            r"((`failure_code` is null) or "
            r"(`failure_code` in (_utf8mb4\'DATA\',"
            r"_utf8mb4\'TIMEOUT\')))"
        )
        declared_clause = (
            "failure_code IS NULL OR "
            "failure_code IN ('DATA','TIMEOUT')"
        )

        self.assertEqual(
            migration_runner._canonical_check_clause(mysql_clause),
            migration_runner._canonical_check_clause(declared_clause),
        )

    def test_reads_exact_daily_ledger_schema_fingerprint(self) -> None:
        expected = (
            migration_runner.expected_daily_ledger_schema_fingerprint()
        )
        connection = _FingerprintConnection(expected)

        observed = (
            migration_runner.read_daily_ledger_schema_fingerprint(
                connection
            )
        )

        self.assertEqual(observed, expected)
        self.assertEqual(len(connection.executed_sql), 5)

    def test_daily_ledger_schema_fingerprint_rejects_missing_postcondition(
        self,
    ) -> None:
        migration_runner.validate_daily_ledger_schema_fingerprint(
            {
                "columns": {},
                "indexes": {},
                "foreign_keys": {},
                "checks": {},
            },
            allow_missing=True,
        )
        with self.assertRaisesRegex(RuntimeError, "missing"):
            migration_runner.validate_daily_ledger_schema_fingerprint(
                {
                    "columns": {},
                    "indexes": {},
                    "foreign_keys": {},
                    "checks": {},
                },
                allow_missing=False,
            )

    def test_daily_ledger_schema_fingerprint_rejects_definition_drift(
        self,
    ) -> None:
        expected = (
            migration_runner.expected_daily_ledger_schema_fingerprint()
        )
        mutations = (
            ("columns", "t_schedule_items.state", "column_type", "varchar(32)"),
            (
                "columns",
                "t_schedule_items.resource_class",
                "default",
                "cpu_standard",
            ),
            (
                "indexes",
                "t_scheme_runs.uk_scheme_runs_execution_token",
                "unique",
                False,
            ),
            (
                "foreign_keys",
                "fk_scheme_run_schedule_item",
                "delete_rule",
                "CASCADE",
            ),
            (
                "checks",
                "ck_schedule_occurrence_feature_date",
                "enforced",
                False,
            ),
            (
                "checks",
                "ck_schedule_item_failure_code",
                "clause",
                "failure_code is null",
            ),
        )
        for category, name, field, value in mutations:
            with self.subTest(
                category=category,
                name=name,
                field=field,
            ):
                drifted = copy.deepcopy(expected)
                drifted[category][name][field] = value
                with self.assertRaisesRegex(RuntimeError, "drift"):
                    migration_runner.validate_daily_ledger_schema_fingerprint(
                        drifted,
                        allow_missing=False,
                    )

    def test_daily_ledger_schema_fingerprint_rejects_untracked_policy_json_drift(
        self,
    ) -> None:
        observed = copy.deepcopy(
            migration_runner.expected_daily_ledger_schema_fingerprint()
        )
        observed["columns"][
            "t_schedule_occurrences.policy_json"
        ] = {
            "column_type": "text",
            "nullable": "yes",
            "default": None,
            "extra": "",
        }

        with self.assertRaisesRegex(RuntimeError, "policy_json"):
            migration_runner.validate_daily_ledger_schema_fingerprint(
                observed,
                allow_missing=False,
            )

    def test_daily_ledger_schema_fingerprint_rejects_extra_ledger_unique_index(
        self,
    ) -> None:
        observed = copy.deepcopy(
            migration_runner.expected_daily_ledger_schema_fingerprint()
        )
        observed["indexes"][
            "t_schedule_items.uk_unexpected_runtime"
        ] = {
            "unique": True,
            "columns": ("occurrence_id", "runtime_type"),
            "visible": True,
        }

        with self.assertRaisesRegex(
            RuntimeError,
            "uk_unexpected_runtime",
        ):
            migration_runner.validate_daily_ledger_schema_fingerprint(
                observed,
                allow_missing=False,
            )

    def test_daily_ledger_schema_fingerprint_checks_table_engine(self) -> None:
        observed = copy.deepcopy(
            migration_runner.expected_daily_ledger_schema_fingerprint()
        )
        observed["tables"]["t_schedule_items"]["engine"] = "myisam"

        with self.assertRaisesRegex(RuntimeError, "t_schedule_items"):
            migration_runner.validate_daily_ledger_schema_fingerprint(
                observed,
                allow_missing=False,
            )

    def test_daily_ledger_schema_fingerprint_checks_index_prefix(self) -> None:
        observed = copy.deepcopy(
            migration_runner.expected_daily_ledger_schema_fingerprint()
        )
        observed["indexes"][
            "t_schedule_item_targets.uk_schedule_target_registry"
        ]["sub_parts"] = (None, 8)

        with self.assertRaisesRegex(
            RuntimeError,
            "uk_schedule_target_registry",
        ):
            migration_runner.validate_daily_ledger_schema_fingerprint(
                observed,
                allow_missing=False,
            )

    def test_daily_ledger_schema_fingerprint_checks_fk_update_rule(self) -> None:
        observed = copy.deepcopy(
            migration_runner.expected_daily_ledger_schema_fingerprint()
        )
        observed["foreign_keys"]["fk_schedule_target_prediction"][
            "update_rule"
        ] = "cascade"

        with self.assertRaisesRegex(
            RuntimeError,
            "fk_schedule_target_prediction",
        ):
            migration_runner.validate_daily_ledger_schema_fingerprint(
                observed,
                allow_missing=False,
            )

    def test_daily_ledger_schema_allows_unrelated_legacy_run_index(
        self,
    ) -> None:
        observed = copy.deepcopy(
            migration_runner.expected_daily_ledger_schema_fingerprint()
        )
        observed["indexes"]["t_scheme_runs.idx_legacy_status"] = {
            "unique": False,
            "columns": ("status",),
            "sub_parts": (None,),
            "orders": ("a",),
            "index_type": "btree",
            "visible": True,
            "expressions": (None,),
        }

        migration_runner.validate_daily_ledger_schema_fingerprint(
            observed,
            allow_missing=False,
        )

    def test_daily_ledger_upgrade_state_accepts_clean_fresh_schema(
        self,
    ) -> None:
        migration_runner.validate_daily_ledger_upgrade_state(
            self._valid_upgrade_state()
        )

    def test_daily_ledger_upgrade_state_requires_base_tables(self) -> None:
        state = self._valid_upgrade_state()
        del state["tables"]["t_scheme_runs"]

        with self.assertRaisesRegex(RuntimeError, "t_scheme_runs"):
            migration_runner.validate_daily_ledger_upgrade_state(state)

    def test_daily_ledger_upgrade_rejects_nonempty_table_missing_identity(
        self,
    ) -> None:
        state = self._valid_upgrade_state()
        fingerprint = copy.deepcopy(
            migration_runner.expected_daily_ledger_schema_fingerprint()
        )
        for column_name in (
            "resource_class",
            "internal_workers",
            "release_offset_minutes",
        ):
            del fingerprint["columns"][
                f"t_schedule_items.{column_name}"
            ]
        state["tables"]["t_schedule_items"] = {
            "row_count": 1,
            "columns": {
                identity.split(".", 1)[1]
                for identity in fingerprint["columns"]
                if identity.startswith("t_schedule_items.")
            },
        }
        state["fingerprint"] = fingerprint

        with self.assertRaisesRegex(RuntimeError, "resource_class"):
            migration_runner.validate_daily_ledger_upgrade_state(state)

    def test_daily_ledger_upgrade_rejects_untrusted_feature_date(
        self,
    ) -> None:
        for violation in (
            "occurrence_feature_date_null_or_zero",
            "occurrence_feature_date_order",
        ):
            with self.subTest(violation=violation):
                state = self._valid_upgrade_state()
                fingerprint = copy.deepcopy(
                    migration_runner.expected_daily_ledger_schema_fingerprint()
                )
                state["tables"]["t_schedule_occurrences"] = {
                    "row_count": 1,
                    "columns": {
                        identity.split(".", 1)[1]
                        for identity in fingerprint["columns"]
                        if identity.startswith(
                            "t_schedule_occurrences."
                        )
                    },
                }
                state["fingerprint"] = fingerprint
                state["violations"][violation] = 1
                with self.assertRaisesRegex(RuntimeError, violation):
                    migration_runner.validate_daily_ledger_upgrade_state(
                        state
                    )

    def test_daily_ledger_upgrade_rejects_any_late_ddl_violation(
        self,
    ) -> None:
        for violation in (
            "duplicate_schedule_attempt",
            "duplicate_execution_token",
            "orphan_schedule_item",
            "invalid_failure_code",
        ):
            with self.subTest(violation=violation):
                state = self._valid_upgrade_state()
                state["violations"][violation] = 1
                with self.assertRaisesRegex(RuntimeError, violation):
                    migration_runner.validate_daily_ledger_upgrade_state(
                        state
                    )

    def test_daily_ledger_upgrade_rejects_missing_existing_unique_key(
        self,
    ) -> None:
        state = self._valid_upgrade_state()
        expected = (
            migration_runner.expected_daily_ledger_schema_fingerprint()
        )
        occurrence_columns = {
            identity.split(".", 1)[1]
            for identity in expected["columns"]
            if identity.startswith("t_schedule_occurrences.")
        }
        state["tables"]["t_schedule_occurrences"] = {
            "row_count": 0,
            "columns": occurrence_columns,
        }
        state["fingerprint"] = copy.deepcopy(expected)
        del state["fingerprint"]["indexes"][
            "t_schedule_occurrences.uk_schedule_occurrence"
        ]

        with self.assertRaisesRegex(RuntimeError, "uk_schedule_occurrence"):
            migration_runner.validate_daily_ledger_upgrade_state(state)

    def test_daily_ledger_preflight_skips_non_mysql(self) -> None:
        connection = SimpleNamespace(
            dialect=SimpleNamespace(name="sqlite")
        )
        with patch.object(
            migration_runner,
            "read_daily_ledger_upgrade_state",
            side_effect=AssertionError("SQLite must not query MySQL I_S"),
        ):
            migration_runner.preflight_daily_ledger_upgrade(connection)

    def test_daily_ledger_preflight_reads_then_validates_state(
        self,
    ) -> None:
        connection = SimpleNamespace(
            dialect=SimpleNamespace(name="mysql")
        )
        state = self._valid_upgrade_state()
        with (
            patch.object(
                migration_runner,
                "read_daily_ledger_upgrade_state",
                return_value=state,
            ) as read_state,
            patch.object(
                migration_runner,
                "validate_daily_ledger_upgrade_state",
            ) as validate_state,
        ):
            migration_runner.preflight_daily_ledger_upgrade(connection)

        read_state.assert_called_once_with(connection)
        validate_state.assert_called_once_with(state)

    def test_reads_clean_daily_ledger_upgrade_state(self) -> None:
        connection = _UpgradeStateConnection(
            table_names=("t_scheme_runs", "t_scheme_predictions"),
            columns={
                "t_scheme_runs": ("run_id",),
                "t_scheme_predictions": ("id",),
            },
        )
        empty_fingerprint = {
            "columns": {},
            "indexes": {},
            "foreign_keys": {},
            "checks": {},
        }
        with patch.object(
            migration_runner,
            "read_daily_ledger_schema_fingerprint",
            return_value=empty_fingerprint,
        ):
            state = migration_runner.read_daily_ledger_upgrade_state(
                connection
            )

        self.assertEqual(
            state,
            {
                "tables": {
                    "t_scheme_runs": {
                        "row_count": 0,
                        "columns": {"run_id"},
                    },
                    "t_scheme_predictions": {
                        "row_count": 0,
                        "columns": {"id"},
                    },
                },
                "violations": {},
                "fingerprint": empty_fingerprint,
            },
        )

    def test_upgrade_state_digest_counts_every_relevant_table(
        self,
    ) -> None:
        table_names = (
            "t_input_generations",
            "t_schedule_occurrences",
            "t_schedule_items",
            "t_schedule_item_targets",
            "t_scheduler_heartbeat",
            "t_scheme_runs",
            "t_scheme_predictions",
        )
        row_counts = {
            table_name: position
            for position, table_name in enumerate(
                table_names,
                start=1,
            )
        }
        connection = _UpgradeStateConnection(
            table_names=table_names,
            columns={
                table_name: ("id",) for table_name in table_names
            },
            row_counts=row_counts,
        )
        with patch.object(
            migration_runner,
            "read_daily_ledger_schema_fingerprint",
            return_value={
                "tables": {},
                "columns": {},
                "indexes": {},
                "foreign_keys": {},
                "checks": {},
            },
        ):
            state = migration_runner.read_daily_ledger_upgrade_state(
                connection
            )

        self.assertEqual(
            row_counts,
            {
                table_name: definition["row_count"]
                for table_name, definition in state["tables"].items()
            },
        )

    def test_reads_daily_ledger_upgrade_data_violations(self) -> None:
        violation_names = (
            "occurrence_feature_date_null_or_zero",
            "occurrence_feature_date_order",
            "item_resource_class_null",
            "item_internal_workers_null",
            "item_release_offset_minutes_null",
            "duplicate_schedule_attempt",
            "duplicate_execution_token",
            "orphan_schedule_item",
            "invalid_failure_code",
        )
        connection = _UpgradeStateConnection(
            table_names=(
                "t_schedule_occurrences",
                "t_schedule_items",
                "t_scheme_runs",
                "t_scheme_predictions",
            ),
            columns={
                "t_schedule_occurrences": (
                    "occurrence_id",
                    "predict_date",
                    "feature_date",
                    "failure_code",
                ),
                "t_schedule_items": (
                    "item_id",
                    "resource_class",
                    "internal_workers",
                    "release_offset_minutes",
                    "failure_code",
                ),
                "t_scheme_runs": (
                    "run_id",
                    "schedule_item_id",
                    "attempt_no",
                    "execution_token",
                    "failure_code",
                ),
                "t_scheme_predictions": ("id",),
            },
            row_counts={
                "t_schedule_occurrences": 3,
                "t_schedule_items": 4,
            },
            violations={
                name: 1 for name in violation_names
            },
        )
        with patch.object(
            migration_runner,
            "read_daily_ledger_schema_fingerprint",
            return_value={
                "columns": {},
                "indexes": {},
                "foreign_keys": {},
                "checks": {},
            },
        ):
            state = migration_runner.read_daily_ledger_upgrade_state(
                connection
            )

        self.assertEqual(
            state["violations"],
            {name: 1 for name in violation_names},
        )


class MigrationRunnerSafetyTests(unittest.TestCase):
    @staticmethod
    def _migration_file(
        directory: str,
        name: str,
        sql: str,
    ) -> Path:
        path = Path(directory) / name
        path.write_text(sql, encoding="utf-8")
        return path

    @staticmethod
    def _release_manifest_fixture(
        directory: str,
    ) -> tuple[Path, Path]:
        fixture_dir = Path(directory)
        entries: list[dict[str, object]] = []
        for source in sorted(MIGRATIONS_DIR.glob("*.sql")):
            target = fixture_dir / source.name
            content = source.read_bytes()
            target.write_bytes(content)
            entries.append(
                {
                    "version": int(source.name[:3]),
                    "filename": source.name,
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        manifest_path = fixture_dir / "release_manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "migrations": entries,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return fixture_dir, manifest_path

    @staticmethod
    def _applying_017_history() -> tuple[
        list[migration_runner.PreparedMigration],
        list[dict[str, object]],
    ]:
        manifest = migration_runner.prepare_migration_files(
            sorted(MIGRATIONS_DIR.glob("*.sql"))
        )
        history = [
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
                "state": (
                    "APPLYING"
                    if migration.version == 17
                    else "APPLIED"
                ),
                "baseline_bootstrap": (
                    1 if migration.version <= 16 else 0
                ),
            }
            for migration in manifest
            if migration.version <= 17
        ]
        return manifest, history

    @staticmethod
    def _complete_017_upgrade_state() -> dict[str, object]:
        fingerprint = copy.deepcopy(
            migration_runner
            .expected_daily_ledger_schema_fingerprint()
        )
        tables = {
            table_name: {
                "row_count": 0,
                "columns": {
                    identity.split(".", 1)[1]
                    for identity in fingerprint["columns"]
                    if identity.startswith(f"{table_name}.")
                },
            }
            for table_name in (
                "t_input_generations",
                "t_schedule_occurrences",
                "t_schedule_items",
                "t_schedule_item_targets",
                "t_scheduler_heartbeat",
                "t_scheme_runs",
                "t_scheme_predictions",
            )
        }
        return {
            "tables": tables,
            "violations": {},
            "fingerprint": fingerprint,
        }

    @staticmethod
    def _pre_ddl_017_upgrade_state() -> dict[str, object]:
        return {
            "tables": {
                "t_scheme_runs": {
                    "row_count": 0,
                    "columns": {"run_id"},
                },
                "t_scheme_predictions": {
                    "row_count": 0,
                    "columns": {"id"},
                },
            },
            "violations": {},
            "fingerprint": {
                "tables": {},
                "columns": {},
                "indexes": {},
                "foreign_keys": {},
                "checks": {},
            },
        }

    def _inspect_017_state(
        self,
        upgrade_state: dict[str, object],
        *,
        history_mutator: object | None = None,
    ) -> dict[str, object]:
        manifest, history = self._applying_017_history()
        if history_mutator is not None:
            history_mutator(history)
        return migration_runner.build_applying_017_inspection(
            manifest=manifest,
            history=history,
            database_identity={
                "database_name": "bond_db_recovery_test",
                "server_uuid": "test-server-uuid",
            },
            upgrade_state=upgrade_state,
        )

    def test_applying_017_inspect_classifies_complete_schema(
        self,
    ) -> None:
        inspection = self._inspect_017_state(
            self._complete_017_upgrade_state()
        )

        self.assertEqual("COMPLETE", inspection["classification"])
        self.assertIsNone(inspection["reason"])
        self.assertRegex(
            str(inspection["state_digest"]),
            r"^[0-9a-f]{64}$",
        )

    def test_applying_017_inspect_classifies_crash_before_ddl(
        self,
    ) -> None:
        inspection = self._inspect_017_state(
            self._pre_ddl_017_upgrade_state()
        )

        self.assertEqual(
            "COMPATIBLE_PARTIAL",
            inspection["classification"],
        )
        self.assertIsNone(inspection["reason"])

    def test_applying_017_inspect_accepts_only_four_nullable_transitions(
        self,
    ) -> None:
        transition_columns = {
            "t_schedule_occurrences.feature_date",
            "t_schedule_items.resource_class",
            "t_schedule_items.internal_workers",
            "t_schedule_items.release_offset_minutes",
        }
        for identity in transition_columns:
            with self.subTest(identity=identity):
                state = self._complete_017_upgrade_state()
                state["fingerprint"]["columns"][identity][
                    "nullable"
                ] = "yes"

                inspection = self._inspect_017_state(state)

                self.assertEqual(
                    "COMPATIBLE_PARTIAL",
                    inspection["classification"],
                )

        unsafe = self._complete_017_upgrade_state()
        unsafe["fingerprint"]["columns"][
            "t_schedule_items.resource_class"
        ].update({"nullable": "yes", "default": "legacy"})

        inspection = self._inspect_017_state(unsafe)

        self.assertEqual("UNSAFE", inspection["classification"])
        self.assertIn("resource_class", str(inspection["reason"]))

    def test_applying_017_inspect_rejects_history_identity_drift(
        self,
    ) -> None:
        def checksum_drift(
            history: list[dict[str, object]],
        ) -> None:
            history[-1]["sha256"] = "0" * 64

        def filename_drift(
            history: list[dict[str, object]],
        ) -> None:
            history[-1]["filename"] = "017_other.sql"

        def noncontiguous(
            history: list[dict[str, object]],
        ) -> None:
            del history[8]

        def wrong_applying(
            history: list[dict[str, object]],
        ) -> None:
            history[-2]["state"] = "APPLYING"
            history[-1]["state"] = "APPLIED"

        for mutator in (
            checksum_drift,
            filename_drift,
            noncontiguous,
            wrong_applying,
        ):
            with self.subTest(mutator=mutator.__name__):
                inspection = self._inspect_017_state(
                    self._complete_017_upgrade_state(),
                    history_mutator=mutator,
                )

                self.assertEqual(
                    "UNSAFE",
                    inspection["classification"],
                )
                self.assertIsNotNone(inspection["reason"])

    def test_applying_017_state_digest_binds_database_and_state(
        self,
    ) -> None:
        manifest, history = self._applying_017_history()
        state = self._pre_ddl_017_upgrade_state()
        first = migration_runner.build_applying_017_inspection(
            manifest=manifest,
            history=history,
            database_identity={
                "database_name": "bond_db_a",
                "server_uuid": "server-a",
            },
            upgrade_state=state,
        )
        repeated = migration_runner.build_applying_017_inspection(
            manifest=manifest,
            history=list(reversed(copy.deepcopy(history))),
            database_identity={
                "server_uuid": "server-a",
                "database_name": "bond_db_a",
            },
            upgrade_state=copy.deepcopy(state),
        )
        other_database = migration_runner.build_applying_017_inspection(
            manifest=manifest,
            history=history,
            database_identity={
                "database_name": "bond_db_b",
                "server_uuid": "server-a",
            },
            upgrade_state=state,
        )

        self.assertEqual(
            first["state_digest"],
            repeated["state_digest"],
        )
        self.assertNotEqual(
            first["state_digest"],
            other_database["state_digest"],
        )

    def test_applying_017_recovery_rejects_stale_digest(self) -> None:
        owner = Mock()
        owner_lock = Mock()
        owner_lock.__enter__ = Mock(return_value=owner)
        owner_lock.__exit__ = Mock(return_value=False)
        with (
            patch.object(
                migration_runner,
                "_migration_owner_connection",
                return_value=owner_lock,
                create=True,
            ),
            patch.object(
                migration_runner,
                "_read_applying_017_inspection",
                return_value={
                    "classification": "COMPLETE",
                    "state_digest": "a" * 64,
                    "reason": None,
                },
                create=True,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "state digest"):
                migration_runner.recover_applying_migration_017(
                    Mock(),
                    sorted(MIGRATIONS_DIR.glob("*.sql")),
                    expected_state_digest="b" * 64,
                )

    def test_complete_017_recovery_marks_only_without_replay(
        self,
    ) -> None:
        engine = Mock()
        inspection = {
            "classification": "COMPLETE",
            "state_digest": "a" * 64,
            "reason": None,
        }
        owner_lock = Mock()
        owner_lock.__enter__ = Mock(return_value=Mock())
        owner_lock.__exit__ = Mock(return_value=False)
        with (
            patch.object(
                migration_runner,
                "_migration_owner_connection",
                return_value=owner_lock,
                create=True,
            ),
            patch.object(
                migration_runner,
                "_read_applying_017_inspection",
                return_value=inspection,
                create=True,
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
            result = migration_runner.recover_applying_migration_017(
                engine,
                sorted(MIGRATIONS_DIR.glob("*.sql")),
                expected_state_digest="a" * 64,
            )

        self.assertEqual("APPLIED", result["recovery_outcome"])
        replay.assert_not_called()
        mark_applied.assert_called_once()

    def test_partial_017_recovery_replays_then_marks(self) -> None:
        engine = Mock()
        events: list[str] = []
        partial = {
            "classification": "COMPATIBLE_PARTIAL",
            "state_digest": "a" * 64,
            "reason": None,
        }
        complete = {
            "classification": "COMPLETE",
            "state_digest": "b" * 64,
            "reason": None,
        }
        owner = Mock()
        owner.rollback.side_effect = lambda: events.append(
            "release_metadata_snapshot"
        )
        owner_lock = Mock()
        owner_lock.__enter__ = Mock(return_value=owner)
        owner_lock.__exit__ = Mock(return_value=False)
        with (
            patch.object(
                migration_runner,
                "_migration_owner_connection",
                return_value=owner_lock,
                create=True,
            ),
            patch.object(
                migration_runner,
                "_read_applying_017_inspection",
                side_effect=(partial, complete),
                create=True,
            ),
            patch.object(
                migration_runner,
                "_execute_prepared_migration_files",
                side_effect=lambda *_args: events.append("replay"),
            ) as replay,
            patch.object(
                migration_runner,
                "_mark_migration_applied",
            ) as mark_applied,
        ):
            result = migration_runner.recover_applying_migration_017(
                engine,
                sorted(MIGRATIONS_DIR.glob("*.sql")),
                expected_state_digest="a" * 64,
            )

        self.assertEqual("APPLIED", result["recovery_outcome"])
        replay.assert_called_once()
        mark_applied.assert_called_once()
        self.assertEqual(
            [
                "release_metadata_snapshot",
                "replay",
                "release_metadata_snapshot",
            ],
            events,
        )

    def test_failed_partial_017_recovery_does_not_mark_applied(
        self,
    ) -> None:
        partial = {
            "classification": "COMPATIBLE_PARTIAL",
            "state_digest": "a" * 64,
            "reason": None,
        }
        owner_lock = Mock()
        owner_lock.__enter__ = Mock(return_value=Mock())
        owner_lock.__exit__ = Mock(return_value=False)
        with (
            patch.object(
                migration_runner,
                "_migration_owner_connection",
                return_value=owner_lock,
                create=True,
            ),
            patch.object(
                migration_runner,
                "_read_applying_017_inspection",
                return_value=partial,
                create=True,
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
                migration_runner.recover_applying_migration_017(
                    Mock(),
                    sorted(MIGRATIONS_DIR.glob("*.sql")),
                    expected_state_digest="a" * 64,
                )

        mark_applied.assert_not_called()

    def test_applying_017_cli_inspect_is_read_only(self) -> None:
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
                "inspect_applying_migration_017",
                return_value=inspection,
                create=True,
            ) as inspect,
            patch.object(
                migration_cli,
                "apply_pending_migration_files",
            ) as apply_pending,
            patch("builtins.print") as print_output,
        ):
            migration_cli.main(["--inspect-applying-017"])

        inspect.assert_called_once()
        apply_pending.assert_not_called()
        print_output.assert_called_once()
        engine.dispose.assert_called_once_with()

    def test_applying_017_cli_recovery_requires_apply_and_digest(
        self,
    ) -> None:
        invalid_commands = (
            ["--recover-applying-017"],
            [
                "--recover-applying-017",
                "--state-digest",
                "a" * 64,
            ],
            ["--recover-applying-017", "--apply"],
        )
        for argv in invalid_commands:
            with self.subTest(argv=argv):
                with patch.object(
                    migration_cli,
                    "create_engine_from_env",
                ) as create_engine:
                    with self.assertRaises(SystemExit) as raised:
                        migration_cli.main(argv)

                self.assertEqual(2, raised.exception.code)
                create_engine.assert_not_called()

    def test_applying_017_cli_routes_fenced_recovery(self) -> None:
        engine = Mock()
        recovery = {
            "recovery_outcome": "APPLIED",
            "initial_classification": "COMPLETE",
        }
        digest = "a" * 64
        with (
            patch.object(
                migration_cli,
                "create_engine_from_env",
                return_value=engine,
            ),
            patch.object(
                migration_cli,
                "recover_applying_migration_017",
                return_value=recovery,
                create=True,
            ) as recover,
            patch.object(
                migration_cli,
                "apply_pending_migration_files",
            ) as apply_pending,
            patch("builtins.print"),
        ):
            migration_cli.main(
                [
                    "--recover-applying-017",
                    "--apply",
                    "--state-digest",
                    digest,
                ]
            )

        recover.assert_called_once()
        self.assertEqual(
            digest,
            recover.call_args.kwargs["expected_state_digest"],
        )
        apply_pending.assert_not_called()
        engine.dispose.assert_called_once_with()

    def test_reviewed_migration_017_bytes_are_unchanged(self) -> None:
        path = MIGRATIONS_DIR / "017_daily_schedule_ledger.sql"

        self.assertEqual(
            migration_runner.DAILY_LEDGER_MIGRATION_SHA256,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    def test_release_manifest_exactly_binds_current_001_through_018(
        self,
    ) -> None:
        prepared = migration_runner.validate_release_migration_manifest(
            sorted(MIGRATIONS_DIR.glob("*.sql")),
            manifest_path=(
                migration_runner.RELEASE_MIGRATION_MANIFEST_PATH
            ),
        )

        self.assertEqual(
            list(range(1, 19)),
            [row.version for row in prepared],
        )
        self.assertEqual(
            [f"{version:03d}" for version in range(1, 19)],
            [row.path.name[:3] for row in prepared],
        )

    def test_release_manifest_drift_stops_every_cli_mode_before_engine(
        self,
    ) -> None:
        scenarios = (
            (
                "tampered_017_apply",
                ["--apply"],
                lambda root: (
                    root / "017_daily_schedule_ledger.sql"
                ).write_bytes(
                    (
                        root / "017_daily_schedule_ledger.sql"
                    ).read_bytes()
                    + b"\n-- tampered\n"
                ),
            ),
            (
                "tampered_001_inspect",
                ["--inspect-applying-017"],
                lambda root: (root / "001_init.sql").write_bytes(
                    (root / "001_init.sql").read_bytes()
                    + b"\n-- tampered\n"
                ),
            ),
            (
                "unknown_file_recover",
                [
                    "--recover-applying-017",
                    "--apply",
                    "--state-digest",
                    "a" * 64,
                ],
                lambda root: (
                    root / "999_unreviewed.sql"
                ).write_text("SELECT 1;\n", encoding="utf-8"),
            ),
            (
                "missing_file_apply",
                ["--apply"],
                lambda root: (root / "016_dual_runtime.sql").unlink(),
            ),
        )
        for name, argv, mutate in scenarios:
            with (
                self.subTest(name=name),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root, manifest_path = self._release_manifest_fixture(tmp)
                mutate(root)
                with (
                    patch.object(migration_cli, "MIGRATIONS_DIR", root),
                    patch.object(
                        migration_cli,
                        "RELEASE_MIGRATION_MANIFEST_PATH",
                        manifest_path,
                        create=True,
                    ),
                    patch.object(
                        migration_cli,
                        "create_engine_from_env",
                    ) as create_engine,
                    patch.object(
                        migration_cli,
                        "apply_pending_migration_files",
                    ),
                    patch.object(
                        migration_cli,
                        "inspect_applying_migration_017",
                        return_value={},
                    ),
                    patch.object(
                        migration_cli,
                        "recover_applying_migration_017",
                        return_value={},
                    ),
                ):
                    with self.assertRaisesRegex(
                        migration_cli.MigrationHistoryError,
                        "release migration manifest",
                    ):
                        migration_cli.main(argv)

                create_engine.assert_not_called()

    def test_exposes_apply_runner_and_partial_apply_error(self) -> None:
        self.assertTrue(
            callable(
                getattr(
                    migration_runner,
                    "apply_migration_files",
                    None,
                )
            )
        )

    def test_main_uses_pending_history_runner(self) -> None:
        engine = Mock()
        with (
            patch.object(
                migration_cli,
                "create_engine_from_env",
                return_value=engine,
            ),
            patch.object(
                migration_cli,
                "apply_pending_migration_files",
            ) as apply_pending,
        ):
            migration_cli.main(["--apply"])

        apply_pending.assert_called_once()
        self.assertIs(apply_pending.call_args.args[0], engine)
        engine.dispose.assert_called_once_with()

    def test_help_exits_before_opening_database(self) -> None:
        with (
            patch.object(
                sys,
                "argv",
                ["scripts/apply_migrations.py", "--help"],
            ),
            patch.object(
                migration_cli,
                "create_engine_from_env",
            ) as create_engine,
            patch.object(
                migration_cli,
                "apply_pending_migration_files",
            ) as apply_pending,
        ):
            with self.assertRaises(SystemExit) as raised:
                migration_cli.main()

        self.assertEqual(raised.exception.code, 0)
        create_engine.assert_not_called()
        apply_pending.assert_not_called()

    def test_apply_flag_is_required_before_opening_database(self) -> None:
        for argv in (
            ["scripts/apply_migrations.py"],
            ["scripts/apply_migrations.py", "--typo"],
        ):
            with self.subTest(argv=argv):
                with (
                    patch.object(sys, "argv", argv),
                    patch.object(
                        migration_cli,
                        "create_engine_from_env",
                    ) as create_engine,
                    patch.object(
                        migration_cli,
                        "apply_pending_migration_files",
                    ) as apply_pending,
                ):
                    with self.assertRaises(SystemExit) as raised:
                        migration_cli.main()

                self.assertEqual(raised.exception.code, 2)
                create_engine.assert_not_called()
                apply_pending.assert_not_called()

    def test_public_apply_runner_cannot_bypass_pending_history(
        self,
    ) -> None:
        engine = Mock()
        paths = [MIGRATIONS_DIR / "017_daily_schedule_ledger.sql"]
        with patch.object(
            migration_runner,
            "apply_pending_migration_files",
        ) as apply_pending:
            migration_runner.apply_migration_files(engine, paths)

        apply_pending.assert_called_once_with(engine, paths)

    def test_history_table_is_created_on_lock_owner_connection(
        self,
    ) -> None:
        connection = Mock()
        connection.dialect = SimpleNamespace(name="mysql")
        with patch.object(
            migration_runner,
            "preflight_migration_session",
        ):
            migration_runner._create_migration_history_table(
                connection
            )

        connection.execute.assert_called_once()
        connection.begin.assert_not_called()

    def test_history_schema_rejects_same_name_definition_drift(
        self,
    ) -> None:
        schema = copy.deepcopy(
            migration_runner._expected_migration_history_schema()
        )
        schema["columns"]["sha256"] = (
            "varchar(64)",
            "no",
            None,
            "",
        )

        with self.assertRaisesRegex(RuntimeError, "history schema"):
            migration_runner._validate_migration_history_schema(schema)
        self.assertTrue(
            issubclass(
                migration_runner.MigrationPartialApplyError,
                RuntimeError,
            )
        )

    def test_history_selects_only_pending_migration_suffix(self) -> None:
        manifest = migration_runner.prepare_migration_files(
            sorted(MIGRATIONS_DIR.glob("*.sql"))
        )
        history = [
            {
                "version": migration.version,
                "filename": migration.path.name,
                "sha256": migration.sha256,
                "state": "APPLIED",
            }
            for migration in manifest
            if migration.version <= 16
        ]

        pending = migration_runner.select_pending_migrations(
            manifest,
            history,
        )

        self.assertEqual(
            [
                "017_daily_schedule_ledger.sql",
                "018_schedule_run_started_at_nullable.sql",
            ],
            [migration.path.name for migration in pending],
        )

    def test_history_rejects_applied_checksum_drift(self) -> None:
        manifest = migration_runner.prepare_migration_files(
            sorted(MIGRATIONS_DIR.glob("*.sql"))
        )
        first = manifest[0]

        with self.assertRaisesRegex(RuntimeError, "checksum"):
            migration_runner.select_pending_migrations(
                manifest,
                [
                    {
                        "version": first.version,
                        "filename": first.path.name,
                        "sha256": "0" * 64,
                        "state": "APPLIED",
                    }
                ],
            )

    def test_history_rejects_interrupted_applying_migration(self) -> None:
        manifest = migration_runner.prepare_migration_files(
            sorted(MIGRATIONS_DIR.glob("*.sql"))
        )
        first = manifest[0]

        with self.assertRaisesRegex(RuntimeError, "APPLYING"):
            migration_runner.select_pending_migrations(
                manifest,
                [
                    {
                        "version": first.version,
                        "filename": first.path.name,
                        "sha256": first.sha256,
                        "state": "APPLYING",
                    }
                ],
            )

    def test_history_rejects_noncontiguous_applied_prefix(self) -> None:
        manifest = migration_runner.prepare_migration_files(
            sorted(MIGRATIONS_DIR.glob("*.sql"))
        )

        with self.assertRaisesRegex(RuntimeError, "contiguous"):
            migration_runner.select_pending_migrations(
                manifest,
                [
                    {
                        "version": manifest[1].version,
                        "filename": manifest[1].path.name,
                        "sha256": manifest[1].sha256,
                        "state": "APPLIED",
                    }
                ],
            )

    def test_legacy_016_baseline_accepts_exact_terminal_markers(
        self,
    ) -> None:
        migration_runner.validate_legacy_016_baseline(
            migration_runner.expected_legacy_016_baseline()
        )

    def test_legacy_016_manifest_has_reviewed_digest(self) -> None:
        def canonical(value: object) -> object:
            if isinstance(value, Mapping):
                return {
                    str(key): canonical(item)
                    for key, item in sorted(
                        value.items(),
                        key=lambda pair: str(pair[0]),
                    )
                }
            if isinstance(value, (set, frozenset)):
                return sorted(canonical(item) for item in value)
            if isinstance(value, tuple):
                return [canonical(item) for item in value]
            return value

        manifest = migration_runner.expected_legacy_016_baseline()
        payload = json.dumps(
            canonical(manifest),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        self.assertEqual(
            "cdd6b65ea9e2e9153c9d17953a8ee3b5"
            "23c6f91818d97866d73dbf53b963a109",
            hashlib.sha256(payload).hexdigest(),
        )
        self.assertEqual(17, len(manifest["tables"]))
        self.assertEqual(238, len(manifest["columns"]))
        self.assertEqual(69, len(manifest["indexes"]))
        self.assertEqual(6, len(manifest["foreign_keys"]))
        self.assertEqual(1, len(manifest["views"]))

    def test_reads_complete_legacy_016_terminal_fingerprint(
        self,
    ) -> None:
        expected = migration_runner.expected_legacy_016_baseline()
        connection = _LegacyBaselineConnection(expected)

        observed = migration_runner.read_legacy_016_baseline(
            connection
        )

        self.assertEqual(expected, observed)
        self.assertEqual(
            set(
                migration_runner
                .legacy_016_data_violation_queries()
            ),
            set(connection.data_probes),
        )

    def test_legacy_016_structural_drift_stops_before_data_probes(
        self,
    ) -> None:
        expected = migration_runner.expected_legacy_016_baseline()

        def drift(rows: dict[str, list[dict[str, object]]]) -> None:
            target = next(
                row
                for row in rows["columns"]
                if row["table_name"] == "t_scheme_predictions"
                and row["column_name"] == "target_tenor"
            )
            target["column_type"] = "varchar(16)"

        connection = _LegacyBaselineConnection(
            expected,
            structural_mutator=drift,
        )

        with self.assertRaisesRegex(RuntimeError, "target_tenor"):
            migration_runner.read_legacy_016_baseline(connection)

        self.assertEqual([], connection.data_probes)

    def test_legacy_016_baseline_rejects_missing_table(self) -> None:
        baseline = copy.deepcopy(
            migration_runner.expected_legacy_016_baseline()
        )
        baseline["tables"].remove("t_scheme_monthly_actuals")

        with self.assertRaisesRegex(
            RuntimeError,
            "t_scheme_monthly_actuals",
        ):
            migration_runner.validate_legacy_016_baseline(baseline)

    def test_legacy_016_baseline_rejects_definition_drift(self) -> None:
        baseline = copy.deepcopy(
            migration_runner.expected_legacy_016_baseline()
        )
        baseline["columns"]["t_scheme_runs.runtime_type"][
            "column_type"
        ] = "varchar(16)"

        with self.assertRaisesRegex(RuntimeError, "runtime_type"):
            migration_runner.validate_legacy_016_baseline(baseline)

    def test_legacy_016_baseline_rejects_target_tenor_definition_drift(
        self,
    ) -> None:
        baseline = copy.deepcopy(
            migration_runner.expected_legacy_016_baseline()
        )
        baseline["columns"]["t_scheme_predictions.target_tenor"] = {
            "column_type": "varchar(16)",
            "nullable": "no",
            "default": None,
            "extra": "",
            "character_set": "utf8mb4",
            "collation": "utf8mb4_0900_ai_ci",
            "ordinal": 5,
        }

        with self.assertRaisesRegex(RuntimeError, "target_tenor"):
            migration_runner.validate_legacy_016_baseline(baseline)

    def test_legacy_016_baseline_rejects_latest_view_definition_drift(
        self,
    ) -> None:
        baseline = copy.deepcopy(
            migration_runner.expected_legacy_016_baseline()
        )
        baseline["views"] = {
            "v_latest_backtest_run": {
                "definition": "select * from t_backtest_runs",
                "columns": ("id",),
                "check_option": "none",
                "security_type": "definer",
            }
        }

        with self.assertRaisesRegex(
            RuntimeError,
            "v_latest_backtest_run",
        ):
            migration_runner.validate_legacy_016_baseline(baseline)

    def test_legacy_016_baseline_rejects_unexpected_table_column(
        self,
    ) -> None:
        baseline = copy.deepcopy(
            migration_runner.expected_legacy_016_baseline()
        )
        baseline["columns"]["t_scheme_predictions.untracked_column"] = {
            "column_type": "int",
            "nullable": "yes",
            "default": None,
            "extra": "",
            "character_set": None,
            "collation": None,
            "ordinal": 999,
        }

        with self.assertRaisesRegex(RuntimeError, "untracked_column"):
            migration_runner.validate_legacy_016_baseline(baseline)

    def test_legacy_016_baseline_rejects_original_index_drift(
        self,
    ) -> None:
        baseline = copy.deepcopy(
            migration_runner.expected_legacy_016_baseline()
        )
        baseline["indexes"][
            "t_scheme_predictions.idx_scheme_predict_date"
        ] = {
            "unique": True,
            "columns": ("predict_date", "scheme_id"),
            "sub_parts": (None, None),
            "orders": ("a", "a"),
            "index_type": "btree",
            "visible": True,
            "expressions": (None, None),
        }

        with self.assertRaisesRegex(
            RuntimeError,
            "idx_scheme_predict_date",
        ):
            migration_runner.validate_legacy_016_baseline(baseline)

    def test_legacy_016_baseline_rejects_foreign_key_drift(
        self,
    ) -> None:
        baseline = copy.deepcopy(
            migration_runner.expected_legacy_016_baseline()
        )
        baseline["foreign_keys"] = {
            "fk_backtest_predictions_run": {
                "table": "t_backtest_predictions",
                "columns": ("run_id",),
                "referenced_table": "t_backtest_runs",
                "referenced_columns": ("id",),
                "delete_rule": "restrict",
                "update_rule": "no action",
            }
        }

        with self.assertRaisesRegex(
            RuntimeError,
            "fk_backtest_predictions_run",
        ):
            migration_runner.validate_legacy_016_baseline(baseline)

    def test_legacy_016_baseline_rejects_data_violation(self) -> None:
        baseline = copy.deepcopy(
            migration_runner.expected_legacy_016_baseline()
        )
        baseline["violations"] = {
            "registry_identity_missing": 1,
        }

        with self.assertRaisesRegex(
            RuntimeError,
            "registry_identity_missing",
        ):
            migration_runner.validate_legacy_016_baseline(baseline)

    def test_legacy_016_data_contract_covers_mutating_migrations(
        self,
    ) -> None:
        queries = migration_runner.legacy_016_data_violation_queries()

        self.assertEqual(
            {
                "backtest_run_identity_drift",
                "registry_composite_identity_drift",
                "registry_deployed_at_missing",
                "registry_task_contract_drift",
                "registry_tenors_snapshot_drift",
                "t_scheme_registry_runtime_type_invalid",
                "t_scheme_runs_runtime_type_invalid",
                "t_scheme_versions_runtime_type_invalid",
                "target_registry_seed_drift",
            },
            set(queries),
        )
        self.assertIn("backtest_run_id <> id", queries[
            "backtest_run_identity_drift"
        ])
        self.assertIn("json_length(tenors) <> 1", queries[
            "registry_tenors_snapshot_drift"
        ].lower())
        self.assertIn("1Y国债活跃", queries[
            "target_registry_seed_drift"
        ])

    def test_parses_all_files_before_opening_database_connection(
        self,
    ) -> None:
        connection = _MigrationConnection()
        engine = _MigrationEngine(connection)
        with tempfile.TemporaryDirectory() as directory:
            valid = self._migration_file(
                directory,
                "016_valid.sql",
                "CREATE TABLE valid_table (id INT);",
            )
            invalid = self._migration_file(
                directory,
                "017_invalid.sql",
                "CREATE TABLE invalid_table (value VARCHAR(8) DEFAULT 'x);",
            )

            with self.assertRaisesRegex(
                MigrationSQLParseError,
                "017_invalid.sql",
            ):
                migration_runner._apply_migration_files_without_history(
                    engine,
                    [valid, invalid],
                )

        self.assertEqual(engine.begin_calls, 0)
        self.assertEqual(connection.executed, [])

    def test_017_runner_orders_preflight_execution_and_postcondition(
        self,
    ) -> None:
        connection = _MigrationConnection()
        engine = _MigrationEngine(connection)
        events: list[str] = []
        expected_fingerprint = {
            "columns": {},
            "indexes": {},
            "foreign_keys": {},
            "checks": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            migration = self._migration_file(
                directory,
                "017_daily_schedule_ledger.sql",
                "CREATE TABLE test_ledger (id INT);",
            )
            with (
                patch.object(
                    migration_runner,
                    "preflight_migration_session",
                    side_effect=lambda _connection: events.append(
                        "session"
                    ),
                ),
                patch.object(
                    migration_runner,
                    "preflight_daily_ledger_upgrade",
                    side_effect=lambda _connection: events.append(
                        "ledger_preflight"
                    ),
                ),
                patch.object(
                    migration_runner,
                    "read_daily_ledger_schema_fingerprint",
                    side_effect=lambda _connection: (
                        events.append("read_postcondition")
                        or expected_fingerprint
                    ),
                ),
                patch.object(
                    migration_runner,
                    "validate_daily_ledger_schema_fingerprint",
                    side_effect=lambda fingerprint, *, allow_missing: (
                        events.append(
                            f"validate_postcondition:{allow_missing}"
                        )
                    ),
                ),
            ):
                migration_runner._apply_migration_files_without_history(
                    engine,
                    [migration],
                )

        self.assertEqual(
            events,
            [
                "session",
                "ledger_preflight",
                "session",
                "read_postcondition",
                "validate_postcondition:False",
            ],
        )
        self.assertEqual(
            connection.executed,
            [
                "SET @bond_factor_lab_migration_runner_017 = "
                ":runner_guard",
                "CREATE TABLE test_ledger (id INT)",
            ],
        )
        self.assertEqual(engine.begin_calls, 2)

    def test_session_preflight_failure_prevents_any_statement(
        self,
    ) -> None:
        connection = _MigrationConnection()
        engine = _MigrationEngine(connection)
        with tempfile.TemporaryDirectory() as directory:
            migration = self._migration_file(
                directory,
                "016_before_ledger.sql",
                "CREATE TABLE blocked_table (id INT);",
            )
            with patch.object(
                migration_runner,
                "preflight_migration_session",
                side_effect=migration_runner.MigrationPreflightError(
                    "unsafe session"
                ),
            ):
                with self.assertRaisesRegex(
                    migration_runner.MigrationPreflightError,
                    "unsafe session",
                ):
                    migration_runner._apply_migration_files_without_history(
                        engine,
                        [migration],
                    )

        self.assertEqual(connection.executed, [])

    def test_non_mysql_017_skips_mysql_ledger_pre_and_postcondition(
        self,
    ) -> None:
        connection = _MigrationConnection(dialect_name="sqlite")
        engine = _MigrationEngine(connection)
        with tempfile.TemporaryDirectory() as directory:
            migration = self._migration_file(
                directory,
                "017_daily_schedule_ledger.sql",
                "CREATE TABLE portable_table (id INT);",
            )
            with (
                patch.object(
                    migration_runner,
                    "preflight_daily_ledger_upgrade",
                    side_effect=AssertionError(
                        "non-MySQL must skip ledger preflight"
                    ),
                ),
                patch.object(
                    migration_runner,
                    "read_daily_ledger_schema_fingerprint",
                    side_effect=AssertionError(
                        "non-MySQL must skip ledger postcondition"
                    ),
                ),
            ):
                migration_runner._apply_migration_files_without_history(
                    engine,
                    [migration],
                )

        self.assertEqual(
            connection.executed,
            ["CREATE TABLE portable_table (id INT)"],
        )
        self.assertEqual(engine.begin_calls, 1)

    def test_017_postcondition_failure_reports_partial_mysql_ddl(
        self,
    ) -> None:
        connection = _MigrationConnection()
        engine = _MigrationEngine(connection)
        with tempfile.TemporaryDirectory() as directory:
            migration = self._migration_file(
                directory,
                "017_daily_schedule_ledger.sql",
                "CREATE TABLE partial_table (id INT);",
            )
            with (
                patch.object(
                    migration_runner,
                    "preflight_migration_session",
                ),
                patch.object(
                    migration_runner,
                    "preflight_daily_ledger_upgrade",
                ),
                patch.object(
                    migration_runner,
                    "read_daily_ledger_schema_fingerprint",
                    return_value={},
                ),
                patch.object(
                    migration_runner,
                    "validate_daily_ledger_schema_fingerprint",
                    side_effect=migration_runner.MigrationPreflightError(
                        "missing schema object"
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    migration_runner.MigrationPartialApplyError,
                    "implicit commit.*partially applied",
                ):
                    migration_runner._apply_migration_files_without_history(
                        engine,
                        [migration],
                    )

    def test_017_execution_failure_still_runs_postcondition_and_is_partial(
        self,
    ) -> None:
        connection = _MigrationConnection(fail_on="BROKEN")
        engine = _MigrationEngine(connection)
        postcondition = Mock(
            return_value={
                "columns": {},
                "indexes": {},
                "foreign_keys": {},
                "checks": {},
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            migration = self._migration_file(
                directory,
                "017_daily_schedule_ledger.sql",
                "CREATE TABLE first_table (id INT);"
                "ALTER TABLE first_table BROKEN;",
            )
            with (
                patch.object(
                    migration_runner,
                    "preflight_migration_session",
                ),
                patch.object(
                    migration_runner,
                    "preflight_daily_ledger_upgrade",
                ),
                patch.object(
                    migration_runner,
                    "read_daily_ledger_schema_fingerprint",
                    postcondition,
                ),
                patch.object(
                    migration_runner,
                    "validate_daily_ledger_schema_fingerprint",
                    side_effect=migration_runner.MigrationPreflightError(
                        "incomplete ledger"
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    migration_runner.MigrationPartialApplyError,
                    "017_daily_schedule_ledger.sql.*partially applied",
                ):
                    migration_runner._apply_migration_files_without_history(
                        engine,
                        [migration],
                    )

        postcondition.assert_called_once_with(connection)
        self.assertEqual(engine.begin_calls, 2)

    def test_non_ddl_failure_is_not_misreported_as_partial_apply(
        self,
    ) -> None:
        connection = _MigrationConnection(fail_on="BROKEN")
        engine = _MigrationEngine(connection)
        with tempfile.TemporaryDirectory() as directory:
            migration = self._migration_file(
                directory,
                "016_data_guard.sql",
                "SET @value = BROKEN;",
            )
            with patch.object(
                migration_runner,
                "preflight_migration_session",
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected execution failure",
                ) as raised:
                    migration_runner._apply_migration_files_without_history(
                        engine,
                        [migration],
                    )

        self.assertNotIsInstance(
            raised.exception,
            migration_runner.MigrationPartialApplyError,
        )


if __name__ == "__main__":
    unittest.main()
