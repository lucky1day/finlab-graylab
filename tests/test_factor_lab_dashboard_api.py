from __future__ import annotations

import asyncio
import base64
import gzip
import json
import random
import re
from typing import Any

import pytest


DASHBOARD_PATH = "/api/factor-lab/dashboard"


@pytest.fixture(autouse=True)
def _authenticated_dashboard_request():
    """既有表示层测试绕过新增鉴权，专注验证 Dashboard 合同。"""
    from backend import main

    main.app.dependency_overrides[main.require_dashboard_user] = lambda: object()
    try:
        yield
    finally:
        main.app.dependency_overrides.pop(main.require_dashboard_user, None)


def _payload(snapshot_id: str) -> dict[str, Any]:
    return {
        "schema_version": "factor-lab-dashboard-v6",
        "representation": "summary",
        "snapshot_id": snapshot_id,
        "generated_at": "2026-08-07T16:41:00+08:00",
        "display_until": "2026-08-07",
        "live_target_start_date": "2026-06-01",
        "monthly_row_fields": [],
        "target_labels": {},
        "schemes": [],
    }


async def _asgi_request(
    app,
    *,
    method: str = "GET",
    query_string: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, dict[str, str], bytes, list[dict[str, Any]]]:
    messages: list[dict[str, Any]] = []
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": DASHBOARD_PATH,
            "raw_path": DASHBOARD_PATH.encode("ascii"),
            "query_string": query_string,
            "root_path": "",
            "headers": headers or [],
            "client": ("127.0.0.1", 43123),
            "server": ("127.0.0.1", 18101),
        },
        receive,
        send,
    )
    start = next(item for item in messages if item["type"] == "http.response.start")
    response_headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in start["headers"]
    }
    body = b"".join(
        item.get("body", b"")
        for item in messages
        if item["type"] == "http.response.body"
    )
    return start["status"], response_headers, body, messages


def _request(app, **kwargs):
    return asyncio.run(_asgi_request(app, **kwargs))


def test_dashboard_get_and_head_build_independently_without_cache_headers(
    monkeypatch,
) -> None:
    from backend import main

    engine = object()
    build_calls: list[object] = []

    def build(received_engine: object) -> dict[str, Any]:
        build_calls.append(received_engine)
        return _payload(f"fresh-{len(build_calls)}")

    monkeypatch.setattr(main, "get_dashboard_engine", lambda: engine)
    monkeypatch.setattr(main, "build_factor_lab_dashboard", build)
    request_headers = [
        (b"accept-encoding", b"identity"),
        (b"x-request-id", b"direct-request-123"),
    ]

    get_status, get_headers, get_body, _ = _request(
        main.app,
        headers=request_headers,
    )
    head_status, head_headers, head_body, _ = _request(
        main.app,
        method="HEAD",
        headers=request_headers,
    )

    assert get_status == head_status == 200
    assert json.loads(get_body)["snapshot_id"] == "fresh-1"
    assert head_body == b""
    assert build_calls == [engine, engine]
    assert get_headers["x-request-id"] == "direct-request-123"
    assert get_headers["x-dashboard-snapshot-id"] == "fresh-1"
    assert head_headers["x-dashboard-snapshot-id"] == "fresh-2"
    for headers in (get_headers, head_headers):
        assert headers["cache-control"] == "no-store"
        assert headers["vary"].casefold() == "accept-encoding"
        assert "x-dashboard-cache" not in headers
        assert "x-dashboard-snapshot-age" not in headers
        assert "x-dashboard-warning" not in headers


def test_dashboard_accepts_only_exact_detail_query(monkeypatch) -> None:
    from backend import main

    calls: list[dict[str, str]] = []
    detail = {
        "schema_version": "factor-lab-dashboard-v6",
        "representation": "detail",
        "snapshot_id": "detail-1",
        "generated_at": "2026-08-07T16:41:00+08:00",
        "display_until": "2026-08-07",
        "live_target_start_date": "2026-06-01",
        "scheme_id": "demo__h1__5Y",
        "month": "2026-08",
        "source": "all",
        "row_fields": [],
        "rows": [],
    }

    def build(_engine: object, **kwargs: str):
        calls.append(kwargs)
        return detail

    monkeypatch.setattr(main, "get_dashboard_engine", object)
    monkeypatch.setattr(main, "build_factor_lab_dashboard_detail", build)
    status, _, body, _ = _request(
        main.app,
        query_string=(
            b"scheme-id=demo__h1__5Y&month=2026-08&source=all"
        ),
        headers=[(b"accept-encoding", b"identity")],
    )

    assert status == 200
    assert json.loads(body)["representation"] == "detail"
    assert calls == [
        {
            "scheme_id": "demo__h1__5Y",
            "month": "2026-08",
            "source": "all",
        }
    ]

    for invalid in (
        b"ignored=",
        b"scheme-id=demo__h1__5Y&month=2026-08",
        b"scheme-id=demo__h1__5Y&month=2026-08&source=all&extra=1",
        b"scheme-id=demo__h1__5Y&scheme-id=demo__h1__5Y&month=2026-08&source=all",
    ):
        invalid_status, _, _, _ = _request(main.app, query_string=invalid)
        assert invalid_status == 400


