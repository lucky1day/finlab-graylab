from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import httpx
import pytest
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from backend.auth import repository, routes
from backend.auth.repository import AuthUser
from backend.auth.service import AuthError, AuthService
from backend.main import app


@pytest.fixture(autouse=True)
def trusted_origin(monkeypatch):
    monkeypatch.setenv("BFL_DEPLOYMENT_TARGET", "mac3-production")
    monkeypatch.setenv("BFL_AUTH_TRUSTED_ORIGIN", "https://bond.finailab.cn")


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://testserver",
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def _user(*, user_id: int = 1, role: str = "user") -> AuthUser:
    return AuthUser(
        id=user_id,
        username=f"user{user_id}",
        password_hash="password-hash",
        role=role,
        status="active",
        is_protected_admin=False,
        must_change_password=False,
        failed_login_count=0,
        login_not_before=None,
        created_at=datetime(2026, 1, 1),
    )


def test_session_repository_exposes_locked_and_unlocked_reads():
    connection = MagicMock()
    connection.execute.return_value.mappings.return_value.one_or_none.return_value = (
        None
    )

    repository.get_session_user(connection, b"token", datetime(2026, 1, 1))
    ordinary_sql = str(connection.execute.call_args.args[0]).upper()
    repository.lock_session_user(connection, b"token", datetime(2026, 1, 1))
    locked_sql = str(connection.execute.call_args.args[0]).upper()

    assert "FOR UPDATE" not in ordinary_sql
    assert "S.REVOKED_AT IS NULL" in ordinary_sql
    assert "S.EXPIRES_AT >" in ordinary_sql
    assert "U.STATUS = 'ACTIVE'" in ordinary_sql
    assert "U.MUST_CHANGE_PASSWORD" in ordinary_sql
    assert "FOR UPDATE" in locked_sql


def test_current_session_uses_nonlocking_read(monkeypatch):
    user = _user()
    ordinary_read = Mock(return_value=(user, datetime(2099, 1, 1)))
    locked_read = Mock(side_effect=AssertionError("read path must not lock"))
    monkeypatch.setattr(repository, "get_session_user", ordinary_read)
    monkeypatch.setattr(repository, "lock_session_user", locked_read)

    result = AuthService(MagicMock()).current_session("session-token")

    assert result.user is user
    ordinary_read.assert_called_once()
    locked_read.assert_not_called()


def test_logout_revalidates_with_lock_in_revocation_transaction(monkeypatch):
    user = _user()
    locked_read = Mock(return_value=(user, datetime(2099, 1, 1)))
    ordinary_read = Mock(side_effect=AssertionError("write path must revalidate"))
    revoke = Mock()
    monkeypatch.setattr(repository, "lock_session_user", locked_read)
    monkeypatch.setattr(repository, "get_session_user", ordinary_read)
    monkeypatch.setattr(repository, "revoke_session", revoke)
    engine = MagicMock()

    AuthService(engine).logout("session-token")

    connection = engine.begin.return_value.__enter__.return_value
    locked_read.assert_called_once()
    assert locked_read.call_args.args[0] is connection
    ordinary_read.assert_not_called()
    revoke.assert_called_once()
    assert revoke.call_args.args[0] is connection


def test_new_request_rechecks_disabled_account(monkeypatch):
    state = {"active": True}
    user = _user()
    session_read = Mock(
        side_effect=lambda *_args: (
            (user, datetime(2099, 1, 1)) if state["active"] else None
        )
    )
    monkeypatch.setattr(repository, "get_session_user", session_read)
    monkeypatch.setattr(routes, "_service", lambda: AuthService(MagicMock()))

    first = _request(
        "GET",
        "/api/auth/me",
        headers={"Cookie": "__Host-bfl-session=session-token"},
    )
    state["active"] = False
    after_disable = _request(
        "GET",
        "/api/auth/me",
        headers={"Cookie": "__Host-bfl-session=session-token"},
    )

    assert first.status_code == 200
    assert after_disable.status_code == 401
    assert session_read.call_count == 2


