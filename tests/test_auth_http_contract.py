from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import httpx
import pytest
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from backend.auth import repository, routes
from backend.auth.repository import AuthUser
from backend.auth.service import AuthService, LoginResult
from backend.main import app
from backend.request_limits import (
    AUTH_JSON_WRITE_PATHS,
    AuthJSONBodyLimitMiddleware,
)


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


def _chunked_asgi_request(
    path: str,
    *,
    headers: list[tuple[bytes, bytes]],
    messages: list[dict],
) -> tuple[int, dict[str, str], bytes]:
    """直接驱动 ASGI receive，覆盖 HTTP 客户端真实分块语义。"""
    sent = _drive_chunked_asgi(path, headers=headers, messages=messages)
    start = next(item for item in sent if item["type"] == "http.response.start")
    response_headers = {
        key.decode("latin-1"): value.decode("latin-1")
        for key, value in start.get("headers", [])
    }
    body = b"".join(
        item.get("body", b"")
        for item in sent
        if item["type"] == "http.response.body"
    )
    return start["status"], response_headers, body


def _drive_chunked_asgi(
    path: str,
    *,
    headers: list[tuple[bytes, bytes]],
    messages: list[dict],
) -> list[dict]:
    """返回直接驱动 ASGI 后发送的原始消息。"""
    sent: list[dict] = []
    pending = list(messages)

    async def receive() -> dict:
        if pending:
            return pending.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    async def invoke() -> None:
        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": b"",
                "headers": headers,
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 443),
                "root_path": "",
            },
            receive,
            send,
        )

    asyncio.run(invoke())
    return sent


def _safe_json_headers(
    *extra: tuple[bytes, bytes],
) -> list[tuple[bytes, bytes]]:
    return [
        (b"host", b"testserver"),
        (b"content-type", b"application/json"),
        (b"origin", b"https://bond.finailab.cn"),
        (b"sec-fetch-site", b"same-origin"),
        *extra,
    ]


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


def test_auth_body_limit_covers_every_declared_auth_write_route():
    declared_write_paths = {
        route.path
        for route in app.routes
        if "POST" in (getattr(route, "methods", None) or set())
        and route.path.startswith(("/api/auth/", "/api/admin/"))
    }

    assert declared_write_paths == AUTH_JSON_WRITE_PATHS


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


def test_non_json_write_remains_rejected_before_service(monkeypatch):
    service = Mock(side_effect=AssertionError("non-JSON body reached service"))
    monkeypatch.setattr(routes, "_service", service)

    response = _request(
        "POST",
        "/api/auth/login",
        headers={
            "Content-Type": "text/plain",
            "Origin": "https://bond.finailab.cn",
            "Sec-Fetch-Site": "same-origin",
        },
        content=b"{}",
    )

    assert response.status_code == 415
    assert response.json() == {"error_code": "invalid_content_type"}
    service.assert_not_called()


@pytest.mark.parametrize(
    "content_length_headers",
    [[], [(b"content-length", b"8192")]],
)
def test_auth_body_limit_allows_exactly_8192_bytes_with_or_without_length(
    monkeypatch,
    content_length_headers,
):
    service = Mock(side_effect=AssertionError("invalid payload must not call service"))
    monkeypatch.setattr(routes, "_service", service)
    body = b"{}" + (b" " * 8190)

    status, _headers, response_body = _chunked_asgi_request(
        "/api/auth/login",
        headers=_safe_json_headers(*content_length_headers),
        messages=[
            {"type": "http.request", "body": body[:4096], "more_body": True},
            {"type": "http.request", "body": body[4096:], "more_body": False},
        ],
    )

    assert status == 422
    assert json.loads(response_body) == {"error_code": "invalid_request"}
    service.assert_not_called()


def test_auth_body_limit_rejects_8193_chunked_bytes_before_service(monkeypatch):
    service = Mock(side_effect=AssertionError("oversize body reached service"))
    monkeypatch.setattr(routes, "_service", service)

    status, headers, response_body = _chunked_asgi_request(
        "/api/admin/users",
        headers=_safe_json_headers(),
        messages=[
            {"type": "http.request", "body": b"x" * 4096, "more_body": True},
            {"type": "http.request", "body": b"x" * 4097, "more_body": True},
            {"type": "http.disconnect"},
        ],
    )

    assert status == 413
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-request-id"]
    assert json.loads(response_body) == {"error_code": "request_body_too_large"}
    service.assert_not_called()


