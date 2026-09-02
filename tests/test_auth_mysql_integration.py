from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import create_engine, text

from backend.auth import repository
from backend.auth.security import hash_password
from backend.auth.service import AuthError, AuthService
from migrations.auth_022 import classify_auth_schema, read_auth_schema
from migrations.runner import split_sql_statements
from migrations.runner import (
    MIGRATIONS_DIR,
    inspect_applying_migration_022,
    recover_applying_migration_022,
    validate_release_migration_manifest,
)


MYSQL_URL = os.getenv("BFL_TEST_AUTH_MYSQL_URL")
pytestmark = pytest.mark.skipif(
    not MYSQL_URL,
    reason="BFL_TEST_AUTH_MYSQL_URL is required for isolated MySQL tests",
)


def _engine(url: str):
    return create_engine(
        url,
        connect_args={"init_command": "SET SESSION time_zone = '+00:00'"},
    )


def _isolated_url() -> tuple[str, str]:
    assert MYSQL_URL is not None
    parsed = urlsplit(MYSQL_URL)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        pytest.fail("auth MySQL integration tests require a loopback server")
    schema_name = "bfl_auth_022_pytest"
    target = urlunsplit(
        (parsed.scheme, parsed.netloc, f"/{schema_name}", parsed.query, "")
    )
    return schema_name, target


