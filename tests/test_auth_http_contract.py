from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import httpx
import pytest

from backend.auth import repository, routes
from backend.auth.service import AuthService
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


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("cookie", ["", "__Host-bfl-session=invalid-session"])
def test_dashboard_rejects_absent_or_invalid_session(monkeypatch, method, cookie):
    service = AuthService(MagicMock())
    monkeypatch.setattr(routes, "_service", lambda: service)
    monkeypatch.setattr(repository, "lock_session_user", lambda *_args: None)

    response = _request(method, "/api/factor-lab/dashboard", headers={"Cookie": cookie})
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


def test_ordinary_user_cannot_read_admin_data(monkeypatch):
    service = AuthService(MagicMock())
    monkeypatch.setattr(routes, "_service", lambda: service)
    monkeypatch.setattr(
        repository, "lock_session_user",
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