@pytest.mark.parametrize(
    "declared_length, expected_status, expected_code",
    [
        (b"1", 400, "invalid_content_length"),
        (b"3", 408, "request_body_incomplete"),
    ],
)
def test_auth_body_limit_rejects_content_length_mismatch(
    monkeypatch,
    declared_length,
    expected_status,
    expected_code,
):
    service = Mock(side_effect=AssertionError("length mismatch reached service"))
    monkeypatch.setattr(routes, "_service", service)

    status, _headers, response_body = _chunked_asgi_request(
        "/api/auth/login",
        headers=_safe_json_headers((b"content-length", declared_length)),
        messages=[
            {"type": "http.request", "body": b"{}", "more_body": False},
        ],
    )

    assert status == expected_status
    assert json.loads(response_body) == {"error_code": expected_code}
    service.assert_not_called()


def test_auth_body_limit_stops_on_client_disconnect_without_service():
    downstream_called = False
    sent: list[dict] = []
    pending = [
        {"type": "http.request", "body": b'{"username":', "more_body": True},
        {"type": "http.disconnect"},
    ]

    async def downstream(_scope, _receive, _send):
        nonlocal downstream_called
        downstream_called = True

    async def receive():
        return pending.pop(0)

    async def send(message):
        sent.append(message)

    async def invoke():
        await AuthJSONBodyLimitMiddleware(downstream)(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/auth/login",
                "headers": [],
            },
            receive,
            send,
        )

    asyncio.run(invoke())
    assert sent == []
    assert downstream_called is False


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


def test_auth_http_dates_are_explicit_utc(monkeypatch):
    user = replace(_user(), created_at=datetime(2026, 12, 31, 20, 30))
    expires_at = datetime(2027, 1, 1, 8, 30)
    service = Mock()
    service.current_session.return_value = SimpleNamespace(user=user, expires_at=expires_at)
    service.login.return_value = LoginResult(user=user, token="opaque", expires_at=expires_at)
    monkeypatch.setattr(routes, "_service", lambda: service)

    me = _request("GET", "/api/auth/me")
    login = _request("POST", "/api/auth/login", headers={
        "Origin": "https://bond.finailab.cn", "Sec-Fetch-Site": "same-origin",
    }, json={"username": "owner", "password": "Example123"})

    for response in (me, login):
        assert response.status_code == 200
        assert response.json()["user"]["created_at"] == "2026-12-31T20:30:00.000000+00:00"
        assert response.json()["expires_at"] == "2027-01-01T08:30:00.000000+00:00"


@pytest.mark.parametrize("candidate", [None, "", "edge-request-1", "x" * 128, "x" * 129, "space id", "\xff"])
def test_auth_audit_reuses_the_response_request_id(monkeypatch, candidate):
    service = Mock()
    service.update_own_profile.return_value = _user()
    monkeypatch.setattr(routes, "_service", lambda: service)
    headers = _safe_json_headers()
    if candidate is not None:
        headers.append((b"x-request-id", candidate.encode("latin-1")))
    response = _request("POST", "/api/auth/update-profile", headers=headers,
                        json={"full_name": "Name", "organization_name": None})
    assert response.status_code == 200
    request_id = response.headers["x-request-id"]
    assert service.update_own_profile.call_args.kwargs["request_id"] == request_id
    if candidate and re.fullmatch(r"[!-~]{1,128}", candidate, re.ASCII):
        assert request_id == candidate
    else:
        assert re.fullmatch(r"[0-9a-f]{32}", request_id)


def test_auth_body_limit_rejects_declared_8193_bytes_before_receive_or_service(
    monkeypatch,
):
    service = Mock(side_effect=AssertionError("oversize body reached service"))
    monkeypatch.setattr(routes, "_service", service)

    status, _headers, response_body = _chunked_asgi_request(
        "/api/auth/login",
        headers=_safe_json_headers((b"content-length", b"8193")),
        messages=[{"type": "http.disconnect"}],
    )

    assert status == 413
    assert json.loads(response_body) == {"error_code": "request_body_too_large"}
    service.assert_not_called()


def test_auth_body_limit_times_out_slow_body_without_service():
    downstream_called = False
    sent: list[dict] = []

    async def downstream(_scope, _receive, _send):
        nonlocal downstream_called
        downstream_called = True

    async def receive():
        await asyncio.sleep(1)
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    async def invoke():
        middleware = AuthJSONBodyLimitMiddleware(
            downstream,
            body_read_timeout_seconds=0.01,
        )
        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/auth/login",
                "headers": [],
            },
            receive,
            send,
        )

    asyncio.run(invoke())
    start = next(item for item in sent if item["type"] == "http.response.start")
    body = b"".join(
        item.get("body", b"")
        for item in sent
        if item["type"] == "http.response.body"
    )
    assert start["status"] == 408
    assert json.loads(body) == {"error_code": "request_body_timeout"}
    assert downstream_called is False
