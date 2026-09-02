from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import text


@dataclass(frozen=True, slots=True)
class AuthUser:
    """认证域内不可变用户快照。"""

    id: int
    username: str
    password_hash: str
    role: str
    status: str
    is_protected_admin: bool
    must_change_password: bool
    failed_login_count: int
    login_not_before: datetime | None
    created_at: datetime
    full_name: str | None = None
    organization_name: str | None = None

    def public_dict(self) -> dict[str, Any]:
        """返回不含密码与限速内部状态的固定公开对象。"""
        return {
            "id": self.id,
            "username": self.username,
            "full_name": self.full_name,
            "organization_name": self.organization_name,
            "role": self.role,
            "status": self.status,
            "is_protected_admin": self.is_protected_admin,
            "must_change_password": self.must_change_password,
            "created_at": self.created_at.isoformat(timespec="microseconds"),
        }


def _user(row: Mapping[str, Any]) -> AuthUser:
    return AuthUser(
        id=int(row["id"]),
        username=str(row["username"]),
        full_name=(
            None if row["full_name"] is None else str(row["full_name"])
        ),
        organization_name=(
            None
            if row["organization_name"] is None
            else str(row["organization_name"])
        ),
        password_hash=str(row["password_hash"]),
        role=str(row["role"]),
        status=str(row["status"]),
        is_protected_admin=bool(row["is_protected_admin"]),
        must_change_password=bool(row["must_change_password"]),
        failed_login_count=int(row["failed_login_count"]),
        login_not_before=row["login_not_before"],
        created_at=row["created_at"],
    )


_USER_COLUMNS = """
    id, username, full_name, organization_name, password_hash,
    role, status, is_protected_admin,
    must_change_password, failed_login_count, login_not_before, created_at
"""


def lock_user_by_username(connection: Any, username: str) -> AuthUser | None:
    row = connection.execute(
        text(
            f"SELECT {_USER_COLUMNS} FROM t_auth_users "
            "WHERE BINARY username = BINARY :username FOR UPDATE"
        ),
        {"username": username},
    ).mappings().one_or_none()
    return None if row is None else _user(row)


def lock_user_by_id(connection: Any, user_id: int) -> AuthUser | None:
    row = connection.execute(
        text(
            f"SELECT {_USER_COLUMNS} FROM t_auth_users "
            "WHERE id = :user_id FOR UPDATE"
        ),
        {"user_id": user_id},
    ).mappings().one_or_none()
    return None if row is None else _user(row)


def lock_session_user(
    connection: Any,
    token_hash: bytes,
    now: datetime,
) -> tuple[AuthUser, datetime] | None:
    row = connection.execute(
        text(
            f"""
            SELECT u.id, u.username, u.full_name, u.organization_name,
                   u.password_hash, u.role, u.status,
                   u.is_protected_admin, u.must_change_password,
                   u.failed_login_count, u.login_not_before, u.created_at,
                   s.expires_at AS session_expires_at
            FROM t_auth_sessions AS s
            INNER JOIN t_auth_users AS u ON u.id = s.user_id
            WHERE s.token_hash = :token_hash
              AND s.revoked_at IS NULL
              AND s.expires_at > :now
              AND u.status = 'active'
            FOR UPDATE
            """
        ),
        {"token_hash": token_hash, "now": now},
    ).mappings().one_or_none()
    return (
        None
        if row is None
        else (_user(row), row["session_expires_at"])
    )


def list_users(connection: Any) -> list[AuthUser]:
    rows = connection.execute(
        text(f"SELECT {_USER_COLUMNS} FROM t_auth_users ORDER BY BINARY username")
    ).mappings().all()
    return [_user(row) for row in rows]


def create_session(
    connection: Any,
    *,
    user_id: int,
    token_hash: bytes,
    expires_at: datetime,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO t_auth_sessions (user_id, token_hash, expires_at)
            VALUES (:user_id, :token_hash, :expires_at)
            """
        ),
        {
            "user_id": user_id,
            "token_hash": token_hash,
            "expires_at": expires_at,
        },
    )


def record_login_failure(
    connection: Any,
    *,
    user_id: int,
    failed_login_count: int,
    login_not_before: datetime | None,
) -> None:
    connection.execute(
        text(
            """
            UPDATE t_auth_users
            SET failed_login_count = :failed_login_count,
                login_not_before = :login_not_before
            WHERE id = :user_id
            """
        ),
        {
            "user_id": user_id,
            "failed_login_count": failed_login_count,
            "login_not_before": login_not_before,
        },
    )


def clear_login_failures(connection: Any, user_id: int) -> None:
    connection.execute(
        text(
            """
            UPDATE t_auth_users
            SET failed_login_count = 0, login_not_before = NULL
            WHERE id = :user_id
            """
        ),
        {"user_id": user_id},
    )


def revoke_session(connection: Any, token_hash: bytes, now: datetime) -> None:
    connection.execute(
        text(
            """
            UPDATE t_auth_sessions SET revoked_at = :now
            WHERE token_hash = :token_hash AND revoked_at IS NULL
            """
        ),
        {"token_hash": token_hash, "now": now},
    )


def revoke_all_sessions(connection: Any, user_id: int, now: datetime) -> None:
    connection.execute(
        text(
            """
            UPDATE t_auth_sessions SET revoked_at = :now
            WHERE user_id = :user_id AND revoked_at IS NULL
            """
        ),
        {"user_id": user_id, "now": now},
    )


def insert_audit(
    connection: Any,
    *,
    event_type: str,
    actor_user_id: int | None,
    target_user_id: int | None,
    request_id: str,
    detail: Mapping[str, Any],
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO t_auth_audit_logs (
                event_type, actor_user_id, target_user_id, request_id, detail
            ) VALUES (
                :event_type, :actor_user_id, :target_user_id,
                :request_id, CAST(:detail AS JSON)
            )
            """
        ),
        {
            "event_type": event_type,
            "actor_user_id": actor_user_id,
            "target_user_id": target_user_id,
            "request_id": request_id,
            "detail": json.dumps(
                dict(detail), ensure_ascii=True, separators=(",", ":"), sort_keys=True
            ),
        },
    )


