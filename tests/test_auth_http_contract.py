from __future__ import annotations

from datetime import datetime, timedelta
import asyncio
from unittest.mock import Mock, patch

import httpx
import pytest

from backend.auth.repository import AuthUser
from backend.auth.service import AuthError, LoginResult, SessionResult
from backend.main import app


NOW = datetime(2026, 9, 2, 1, 2, 3, 456789)


@pytest.fixture(autouse=True)
def _trusted_origin(monkeypatch):
    monkeypatch.setenv("BFL_DEPLOYMENT_TARGET", "mac3-production")
    monkeypatch.setenv(
        "BFL_AUTH_TRUSTED_ORIGIN", "https://bond.finailab.cn"
    )


def _user(
    *,
    role: str = "admin",
    must_change_password: bool = False,
) -> AuthUser:
    return AuthUser(
        id=1,
        username="admin",
        password_hash="not-public",
        role=role,
        status="active",
        is_protected_admin=True,
        must_change_password=must_change_password,
        failed_login_count=0,
        login_not_before=None,
        created_at=NOW,
    )


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Origin": "https://bond.finailab.cn",
        "Sec-Fetch-Site": "same-origin",
    }


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def test_login_cookie_and_public_shape_are_exact(monkeypatch) -> None:
    service = Mock()
    service.login.return_value = LoginResult(
        user=_user(), token="opaque-token", expires_at=NOW + timedelta(hours=12)
    )
    with patch("backend.auth.routes._service", return_value=service):
        response = _request(
            "POST",
            "/api/auth/login",
            headers=_headers(),
            json={"username": "admin", "password": "Secret123"},
        )
    assert response.status_code == 200
    assert response.json() == {
        "user": {
            "id": 1,
            "username": "admin",
            "role": "admin",
            "status": "active",
            "is_protected_admin": True,
            "must_change_password": False,
            "created_at": "2026-09-02T01:02:03.456789",
        },
        "expires_at": "2026-09-02T13:02:03.456789",
    }
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-bfl-session=opaque-token;")
    for attribute in (
        "HttpOnly",
        "Max-Age=43200",
        "Path=/",
        "SameSite=strict",
        "Secure",
    ):
        assert attribute in cookie
    assert response.headers["cache-control"] == "no-store"


def test_state_change_request_guards_fail_closed(monkeypatch) -> None:
    no_origin = _request(
            "POST",
            "/api/auth/login",
            headers={
                "Content-Type": "application/json",
                "Sec-Fetch-Site": "same-origin",
            },
            json={"username": "admin", "password": "Secret123"},
        )
    wrong_site = _request(
            "POST",
            "/api/auth/login",
            headers={**_headers(), "Sec-Fetch-Site": "cross-site"},
            json={"username": "admin", "password": "Secret123"},
        )
    wrong_type = _request(
            "POST",
            "/api/auth/login",
            headers={
                "Origin": "https://testserver",
                "Sec-Fetch-Site": "same-origin",
            },
            content="username=admin",
        )
    assert no_origin.json() == {"error_code": "invalid_origin"}
    assert wrong_site.json() == {"error_code": "invalid_request_site"}
    assert wrong_type.json() == {"error_code": "invalid_content_type"}


def test_validation_failure_does_not_echo_password(monkeypatch) -> None:
    secret = "NeverEchoThis123"
    response = _request(
            "POST",
            "/api/auth/login",
            headers=_headers(),
            json={
                "username": "admin",
                "password": secret,
                "unexpected": secret,
            },
        )
    assert response.status_code == 422
    assert response.json() == {"error_code": "invalid_request"}
    assert secret not in response.text


def test_dashboard_get_and_head_require_session(monkeypatch) -> None:
    service = Mock()
    service.authenticate.side_effect = AuthError("not_authenticated", 401)
    with patch("backend.auth.routes._service", return_value=service):
        get_response = _request("GET", "/api/factor-lab/dashboard")
        head_response = _request("HEAD", "/api/factor-lab/dashboard")
    assert get_response.status_code == 401
    assert get_response.json() == {"error_code": "not_authenticated"}
    assert head_response.status_code == 401


def test_must_change_password_cannot_read_dashboard(monkeypatch) -> None:
    service = Mock()
    service.authenticate.return_value = _user(must_change_password=True)
    with patch("backend.auth.routes._service", return_value=service):
        response = _request(
            "GET",
            "/api/factor-lab/dashboard",
            headers={"Cookie": "__Host-bfl-session=opaque-token"},
        )
    assert response.status_code == 403
    assert response.json() == {"error_code": "password_change_required"}


def test_me_and_admin_forbidden_contract(monkeypatch) -> None:
    service = Mock()
    service.current_session.return_value = SessionResult(
        user=_user(role="user"), expires_at=NOW + timedelta(hours=12)
    )
    service.list_users.side_effect = AuthError("forbidden", 403)
    with patch("backend.auth.routes._service", return_value=service):
        me = _request(
            "GET",
            "/api/auth/me",
            headers={"Cookie": "__Host-bfl-session=opaque-token"},
        )
        users = _request(
            "GET",
            "/api/admin/users",
            headers={"Cookie": "__Host-bfl-session=opaque-token"},
        )
    assert me.status_code == 200
    assert me.json()["user"]["role"] == "user"
    assert users.status_code == 403
    assert users.json() == {"error_code": "forbidden"}


def test_security_headers_apply_to_html_and_api() -> None:
    response = _request("GET", "/")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
