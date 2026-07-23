from __future__ import annotations

import math
import re

import pytest
from pymysql.connections import Connection as PyMySQLConnection
from sqlalchemy import event

from backend import db
from shared.db_config import DatabaseConfig


def test_dashboard_engine_uses_bounded_pymysql_and_query_timeouts(
    monkeypatch,
) -> None:
    config = DatabaseConfig(
        user="dashboard_reader",
        password="secret",
        host="db.internal",
        port=3307,
        database="bond_test",
        charset="utf8mb4",
    )

    class StopBeforeDbApiConnect(Exception):
        pass

    db.get_dashboard_engine.cache_clear()
    monkeypatch.setattr(db.DatabaseConfig, "from_env", lambda: config)
    try:
        engine = db.get_dashboard_engine()
        assert db.get_dashboard_engine() is engine

        captured_connect_args: dict[str, object] = {}

        def capture_connect_args(
            dialect,
            connection_record,
            positional_args,
            keyword_args,
        ) -> None:
            captured_connect_args.update(keyword_args)
            raise StopBeforeDbApiConnect

        event.listen(engine, "do_connect", capture_connect_args)
        with pytest.raises(StopBeforeDbApiConnect):
            engine.connect()
    finally:
        if "engine" in locals():
            engine.dispose()
        db.get_dashboard_engine.cache_clear()

    url = engine.url
    assert url.drivername == "mysql+pymysql"
    assert url.username == "dashboard_reader"
    assert url.password == "secret"
    assert url.host == "db.internal"
    assert url.port == 3307
    assert url.database == "bond_test"
    assert url.query["charset"] == "utf8mb4"

    for name in ("connect_timeout", "read_timeout", "write_timeout"):
        timeout = captured_connect_args[name]
        assert isinstance(timeout, (int, float))
        assert math.isfinite(timeout)
        assert 0.0 < timeout < 3.0

    query_timeout = re.fullmatch(
        r"SET SESSION MAX_EXECUTION_TIME=(\d+)",
        captured_connect_args["init_command"],
    )
    assert query_timeout is not None
    assert 0 < int(query_timeout.group(1)) < 3_000

    # defer_connect 走真实 PyMySQL 参数校验而不建立网络连接，防止测试只在
    # SQLAlchemy mock 边界通过，却把拼错或类型不兼容的 connect_args 带到生产。
    pymysql_connection = PyMySQLConnection(
        defer_connect=True,
        **captured_connect_args,
    )
    try:
        assert pymysql_connection.connect_timeout == pytest.approx(0.5)
        assert pymysql_connection._read_timeout == pytest.approx(0.75)
        assert pymysql_connection._write_timeout == pytest.approx(0.5)
    finally:
        pymysql_connection._force_close()


def test_default_backend_engine_keeps_scheduler_factory_defaults(
    monkeypatch,
) -> None:
    sentinel_engine = object()
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_scheduler_engine(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel_engine

    db.get_engine.cache_clear()
    monkeypatch.setattr(
        db,
        "create_engine_from_env",
        fake_scheduler_engine,
    )
    try:
        assert db.get_engine() is sentinel_engine
    finally:
        db.get_engine.cache_clear()

    assert calls == [((), {})]