def lock_active_admin_ids(connection: Any) -> list[int]:
    rows = connection.execute(
        text(
            """
            SELECT id FROM t_auth_users
            WHERE role = 'admin' AND status = 'active'
            ORDER BY id FOR UPDATE
            """
        )
    ).all()
    return [int(row[0]) for row in rows]


def create_user(
    connection: Any,
    *,
    username: str,
    password_hash: str,
    role: str,
    full_name: str | None,
    organization_name: str | None,
    actor_user_id: int,
    now: datetime,
) -> int:
    result = connection.execute(
        text(
            """
            INSERT INTO t_auth_users (
                username, full_name, organization_name,
                password_hash, role, status,
                is_protected_admin, must_change_password,
                password_changed_at, created_by
            ) VALUES (
                :username, :full_name, :organization_name,
                :password_hash, :role, 'active',
                0, 0, :now, :actor_user_id
            )
            """
        ),
        {
            "username": username,
            "password_hash": password_hash,
            "role": role,
            "full_name": full_name,
            "organization_name": organization_name,
            "now": now,
            "actor_user_id": actor_user_id,
        },
    )
    return int(result.lastrowid)


def update_password(
    connection: Any,
    *,
    user_id: int,
    password_hash: str,
    must_change_password: bool,
    now: datetime,
) -> None:
    connection.execute(
        text(
            """
            UPDATE t_auth_users
            SET password_hash = :password_hash,
                must_change_password = :must_change_password,
                password_changed_at = :now,
                failed_login_count = 0,
                login_not_before = NULL
            WHERE id = :user_id
            """
        ),
        {
            "password_hash": password_hash,
            "must_change_password": int(must_change_password),
            "now": now,
            "user_id": user_id,
        },
    )


def update_username(connection: Any, user_id: int, username: str) -> None:
    connection.execute(
        text("UPDATE t_auth_users SET username = :username WHERE id = :user_id"),
        {"username": username, "user_id": user_id},
    )


def update_profile(
    connection: Any,
    *,
    user_id: int,
    full_name: str | None,
    organization_name: str | None,
) -> None:
    connection.execute(
        text(
            """
            UPDATE t_auth_users
            SET full_name = :full_name,
                organization_name = :organization_name
            WHERE id = :user_id
            """
        ),
        {
            "user_id": user_id,
            "full_name": full_name,
            "organization_name": organization_name,
        },
    )


def update_role(connection: Any, user_id: int, role: str) -> None:
    connection.execute(
        text("UPDATE t_auth_users SET role = :role WHERE id = :user_id"),
        {"role": role, "user_id": user_id},
    )


def update_status(
    connection: Any,
    *,
    user_id: int,
    status: str,
    actor_user_id: int,
    now: datetime,
) -> None:
    connection.execute(
        text(
            """
            UPDATE t_auth_users
            SET status = :status,
                disabled_by = CASE
                    WHEN :status = 'disabled' THEN :actor_user_id ELSE NULL
                END,
                disabled_at = CASE
                    WHEN :status = 'disabled' THEN :now ELSE NULL
                END
            WHERE id = :user_id
            """
        ),
        {
            "status": status,
            "actor_user_id": actor_user_id,
            "now": now,
            "user_id": user_id,
        },
    )


def initialize_protected_admin(
    connection: Any,
    *,
    password_hash: str,
) -> int:
    result = connection.execute(
        text(
            """
            INSERT INTO t_auth_users (
                username, password_hash, role, status,
                is_protected_admin, must_change_password,
                password_changed_at, created_by
            ) VALUES (
                'admin', :password_hash, 'admin', 'active',
                1, 0, UTC_TIMESTAMP(6), NULL
            )
            """
        ),
        {"password_hash": password_hash},
    )
    return int(result.lastrowid)


def reset_protected_admin_password(
    connection: Any,
    *,
    user_id: int,
    password_hash: str,
) -> None:
    connection.execute(
        text(
            """
            UPDATE t_auth_users
            SET password_hash = :password_hash,
                must_change_password = 0,
                password_changed_at = UTC_TIMESTAMP(6),
                failed_login_count = 0,
                login_not_before = NULL
            WHERE id = :user_id
              AND role = 'admin'
              AND status = 'active'
              AND is_protected_admin = 1
            """
        ),
        {"password_hash": password_hash, "user_id": user_id},
    )


def revoke_all_admin_sessions(connection: Any) -> None:
    connection.execute(
        text(
            """
            UPDATE t_auth_sessions AS sessions
            INNER JOIN t_auth_users AS users ON users.id = sessions.user_id
            SET sessions.revoked_at = UTC_TIMESTAMP(6)
            WHERE users.role = 'admin' AND sessions.revoked_at IS NULL
            """
        )
    )
