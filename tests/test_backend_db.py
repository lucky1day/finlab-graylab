from __future__ import annotations

import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.pool import QueuePool

from backend import db
from shared.db_config import DatabaseConfig


def _config() -> DatabaseConfig:
    return DatabaseConfig.from_mapping(
        {
            "BOND_DB_USER": "http_user",
            "BOND_DB_PASSWORD": "secret",
            "BOND_DB_HOST": "127.0.0.1",
            "BOND_DB_NAME": "bond_http",
        }
    )


def test_http_engines_use_bounded_pools_and_driver_timeouts(monkeypatch) -> None:
    captured: dict = {}
    sentinel = object()

    def capture(_url, **kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(db, "create_engine", capture)
    monkeypatch.setattr(db.event, "listen", lambda *_args: None)

    assert db._create_http_engine(_config(), pool_size=5) is sentinel
    assert captured["pool_size"] == 5
    assert captured["max_overflow"] == 0
    assert captured["pool_timeout"] == 0.25
    assert captured["pool_pre_ping"] is True
    assert captured["connect_args"] == {
        "connect_timeout": 0.5,
        "read_timeout": 2.0,
        "write_timeout": 0.5,
        "init_command": "SET SESSION MAX_EXECUTION_TIME=1000",
    }


def test_saturated_pool_fails_quickly_and_recovers_after_release() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.05,
    )
    held = engine.connect()
    try:
        started_at = time.perf_counter()
        with pytest.raises(SQLAlchemyTimeoutError):
            engine.connect()
        assert time.perf_counter() - started_at < 0.2
    finally:
        held.close()

    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1
    assert engine.pool.checkedout() == 0
    engine.dispose()