def test_dashboard_gzip_and_identity_are_one_representation(monkeypatch) -> None:
    from backend import main

    payload = _payload("fresh-compressed")
    # 只用于跨过压缩阈值；route 不应因表示协商而改变 JSON 语义。
    payload["padding"] = "x" * 1_000
    monkeypatch.setattr(main, "get_dashboard_engine", object)
    monkeypatch.setattr(main, "build_factor_lab_dashboard", lambda _engine: payload)

    _, identity_headers, identity_body, _ = _request(
        main.app,
        headers=[(b"accept-encoding", b"gzip;q=0")],
    )
    _, gzip_headers, gzip_body, _ = _request(
        main.app,
        headers=[(b"accept-encoding", b"gzip")],
    )

    assert "content-encoding" not in identity_headers
    assert gzip_headers["content-encoding"] == "gzip"
    decoded_gzip = gzip.decompress(gzip_body)
    assert json.loads(decoded_gzip) == json.loads(identity_body)


def test_dashboard_replaces_untrusted_request_id_before_logging(monkeypatch, caplog) -> None:
    from backend import main

    monkeypatch.setattr(main, "get_dashboard_engine", object)
    monkeypatch.setattr(
        main,
        "build_factor_lab_dashboard",
        lambda _engine: _payload("fresh-request-id"),
    )
    injected = "bad-request-id\nforged-log-line"

    with caplog.at_level("INFO", logger="uvicorn.error"):
        status, headers, _, _ = _request(
            main.app,
            headers=[(b"x-request-id", injected.encode("latin-1"))],
        )

    assert status == 200
    assert re.fullmatch(r"[0-9a-f]{32}", headers["x-request-id"])
    assert injected not in caplog.text
    assert "forged-log-line" not in caplog.text


@pytest.mark.parametrize("budget", ("raw", "gzip"))
def test_dashboard_route_enforces_real_encoding_budgets(
    monkeypatch,
    caplog,
    budget: str,
) -> None:
    from backend import main
    from backend.factor_lab_dashboard import MAX_RAW_JSON_BYTES

    payload = _payload(f"oversized-{budget}")
    payload["padding"] = (
        "x" * (MAX_RAW_JSON_BYTES + 1)
        if budget == "raw"
        else base64.b64encode(
            random.Random(0).randbytes(200_000)
        ).decode("ascii")
    )
    monkeypatch.setattr(main, "get_dashboard_engine", object)
    monkeypatch.setattr(
        main,
        "build_factor_lab_dashboard",
        lambda _engine: payload,
    )

    with caplog.at_level("INFO", logger="uvicorn.error"):
        status, _, body, _ = _request(main.app)

    assert status == 503
    assert json.loads(body) == {"error_code": "dashboard_data_unavailable"}
    event = caplog.records[-1].dashboard_event
    assert event["failure_stage"] == "dashboard_encoding"
    assert event["exception_class"] == "DashboardDataError"


def test_dashboard_503_logs_only_safe_failure_diagnostics(
    monkeypatch,
    caplog,
) -> None:
    from backend import main

    class DatabaseSecretError(RuntimeError):
        pass

    secret = "mysql://user:password@host/db SELECT private_column"

    def fail(_engine: object) -> dict[str, Any]:
        raise DatabaseSecretError(secret)

    monkeypatch.setattr(main, "get_dashboard_engine", object)
    monkeypatch.setattr(main, "build_factor_lab_dashboard", fail)

    with caplog.at_level("INFO", logger="uvicorn.error"):
        status, _, body, _ = _request(main.app)

    assert status == 503
    assert json.loads(body) == {"error_code": "dashboard_data_unavailable"}
    event = caplog.records[-1].dashboard_event
    assert event["failure_stage"] == "dashboard_build"
    assert event["exception_class"] == "DatabaseSecretError"
    assert secret not in caplog.text
    assert "private_column" not in caplog.text
