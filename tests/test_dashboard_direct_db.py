from __future__ import annotations

import asyncio
import json
from typing import Any


DASHBOARD_PATH = "/api/factor-lab/dashboard"


def _payload(snapshot_id: str) -> dict[str, Any]:
    """足够通过 route 编码的最小 dashboard 结果。"""
    return {
        "schema_version": "factor-lab-dashboard-v1",
        "snapshot_id": snapshot_id,
        "generated_at": "2026-08-07T16:41:00+08:00",
        "display_until": "2026-08-07",
        "stale": False,
        "snapshot_age_ms": 0,
        "row_fields": [],
        "target_labels": {},
        "schemes": [],
    }


async def _asgi_request(app) -> tuple[int, dict[str, str], bytes]:
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
            "method": "GET",
            "scheme": "http",
            "path": DASHBOARD_PATH,
            "raw_path": DASHBOARD_PATH.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 43123),
            "server": ("127.0.0.1", 18101),
        },
        receive,
        send,
    )
    start = next(item for item in messages if item["type"] == "http.response.start")
    headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in start["headers"]
    }
    body = b"".join(
        item.get("body", b"")
        for item in messages
        if item["type"] == "http.response.body"
    )
    return start["status"], headers, body


def _request(app) -> tuple[int, dict[str, str], bytes]:
    return asyncio.run(_asgi_request(app))


def test_dashboard_reads_builder_for_every_request_without_lkg_store(
    monkeypatch,
) -> None:
    from backend import main

    engine = object()
    calls: list[object] = []

    def build(received_engine: object) -> dict[str, Any]:
        calls.append(received_engine)
        return _payload(f"fresh-{len(calls)}")

    monkeypatch.setattr(main, "get_dashboard_engine", lambda: engine)
    monkeypatch.setattr(main, "build_factor_lab_dashboard", build)

    first_status, first_headers, first_body = _request(main.app)
    second_status, second_headers, second_body = _request(main.app)

    assert first_status == second_status == 200
    assert json.loads(first_body)["snapshot_id"] == "fresh-1"
    assert json.loads(second_body)["snapshot_id"] == "fresh-2"
    assert calls == [engine, engine]
    for headers in (first_headers, second_headers):
        assert headers["cache-control"] == "no-store"
        assert "x-dashboard-cache" not in headers
        assert "x-dashboard-snapshot-age" not in headers
        assert "x-dashboard-warning" not in headers


def test_dashboard_failure_after_a_success_returns_503_not_old_payload(
    monkeypatch,
) -> None:
    from backend import main

    calls = 0

    def build(_engine: object) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _payload("fresh-before-failure")
        raise RuntimeError("mysql://secret-user@secret-host")

    monkeypatch.setattr(main, "get_dashboard_engine", object)
    monkeypatch.setattr(main, "build_factor_lab_dashboard", build)

    first_status, _, first_body = _request(main.app)
    second_status, second_headers, second_body = _request(main.app)

    assert first_status == 200
    assert json.loads(first_body)["snapshot_id"] == "fresh-before-failure"
    assert second_status == 503
    assert json.loads(second_body) == {"error_code": "dashboard_data_unavailable"}
    assert b"fresh-before-failure" not in second_body
    assert b"secret" not in second_body
    assert second_headers["cache-control"] == "no-store"
