from __future__ import annotations

from dataclasses import dataclass

import pytest

from tests.integration.conftest import (
    DISPOSABLE_MARKER_VALUE,
    _require_expected_mysql_server,
)


@dataclass
class _ScalarResult:
    value: str

    def scalar_one(self) -> str:
        return self.value


@dataclass
class _MappingResult:
    rows: list[dict[str, str]]

    def mappings(self) -> _MappingResult:
        return self

    def all(self) -> list[dict[str, str]]:
        return self.rows


class _Connection:
    def __init__(self, *, marker_rows: list[dict[str, str]], server_uuid: str):
        self.marker_rows = marker_rows
        self.server_uuid = server_uuid
        self.statements: list[str] = []

    def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if "server_identity" in sql:
            return _MappingResult(self.marker_rows)
        if "@@server_uuid" in sql:
            return _ScalarResult(self.server_uuid)
        raise AssertionError(f"unexpected SQL: {sql}")


def test_mysql_integration_guard_rejects_missing_server_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BFL_TEST_MYSQL_EXPECTED_SERVER_UUID", "uuid-1")
    connection = _Connection(marker_rows=[], server_uuid="uuid-1")

    with pytest.raises(pytest.fail.Exception, match="marker is invalid"):
        _require_expected_mysql_server(connection)  # type: ignore[arg-type]

    assert all(
        "CREATE" not in sql and "DROP" not in sql
        for sql in connection.statements
    )


def test_mysql_integration_guard_accepts_bound_marker_and_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BFL_TEST_MYSQL_EXPECTED_SERVER_UUID", "uuid-1")
    connection = _Connection(
        marker_rows=[
            {"marker": DISPOSABLE_MARKER_VALUE, "server_uuid": "uuid-1"}
        ],
        server_uuid="uuid-1",
    )

    _require_expected_mysql_server(connection)  # type: ignore[arg-type]

    assert len(connection.statements) == 2
