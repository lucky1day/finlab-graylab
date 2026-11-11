from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, make_url

from migrations.runner import MIGRATIONS_DIR, apply_pending_migration_files


DISPOSABLE_MARKER_SCHEMA = "bfl_disposable_test_marker"
DISPOSABLE_MARKER_VALUE = "BOND_FACTOR_LAB_DISPOSABLE_TEST_MYSQL"


def _prepare_runtime_contract_schema(engine: Engine) -> None:
    """经正式迁移入口（基线加增量）建立集成测试 schema。"""
    apply_pending_migration_files(engine, sorted(MIGRATIONS_DIR.glob("*.sql")))


def _require_expected_mysql_server(connection: Connection) -> None:
    """在任何测试 DDL 前核对预置标记和显式批准的实例身份。"""
    expected = os.environ["BFL_TEST_MYSQL_EXPECTED_SERVER_UUID"].strip()
    if not expected:
        pytest.fail("BFL_TEST_MYSQL_EXPECTED_SERVER_UUID cannot be empty")
    try:
        rows = connection.execute(
            text(
                "SELECT marker, server_uuid "
                f"FROM `{DISPOSABLE_MARKER_SCHEMA}`.server_identity"
            )
        ).mappings().all()
    except Exception as exc:
        pytest.fail(
            "disposable MySQL server marker is unavailable; refusing DDL: "
            f"{type(exc).__name__}"
        )
    actual = str(
        connection.execute(text("SELECT @@server_uuid")).scalar_one()
    ).strip()
    if len(rows) != 1:
        pytest.fail("disposable MySQL server marker is invalid; refusing DDL")
    marker = str(rows[0].get("marker", "")).strip()
    marker_uuid = str(rows[0].get("server_uuid", "")).strip()
    if (
        marker != DISPOSABLE_MARKER_VALUE
        or marker_uuid != actual
        or actual != expected
    ):
        pytest.fail("disposable MySQL server identity mismatch; refusing DDL")


@contextmanager
def _disposable_database(*, migrate: bool) -> Iterator[Engine]:
    """创建（可选迁移）并最终删除一个独占的临时 MySQL 数据库。"""
    raw_url = os.environ["BFL_TEST_MYSQL_ADMIN_URL"]
    try:
        configured_url = make_url(raw_url)
    except Exception as exc:
        pytest.fail(f"BFL_TEST_MYSQL_ADMIN_URL is invalid: {type(exc).__name__}")
    if configured_url.drivername != "mysql+pymysql":
        pytest.fail("BFL_TEST_MYSQL_ADMIN_URL must use mysql+pymysql")
    if configured_url.database not in {None, "", "mysql"}:
        pytest.fail(
            "BFL_TEST_MYSQL_ADMIN_URL must not name an application database; "
            "use /mysql or omit the database"
        )

    admin_engine = create_engine(
        configured_url.set(database="mysql"),
        future=True,
        pool_pre_ping=True,
        isolation_level="AUTOCOMMIT",
    )
    database_name = f"bfl_test_{uuid.uuid4().hex[:16]}"
    application_engine: Engine | None = None
    created = False
    try:
        with admin_engine.connect() as connection:
            _require_expected_mysql_server(connection)
            connection.execute(
                text(
                    f"CREATE DATABASE `{database_name}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                )
            )
        created = True
        application_engine = create_engine(
            configured_url.set(database=database_name),
            future=True,
            pool_pre_ping=True,
            connect_args={
                "init_command": "SET SESSION time_zone = '+00:00'",
            },
        )
        if migrate:
            _prepare_runtime_contract_schema(application_engine)
        yield application_engine
    finally:
        if application_engine is not None:
            application_engine.dispose()
        if created:
            with admin_engine.connect() as connection:
                _require_expected_mysql_server(connection)
                connection.execute(text(f"DROP DATABASE `{database_name}`"))
        admin_engine.dispose()


@pytest.fixture(scope="session")
def mysql_test_engine() -> Iterator[Engine]:
    """本次测试会话共享的、已迁移到最新版本的临时数据库。"""
    with _disposable_database(migrate=True) as engine:
        yield engine


@pytest.fixture
def empty_mysql_engine() -> Iterator[Engine]:
    """每个测试独占的、尚未迁移的空临时数据库。"""
    with _disposable_database(migrate=False) as engine:
        yield engine
