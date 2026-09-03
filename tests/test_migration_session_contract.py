from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from migrations.runner import (
    MigrationPartialApplyError,
    MigrationPreflightError,
    _execute_prepared_migration_files,
    _migration_owner_connection,
    validate_mysql_session_contract,
)


class _LockResult:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value


class _LockConnection:
    def __init__(
        self,
        *,
        dialect_name: str = "mysql",
        acquired: int = 1,
        owned: int | None = 1,
        released: int | None = 1,
        release_error: BaseException | None = None,
    ) -> None:
        self.dialect = SimpleNamespace(name=dialect_name)
        self.acquired = acquired
        self.owned = owned
        self.released = released
        self.release_error = release_error
        self.statements: list[str] = []

    def execute(self, statement: object, parameters=None) -> _LockResult:
        sql = str(statement)
        self.statements.append(sql)
        if "GET_LOCK" in sql:
            return _LockResult(self.acquired)
        if "IS_USED_LOCK" in sql:
            return _LockResult(self.owned)
        if "LOSE_OWNER_FOR_TEST" in sql:
            self.owned = 0
            return _LockResult(1)
        if "RELEASE_LOCK" in sql and self.release_error is not None:
            raise self.release_error
        if "RELEASE_LOCK" in sql:
            return _LockResult(self.released)
        return _LockResult(1)


class _LockEngine:
    def __init__(self, connection: _LockConnection) -> None:
        self.connection = connection

    def connect(self):
        return nullcontext(self.connection)

    def begin(self):
        return nullcontext(self.connection)


def test_migration_owner_lock_acquire_failure_does_not_release() -> None:
    connection = _LockConnection(acquired=0)
    with (
        patch("migrations.runner.preflight_migration_session"),
        pytest.raises(
            MigrationPreflightError,
            match="could not acquire migration owner lock",
        ),
    ):
        with _migration_owner_connection(_LockEngine(connection)):
            pytest.fail("lock body must not run")

    assert sum("GET_LOCK" in sql for sql in connection.statements) == 1
    assert all("RELEASE_LOCK" not in sql for sql in connection.statements)


def test_migration_owner_lock_releases_after_success() -> None:
    connection = _LockConnection()
    with patch("migrations.runner.preflight_migration_session"):
        with _migration_owner_connection(_LockEngine(connection)):
            pass

    assert sum("GET_LOCK" in sql for sql in connection.statements) == 1
    assert sum("RELEASE_LOCK" in sql for sql in connection.statements) == 1


@pytest.mark.parametrize("owned", [0, None])
def test_migration_owner_lock_rejects_lost_ownership(
    owned: int | None,
) -> None:
    connection = _LockConnection(owned=owned)
    with (
        patch("migrations.runner.preflight_migration_session"),
        pytest.raises(
            MigrationPreflightError,
            match="migration owner lock ownership check failed",
        ),
    ):
        with _migration_owner_connection(_LockEngine(connection)):
            pytest.fail("lock body must not run after ownership loss")

    assert sum("IS_USED_LOCK" in sql for sql in connection.statements) == 1
    assert sum("RELEASE_LOCK" in sql for sql in connection.statements) == 1


def test_migration_owner_connection_allows_non_mysql_apply_without_lock() -> None:
    connection = _LockConnection(dialect_name="sqlite")
    with patch("migrations.runner.preflight_migration_session") as preflight:
        with _migration_owner_connection(
            _LockEngine(connection),
            require_mysql=False,
        ) as yielded:
            assert yielded is connection

    preflight.assert_not_called()
    assert connection.statements == []


def test_migration_owner_connection_rejects_non_mysql_by_default() -> None:
    connection = _LockConnection(dialect_name="sqlite")
    with pytest.raises(
        MigrationPreflightError,
        match="inspection/recovery requires MySQL",
    ):
        with _migration_owner_connection(_LockEngine(connection)):
            pytest.fail("non-MySQL inspection body must not run")

    assert connection.statements == []


