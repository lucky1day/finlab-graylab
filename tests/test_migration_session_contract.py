from __future__ import annotations

import pytest

from migrations.runner import (
    MigrationPreflightError,
    _show_create_mentions_serving_pointer,
    validate_mysql_session_contract,
)


def _safe_facts(lower_case_table_names: object) -> dict[str, object]:
    return {
        "server_version": "8.0.43",
        "version_comment": "MySQL Community Server - GPL",
        "sql_mode": "STRICT_TRANS_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE",
        "session_time_zone": "+00:00",
        "foreign_key_checks": 1,
        "lower_case_table_names": lower_case_table_names,
    }


@pytest.mark.parametrize("mode", [0, 1, 2])
def test_general_migration_accepts_supported_identifier_modes(
    mode: int,
) -> None:
    validate_mysql_session_contract(_safe_facts(mode))


def test_migration_017_keeps_reviewed_constraint_namespace_gate() -> None:
    with pytest.raises(
        MigrationPreflightError,
        match="migration 017 requires lower_case_table_names=2",
    ):
        validate_mysql_session_contract(
            _safe_facts(0),
            require_reviewed_constraint_namespace=True,
        )

    validate_mysql_session_contract(
        _safe_facts(2),
        require_reviewed_constraint_namespace=True,
    )


def test_general_migration_rejects_unknown_identifier_mode() -> None:
    with pytest.raises(
        MigrationPreflightError,
        match="unsupported lower_case_table_names",
    ):
        validate_mysql_session_contract(_safe_facts("unknown"))


class _ShowCreateResult:
    def __init__(self, definition: str) -> None:
        self._definition = definition

    def mappings(self) -> _ShowCreateResult:
        return self

    def one(self) -> dict[str, str]:
        return {"Create Procedure": self._definition}


class _ShowCreateConnection:
    def __init__(self, definition: str) -> None:
        self._definition = definition
        self.statement = ""

    def execute(self, statement: object) -> _ShowCreateResult:
        self.statement = str(statement)
        return _ShowCreateResult(self._definition)


def test_hidden_routine_definition_uses_show_create() -> None:
    connection = _ShowCreateConnection(
        "CREATE PROCEDURE `p` () SELECT 1"
    )
    assert not _show_create_mentions_serving_pointer(
        connection,
        object_type="PROCEDURE",
        object_schema="bond_db",
        object_name="p",
    )
    assert connection.statement == "SHOW CREATE PROCEDURE `bond_db`.`p`"


def test_hidden_definition_detects_actual_pointer_dependency() -> None:
    connection = _ShowCreateConnection(
        "CREATE VIEW `v` AS SELECT * FROM t_scheme_serving_pointer"
    )
    assert _show_create_mentions_serving_pointer(
        connection,
        object_type="VIEW",
        object_schema="bond_db",
        object_name="v",
    )