def test_auth_schema_fresh_partial_complete_and_drift_on_mysql8() -> None:
    assert MYSQL_URL is not None
    schema_name, target_url = _isolated_url()
    admin = _engine(MYSQL_URL)
    with admin.connect() as connection:
        version = str(connection.execute(text("SELECT VERSION() ")).scalar_one())
        assert version.startswith("8.0.")
        connection.execute(text(f"DROP DATABASE IF EXISTS `{schema_name}`"))
        connection.execute(
            text(
                f"CREATE DATABASE `{schema_name}` CHARACTER SET utf8mb4 "
                "COLLATE utf8mb4_0900_ai_ci"
            )
        )
        connection.commit()
    target = _engine(target_url)
    try:
        statements = split_sql_statements(
            Path("migrations/022_authentication.sql").read_text(
                encoding="utf-8"
            )
        )
        with target.begin() as connection:
            assert classify_auth_schema(read_auth_schema(connection)) == (
                "COMPATIBLE_PARTIAL"
            )
            connection.execute(text(statements[0]))
            assert classify_auth_schema(read_auth_schema(connection)) == (
                "COMPATIBLE_PARTIAL"
            )
            connection.execute(text(statements[1]))
            assert classify_auth_schema(read_auth_schema(connection)) == (
                "COMPATIBLE_PARTIAL"
            )
            connection.execute(text(statements[2]))
            assert classify_auth_schema(read_auth_schema(connection)) == (
                "COMPLETE"
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE t_schema_migrations (
                        version INT NOT NULL,
                        filename VARCHAR(255) NOT NULL,
                        sha256 CHAR(64) NOT NULL,
                        state ENUM('APPLYING','APPLIED') NOT NULL,
                        baseline_bootstrap TINYINT(1) NOT NULL DEFAULT 0,
                        started_at DATETIME(6) NOT NULL
                            DEFAULT (UTC_TIMESTAMP(6)),
                        applied_at DATETIME(6) DEFAULT NULL,
                        PRIMARY KEY (version),
                        UNIQUE KEY uk_schema_migration_filename (filename)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_0900_ai_ci
                    """
                )
            )
            manifest = validate_release_migration_manifest(
                sorted(MIGRATIONS_DIR.glob("*.sql"))
            )
            for migration in manifest:
                state = "APPLYING" if migration.version == 22 else "APPLIED"
                connection.execute(
                    text(
                        """
                        INSERT INTO t_schema_migrations (
                            version, filename, sha256, state,
                            baseline_bootstrap, started_at, applied_at
                        ) VALUES (
                            :version, :filename, :sha256, :state, 0,
                            UTC_TIMESTAMP(6),
                            CASE WHEN :state = 'APPLIED'
                                 THEN UTC_TIMESTAMP(6) ELSE NULL END
                        )
                        """
                    ),
                    {
                        "version": migration.version,
                        "filename": migration.path.name,
                        "sha256": migration.sha256,
                        "state": state,
                    },
                )
        with target.begin() as connection:
            connection.execute(text("DROP TABLE t_auth_audit_logs"))
        paths = sorted(MIGRATIONS_DIR.glob("*.sql"))
        inspection = inspect_applying_migration_022(target, paths)
        assert inspection["classification"] == "COMPATIBLE_PARTIAL"
        recovery = recover_applying_migration_022(
            target,
            paths,
            expected_state_digest=str(inspection["state_digest"]),
        )
        assert recovery["recovery_outcome"] == "APPLIED"
        with target.begin() as connection:
            assert classify_auth_schema(read_auth_schema(connection)) == (
                "COMPLETE"
            )
            connection.execute(
                text(
                    "ALTER TABLE t_auth_users "
                    "MODIFY username VARCHAR(31) CHARACTER SET ascii "
                    "COLLATE ascii_bin NOT NULL"
                )
            )
            assert classify_auth_schema(read_auth_schema(connection)) == (
                "UNSAFE"
            )
    finally:
        target.dispose()
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE `{schema_name}`"))
            connection.commit()
        admin.dispose()


def test_auth_service_sessions_throttle_and_admin_lifecycle_on_mysql8() -> None:
    assert MYSQL_URL is not None
    engine = _engine(MYSQL_URL)
    clock = [datetime(2026, 9, 2, 0, 0, 0)]
    service = AuthService(engine, now=lambda: clock[0])
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM t_auth_audit_logs"))
        connection.execute(text("DELETE FROM t_auth_sessions"))
        connection.execute(
            text(
                "UPDATE t_auth_users SET created_by = NULL, "
                "disabled_by = NULL"
            )
        )
        connection.execute(text("DELETE FROM t_auth_users"))
        repository.initialize_protected_admin(
            connection,
            password_hash=hash_password("Admin123"),
        )
    for _attempt in range(5):
        with pytest.raises(AuthError, match="invalid_credentials"):
            service.login("admin", "Wrong123")
    with engine.connect() as connection:
        failure_count, not_before = connection.execute(
            text(
                "SELECT failed_login_count, login_not_before "
                "FROM t_auth_users WHERE username='admin'"
            )
        ).one()
    assert int(failure_count) == 5
    assert not_before == clock[0] + timedelta(seconds=60)
    with pytest.raises(AuthError, match="invalid_credentials"):
        service.login("admin", "Admin123")
    clock[0] += timedelta(seconds=61)
    first = service.login("admin", "Admin123")
    second = service.login("admin", "Admin123")
    assert first.token != second.token
    service.change_password(
        first.token, "Admin123", "Changed456", "request-change"
    )
    for token in (first.token, second.token):
        with pytest.raises(AuthError, match="not_authenticated"):
            service.authenticate(token)
    admin = service.login("admin", "Changed456")
    created = service.create_user(
        admin.token,
        username="Second.Admin",
        initial_password="Second123",
        role="admin",
        request_id="request-create",
    )
    assert created.username == "second.admin"
    with pytest.raises(AuthError, match="protected_admin"):
        service.change_username(
            admin.token,
            user_id=admin.user.id,
            username="renamed-admin",
            request_id="request-protected",
        )
    before = service.login("second.admin", "Second123")
    unchanged = service.change_role(
        admin.token,
        user_id=created.id,
        role="admin",
        request_id="request-idempotent",
    )
    assert unchanged.role == "admin"
    assert service.authenticate(before.token).id == created.id
    with engine.connect() as connection:
        audit_payload = " ".join(
            str(row[0])
            for row in connection.execute(
                text("SELECT detail FROM t_auth_audit_logs")
            ).all()
        )
    assert "Changed456" not in audit_payload
    assert "Second123" not in audit_payload
    engine.dispose()
