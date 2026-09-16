from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, make_url

from migrations.runner import (
    MIGRATIONS_DIR,
    _execute_prepared_migration_files,
    _registry_owner_authority,
    prepare_migration_files,
)


DISPOSABLE_MARKER_SCHEMA = "bfl_disposable_test_marker"
DISPOSABLE_MARKER_VALUE = "BOND_FACTOR_LAB_DISPOSABLE_TEST_MYSQL"


def _prepare_runtime_contract_schema(engine: Engine) -> None:
    """经迁移执行器建立集成测试所需 schema，并满足 021 权威围栏。"""
    prepared = prepare_migration_files(sorted(MIGRATIONS_DIR.glob("*.sql")))
    before_owner = prepared[:20]
    owner_and_runtime = prepared[20:]
    _execute_prepared_migration_files(
        engine,
        [(item.path, item.statements) for item in before_owner],
    )

    owner_authority = _registry_owner_authority(prepared[20])
    seed_rows = []
    for scheme_id in sorted(owner_authority):
        base_scheme_id, target = scheme_id.rsplit("__", 1)
        raw_base, raw_horizon = base_scheme_id.rsplit("__h", 1)
        seed_rows.append(
            {
                "scheme_id": scheme_id,
                "base_scheme_id": raw_base,
                "name": scheme_id,
                "horizon": int(raw_horizon),
                "tenors": f'["{target}"]',
                "frequency": "daily",
                "target_tenor": target,
                "schedule_cron": "0 0 * * *",
            }
        )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO t_scheme_registry
                    (scheme_id, base_scheme_id, name, horizon, tenors,
                     frequency, target_tenor, schedule_cron)
                VALUES
                    (:scheme_id, :base_scheme_id, :name, :horizon,
                     CAST(:tenors AS JSON), :frequency, :target_tenor,
                     :schedule_cron)
                """
            ),
            seed_rows,
        )
    _execute_prepared_migration_files(
        engine,
        [(item.path, item.statements) for item in owner_and_runtime],
    )


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


@pytest.fixture(scope="session")
def mysql_test_engine() -> Iterator[Engine]:
    """创建、迁移并最终删除本次测试独占的临时 MySQL 数据库。"""
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
