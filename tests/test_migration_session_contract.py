from __future__ import annotations

import pytest

from migrations.runner import (
    MigrationPreflightError,
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
