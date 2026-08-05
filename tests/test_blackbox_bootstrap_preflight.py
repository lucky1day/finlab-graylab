"""Blackbox certification bootstrap preflight 的仓库级回归测试。"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scheduler import repository


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value


class _ConnectionContext:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def __enter__(self) -> object:
        return self._connection

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _BootstrapLockConnection:
    def execute(self, statement, params=None) -> _ScalarResult:
        statement_text = str(statement)
        if "GET_LOCK" in statement_text or "RELEASE_LOCK" in statement_text:
            return _ScalarResult(1)
        raise AssertionError(f"unexpected bootstrap lock statement: {statement_text}")


class _BootstrapPreflightConnection:
    _COUNT_TABLE_RE = re.compile(r"FROM `(?P<table>[^`]+)`")

    def __init__(self, *, schema: str, table_counts: dict[str, int]) -> None:
        self._schema = schema
        self._table_counts = table_counts
        self.statements: list[str] = []

    def execute(self, statement, params=None) -> _ScalarResult:
        statement_text = str(statement)
        self.statements.append(statement_text)
        if "SELECT DATABASE()" in statement_text:
            return _ScalarResult(self._schema)
        count_match = self._COUNT_TABLE_RE.search(statement_text)
        if "SELECT COUNT(*)" in statement_text and count_match is not None:
            return _ScalarResult(self._table_counts.get(count_match.group("table"), 0))
        raise AssertionError(f"unexpected bootstrap preflight statement: {statement_text}")


class _BootstrapPreflightEngine:
    def __init__(self, *, schema: str, table_counts: dict[str, int]) -> None:
        self.lock_connection = _BootstrapLockConnection()
        self.transaction_connection = _BootstrapPreflightConnection(
            schema=schema,
            table_counts=table_counts,
        )

    def connect(self) -> _ConnectionContext:
        return _ConnectionContext(self.lock_connection)

    def begin(self) -> _ConnectionContext:
        return _ConnectionContext(self.transaction_connection)


def _bootstrap_config() -> SimpleNamespace:
    return SimpleNamespace(
        scheme_id="bootstrap_demo",
        scheme_version="bootstrap-version",
        runtime_type="blackbox_v2",
        status="paused",
        version_status="draft",
        tenors=("10Y",),
        horizon=1,
        task_type="T+1",
    )


def _valid_bootstrap_registry_row(cfg: SimpleNamespace) -> dict[str, object]:
    return {
        "scheme_id": repository.registry_scheme_id(
            cfg.scheme_id,
            cfg.horizon,
            cfg.tenors[0],
        ),
        "base_scheme_id": cfg.scheme_id,
        "runtime_type": "blackbox_v2",
        "status": "paused",
        "task_type": cfg.task_type,
        "target_tenor": cfg.tenors[0],
        "horizon": cfg.horizon,
    }


def test_bootstrap_preflight_ignores_nonempty_serving_pointer() -> None:
    cfg = _bootstrap_config()
    engine = _BootstrapPreflightEngine(
        schema="bbv2_cert_g87_pointer",
        table_counts={"t_scheme_serving_pointer": 1},
    )
    version_row = {
        "scheme_id": cfg.scheme_id,
        "scheme_version": cfg.scheme_version,
        "runtime_type": "blackbox_v2",
        "status": "draft",
        "approved_by": None,
        "approved_at": None,
    }

    with (
        patch.object(
            repository,
            "_validate_target_registry_baseline_conn",
            return_value={"status": "passed"},
        ),
        patch.object(repository, "_upsert_scheme_version_conn"),
        patch.object(repository, "_sync_scheme_registry_conn"),
        patch.object(
            repository,
            "_read_scheme_version_conn",
            return_value=version_row,
        ),
        patch.object(
            repository,
            "_read_scheme_registry_rows_conn",
            return_value=[_valid_bootstrap_registry_row(cfg)],
        ),
    ):
        state = repository.bootstrap_blackbox_control_plane(
            engine,
            cfg,
            expected_schema="bbv2_cert_g87_pointer",
        )

    counted_tables = {
        match.group("table")
        for statement in engine.transaction_connection.statements
        if "SELECT COUNT(*)" in statement
        if (match := _BootstrapPreflightConnection._COUNT_TABLE_RE.search(statement))
        is not None
    }
    assert "t_scheme_serving_pointer" not in counted_tables
    assert "t_scheme_serving_pointer" not in state.table_counts
    assert state.table_counts["t_scheme_runs"] == 0


def test_bootstrap_preflight_rejects_nonempty_scheme_runs() -> None:
    engine = _BootstrapPreflightEngine(
        schema="bbv2_cert_g87_runs",
        table_counts={"t_scheme_runs": 1},
    )

    with pytest.raises(RuntimeError, match=r"t_scheme_runs=1"):
        repository.bootstrap_blackbox_control_plane(
            engine,
            _bootstrap_config(),
            expected_schema="bbv2_cert_g87_runs",
        )