def test_new_admin_request_rechecks_changed_role(monkeypatch):
    state = {"role": "admin"}
    session_read = Mock(
        side_effect=lambda *_args: (
            _user(role=state["role"]),
            datetime(2099, 1, 1),
        )
    )
    monkeypatch.setattr(repository, "get_session_user", session_read)
    monkeypatch.setattr(repository, "list_users", lambda _connection: [])
    monkeypatch.setattr(routes, "_service", lambda: AuthService(MagicMock()))

    first = _request(
        "GET",
        "/api/admin/users",
        headers={"Cookie": "__Host-bfl-session=session-token"},
    )
    state["role"] = "user"
    after_role_change = _request(
        "GET",
        "/api/admin/users",
        headers={"Cookie": "__Host-bfl-session=session-token"},
    )

    assert first.status_code == 200
    assert after_role_change.status_code == 403
    assert session_read.call_count == 2


def test_role_mutation_revalidates_locked_actor_and_protected_target(monkeypatch):
    actor = _user(user_id=1, role="admin")
    target = replace(
        _user(user_id=2, role="admin"),
        is_protected_admin=True,
    )
    locked_session = Mock(return_value=(actor, datetime(2099, 1, 1)))
    update_role = Mock(side_effect=AssertionError("protected user must not change"))
    monkeypatch.setattr(repository, "lock_session_user", locked_session)
    monkeypatch.setattr(repository, "lock_user_by_id", lambda *_args: target)
    monkeypatch.setattr(repository, "update_role", update_role)

    with pytest.raises(AuthError, match="protected_admin"):
        AuthService(MagicMock()).change_role(
            "session-token",
            user_id=target.id,
            role="user",
            request_id="role-change",
        )

    locked_session.assert_called_once()
    update_role.assert_not_called()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("cookie", ["", "__Host-bfl-session=invalid-session"])
def test_dashboard_rejects_absent_or_invalid_session(monkeypatch, method, cookie):
    service = AuthService(MagicMock())
    monkeypatch.setattr(routes, "_service", lambda: service)
    monkeypatch.setattr(repository, "get_session_user", lambda *_args: None)

    response = _request(method, "/api/factor-lab/dashboard", headers={"Cookie": cookie})
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


def test_dashboard_auth_pool_failure_returns_stable_503(monkeypatch, caplog):
    engine = MagicMock()
    engine.connect.side_effect = SQLAlchemyTimeoutError("private pool detail")
    monkeypatch.setattr(routes, "_service", lambda: AuthService(engine))

    with caplog.at_level("ERROR", logger="backend.auth.routes"):
        response = _request(
            "GET",
            "/api/factor-lab/dashboard",
            headers={
                "Cookie": "__Host-bfl-session=test-session",
                "X-Request-ID": "auth-pool-timeout",
            },
        )

    assert response.status_code == 503
    assert response.json() == {"error_code": "auth_unavailable"}
    assert "private pool detail" not in response.text
    assert '"failure_stage":"auth"' in caplog.text
    assert '"request_id":"auth-pool-timeout"' in caplog.text
    assert "private pool detail" not in caplog.text


def test_ordinary_user_cannot_read_admin_data(monkeypatch):
    service = AuthService(MagicMock())
    monkeypatch.setattr(routes, "_service", lambda: service)
    monkeypatch.setattr(
        repository, "get_session_user",
        lambda *_args: (SimpleNamespace(role="user"), datetime(2099, 1, 1)),
    )
    read_users = Mock(side_effect=AssertionError("admin data must not be read"))
    monkeypatch.setattr(repository, "list_users", read_users)

    response = _request(
        "GET", "/api/admin/users", headers={"Cookie": "__Host-bfl-session=user-session"},
    )
    assert response.status_code == 403
    assert response.json() == {"error_code": "forbidden"}
    read_users.assert_not_called()


@pytest.mark.parametrize("origin,site", [
    ("", "same-origin"),
    ("https://untrusted.example", "same-origin"),
    ("https://bond.finailab.cn", "cross-site"),
])
def test_cross_site_write_is_rejected_before_service(monkeypatch, origin, site):
    service = Mock(side_effect=AssertionError("write must be rejected before service"))
    monkeypatch.setattr(routes, "_service", service)
    response = _request(
        "POST", "/api/auth/login",
        headers={"Origin": origin, "Sec-Fetch-Site": site},
        json={"username": "admin", "password": "Secret123"},
    )
    assert response.status_code == 403
    service.assert_not_called()


def test_validation_failure_does_not_echo_password():
    secret = "NeverEchoThis123"
    response = _request(
        "POST", "/api/auth/login",
        headers={"Origin": "https://bond.finailab.cn", "Sec-Fetch-Site": "same-origin"},
        json={"username": "admin", "password": secret, "unexpected": secret},
    )
    assert response.status_code == 422
    assert response.json() == {"error_code": "invalid_request"}
    assert secret not in response.text