def test_migration_owner_lock_preserves_body_failure() -> None:
    connection = _LockConnection()
    body_error = RuntimeError("migration body failed")
    with (
        patch("migrations.runner.preflight_migration_session"),
        pytest.raises(RuntimeError, match="migration body failed") as raised,
    ):
        with _migration_owner_connection(_LockEngine(connection)):
            raise body_error

    assert raised.value is body_error
    assert sum("RELEASE_LOCK" in sql for sql in connection.statements) == 1


def test_migration_owner_lock_reports_release_failure_after_success() -> None:
    release_error = RuntimeError("release failed")
    connection = _LockConnection(release_error=release_error)
    with (
        patch("migrations.runner.preflight_migration_session"),
        pytest.raises(RuntimeError, match="release failed") as raised,
    ):
        with _migration_owner_connection(_LockEngine(connection)):
            pass

    assert raised.value is release_error


@pytest.mark.parametrize("released", [0, None])
def test_migration_owner_lock_rejects_non_owner_release_result(
    released: int | None,
) -> None:
    connection = _LockConnection(released=released)
    with (
        patch("migrations.runner.preflight_migration_session"),
        pytest.raises(
            MigrationPreflightError,
            match="migration owner lock release failed",
        ),
    ):
        with _migration_owner_connection(_LockEngine(connection)):
            pass


def test_migration_owner_lock_preserves_body_when_release_result_fails() -> None:
    body_error = RuntimeError("migration body failed")
    connection = _LockConnection(released=0)
    with (
        patch("migrations.runner.preflight_migration_session"),
        pytest.raises(RuntimeError, match="migration body failed") as raised,
    ):
        with _migration_owner_connection(_LockEngine(connection)):
            raise body_error

    assert raised.value is body_error
    assert raised.value.__notes__ == [
        "migration owner lock release failed: MigrationHistoryError: "
        "migration owner lock release failed"
    ]


def test_migration_executor_stops_before_next_statement_after_owner_loss() -> None:
    connection = _LockConnection()
    with (
        patch("migrations.runner.preflight_migration_session"),
        pytest.raises(
            MigrationPreflightError,
            match="migration owner lock ownership check failed",
        ),
    ):
        _execute_prepared_migration_files(
            object(),
            [
                (
                    Path("999_owner_loss_test.sql"),
                    (
                        "SELECT 'LOSE_OWNER_FOR_TEST'",
                        "CREATE TABLE forbidden_after_owner_loss (id INT)",
                    ),
                )
            ],
            owner_connection=connection,
        )

    assert any(
        "LOSE_OWNER_FOR_TEST" in sql for sql in connection.statements
    )
    assert all(
        "forbidden_after_owner_loss" not in sql
        for sql in connection.statements
    )


def test_migration_024_dynamic_ddl_failure_is_reported_as_partial() -> None:
    class FailingConnection(_LockConnection):
        def execute(self, statement: object, parameters=None):
            if "FAIL_AFTER_DYNAMIC_DDL" in str(statement):
                raise RuntimeError("injected dynamic DDL failure")
            return super().execute(statement, parameters)

    connection = FailingConnection()
    with (
        patch("migrations.runner.preflight_migration_session"),
        patch(
            "migrations.runner.read_scheme_prediction_fact_state",
            return_value={},
        ),
        patch(
            "migrations.runner.classify_scheme_prediction_fact_state",
            return_value="COMPATIBLE_PARTIAL",
        ),
        pytest.raises(
            MigrationPartialApplyError,
            match="may be partially applied",
        ),
    ):
        _execute_prepared_migration_files(
            _LockEngine(connection),
            [
                (
                    Path("024_scheme_prediction_fact_source.sql"),
                    ("SELECT 'FAIL_AFTER_DYNAMIC_DDL'",),
                )
            ],
        )


def test_migration_owner_lock_preserves_body_failure_when_release_fails() -> None:
    body_error = RuntimeError("migration body failed")
    connection = _LockConnection(
        release_error=RuntimeError("release failed"),
    )
    with (
        patch("migrations.runner.preflight_migration_session"),
        pytest.raises(RuntimeError, match="migration body failed") as raised,
    ):
        with _migration_owner_connection(_LockEngine(connection)):
            raise body_error

    assert raised.value is body_error
    assert raised.value.__notes__ == [
        "migration owner lock release failed: RuntimeError: release failed"
    ]


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
