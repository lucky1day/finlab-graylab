from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from backend.auth import repository
from backend.auth.repository import AuthUser
from backend.auth.service import AuthError, AuthService


NOW = datetime(2026, 9, 3, 1, 2, 3, 456789)


def _user(
    user_id: int,
    username: str,
    *,
    role: str,
    status: str = "active",
    protected: bool = False,
    full_name: str | None = None,
    organization_name: str | None = None,
) -> AuthUser:
    return AuthUser(
        id=user_id,
        username=username,
        full_name=full_name,
        organization_name=organization_name,
        password_hash="encoded-password",
        role=role,
        status=status,
        is_protected_admin=protected,
        must_change_password=False,
        failed_login_count=0,
        login_not_before=None,
        created_at=NOW,
    )


def test_edit_user_saves_all_account_fields_in_one_transaction() -> None:
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    actor = _user(1, "admin.one", role="admin")
    target = _user(2, "member.old", role="user")
    updated = _user(
        2,
        "member.new",
        role="admin",
        status="disabled",
        full_name="张三",
        organization_name="示例机构",
    )
    service = AuthService(engine, now=lambda: NOW)

    with (
        patch.object(service, "_require_admin", return_value=actor),
        patch.object(service, "_lock_admin", return_value=actor),
        patch.object(
            repository,
            "lock_user_by_id",
            side_effect=(target, updated),
        ),
        patch.object(repository, "update_username") as update_username,
        patch.object(repository, "update_profile") as update_profile,
        patch.object(repository, "update_role") as update_role,
        patch.object(repository, "update_status") as update_status,
        patch.object(repository, "revoke_all_sessions") as revoke_sessions,
        patch.object(repository, "insert_audit") as insert_audit,
    ):
        result = service.edit_user(
            "session-token",
            user_id=2,
            username="Member.New",
            full_name=" 张三 ",
            organization_name=" 示例机构 ",
            role="admin",
            status="disabled",
            request_id="request-edit",
        )

    assert result is updated
    update_username.assert_called_once_with(connection, 2, "member.new")
    update_profile.assert_called_once_with(
        connection,
        user_id=2,
        full_name="张三",
        organization_name="示例机构",
    )
    update_role.assert_called_once_with(connection, 2, "admin")
    update_status.assert_called_once_with(
        connection,
        user_id=2,
        status="disabled",
        actor_user_id=1,
        now=NOW,
    )
    revoke_sessions.assert_called_once_with(connection, 2, NOW)
    assert insert_audit.call_count == 4
    assert [item.kwargs["event_type"] for item in insert_audit.call_args_list] == [
        "user_username_changed",
        "user_profile_changed",
        "user_role_changed",
        "user_status_changed",
    ]


def test_edit_user_allows_protected_admin_profile_only_change() -> None:
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    target = _user(1, "admin", role="admin", protected=True)
    updated = _user(
        1,
        "admin",
        role="admin",
        protected=True,
        full_name="管理员",
    )
    service = AuthService(engine, now=lambda: NOW)

    with (
        patch.object(service, "_require_admin", return_value=target),
        patch.object(service, "_lock_admin", return_value=target),
        patch.object(
            repository,
            "lock_user_by_id",
            side_effect=(target, updated),
        ),
        patch.object(repository, "update_profile") as update_profile,
        patch.object(repository, "revoke_all_sessions") as revoke_sessions,
        patch.object(repository, "insert_audit") as insert_audit,
    ):
        result = service.edit_user(
            "session-token",
            user_id=1,
            username="admin",
            full_name="管理员",
            organization_name=None,
            role="admin",
            status="active",
            request_id="request-profile",
        )

    assert result is updated
    update_profile.assert_called_once()
    revoke_sessions.assert_not_called()
    insert_audit.assert_called_once()


def test_edit_user_rejects_restricted_protected_admin_change() -> None:
    engine = MagicMock()
    target = _user(1, "admin", role="admin", protected=True)
    service = AuthService(engine, now=lambda: NOW)

    with (
        patch.object(service, "_require_admin", return_value=target),
        patch.object(service, "_lock_admin", return_value=target),
        patch.object(repository, "lock_user_by_id", return_value=target),
        patch.object(repository, "update_username") as update_username,
    ):
        with pytest.raises(AuthError, match="protected_admin"):
            service.edit_user(
                "session-token",
                user_id=1,
                username="admin.renamed",
                full_name=None,
                organization_name=None,
                role="admin",
                status="active",
                request_id="request-protected",
            )

    update_username.assert_not_called()
