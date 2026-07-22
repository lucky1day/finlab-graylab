from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from backend.dashboard_snapshot import (
    DashboardSnapshotStore,
    SnapshotResult,
    SnapshotUnavailable,
)
from backend.factor_lab_dashboard import (
    MAX_GZIP_JSON_BYTES,
    MAX_RAW_JSON_BYTES,
)
from backend.factor_lab_dashboard_semantics import DashboardDataError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_PATH = "/api/factor-lab/dashboard"


def _payload(*, snapshot_id: str = "snapshot-test") -> dict[str, Any]:
    return {
        "schema_version": "factor-lab-dashboard-v1",
        "snapshot_id": snapshot_id,
        "generated_at": "2026-07-22T12:00:00+08:00",
        "display_until": "2026-07-22",
        "stale": False,
        "snapshot_age_ms": 0,
        "row_fields": [
            "predict_date",
            "feature_date",
            "target_date",
            "prediction_phase",
            "predicted_direction",
            "actual_direction",
        ],
        "target_labels": {"10Y": "10Y国债活跃"},
        "schemes": [
            {
                "scheme_id": "secret-scheme-must-not-be-logged__h1__10Y",
                "description": "x" * 1_000,
                "live_rows": [],
                "backtest": None,
            }
        ],
    }


class _FixedStore:
    def __init__(
        self,
        result: SnapshotResult | None = None,
        *,
        error: BaseException | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.get_calls = 0
        self.prewarm_calls = 0
        self._diagnostics = diagnostics or {
            "active_waiter_count": 0,
            "last_waiter_count": 0,
            "last_build_seconds": None,
        }

    def get(self) -> SnapshotResult:
        self.get_calls += 1
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    def prewarm(self) -> SnapshotResult:
        self.prewarm_calls += 1
        return self.get()

    def diagnostics(self) -> dict[str, Any]:
        return dict(self._diagnostics)


async def _asgi_request(
    app,
    path: str,
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
            return {
                "type": "http.request",
                "body": b"",
                "more_body": False,
            }
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query_string,
        "root_path": "",
        "headers": headers or [],
        "client": ("127.0.0.1", 43123),
        "server": ("127.0.0.1", 18101),
    }
    await app(scope, receive, send)
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


def _request(
    app,
    path: str = DASHBOARD_PATH,
    *,
    method: str = "GET",
    query_string: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, dict[str, str], bytes, list[dict[str, Any]]]:
    return asyncio.run(
        _asgi_request(
            app,
            path,
            method=method,
            query_string=query_string,
            headers=headers,
        )
    )


@pytest.fixture
def main_module(monkeypatch):
    from backend import main

    original = (
        main._dashboard_health_snapshot()
        if hasattr(main, "_dashboard_health_snapshot")
        else None
    )
    monkeypatch.setattr(
        main,
        "dashboard_build_diagnostics",
        lambda snapshot_id: {
            "snapshot_id": snapshot_id,
            "db_read_seconds": 0.012,
            "canonical_build_seconds": 0.023,
            "canonical_serialization_seconds": 0.004,
            "scheme_count": 1,
            "live_row_count": 2,
            "backtest_row_count": 3,
            "detail_row_count": 5,
            "raw_bytes": 1_234,
            "gzip_bytes": 456,
        },
        raising=False,
    )
    yield main
    if original is not None and hasattr(main, "_set_dashboard_health"):
        main._set_dashboard_health(original["status"], original["error_code"])


def test_module_import_is_lazy_and_does_not_touch_database_or_build_snapshot() -> None:
    code = """
from unittest.mock import patch
with patch('backend.db.get_engine') as engine, patch(
    'backend.factor_lab_dashboard.build_factor_lab_dashboard'
) as build:
    import backend.main
    assert engine.call_count == 0
    assert build.call_count == 0
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_dashboard_get_returns_schema_no_store_vary_and_observability_headers(
    main_module,
    monkeypatch,
) -> None:
    result = SnapshotResult(_payload(), "MISS", 0.012, 0.031)
    store = _FixedStore(result)
    monkeypatch.setattr(main_module, "dashboard_snapshot_store", store)

    status, headers, raw_body, _ = _request(
        main_module.app,
        headers=[(b"x-request-id", b"edge-request-123")],
    )

    body = json.loads(raw_body)
    assert status == 200
    assert body["schema_version"] == "factor-lab-dashboard-v1"
    assert body["snapshot_id"] == "snapshot-test"
    assert body["stale"] is False
    assert body["snapshot_age_ms"] == 12
    assert headers["content-type"] == "application/json"
    assert headers["cache-control"] == "no-store"
    assert headers["vary"].casefold() == "accept-encoding"
    assert headers["x-request-id"] == "edge-request-123"
    assert headers["x-dashboard-snapshot-id"] == "snapshot-test"
    assert headers["x-dashboard-cache"] == "MISS"
    assert headers["x-dashboard-snapshot-age"] == "12"
    assert "serialization;dur=" in headers["server-timing"]
    assert "route;dur=" in headers["server-timing"]
    assert "x-dashboard-warning" not in headers


@pytest.mark.parametrize("accept_encoding", [b"gzip", b"identity"])
def test_dashboard_head_matches_get_representation_headers_and_has_no_wire_body(
    main_module,
    monkeypatch,
    accept_encoding: bytes,
) -> None:
    store = _FixedStore(SnapshotResult(_payload(), "HIT", 0.0, None))
    monkeypatch.setattr(main_module, "dashboard_snapshot_store", store)
    request_headers = [(b"accept-encoding", accept_encoding)]

    get_status, get_headers, get_body, _ = _request(
        main_module.app,
        headers=request_headers,
    )
    head_status, head_headers, head_body, head_messages = _request(
        main_module.app,
        method="HEAD",
        headers=request_headers,
    )

    assert head_status == get_status == 200
    for name in (
        "content-type",
        "content-length",
        "cache-control",
        "vary",
        "x-dashboard-cache",
        "x-dashboard-snapshot-id",
        "x-dashboard-snapshot-age",
    ):
        assert head_headers[name] == get_headers[name]
    assert head_headers.get("content-encoding") == get_headers.get("content-encoding")
    assert get_body
    assert head_body == b""
    assert all(
        item.get("body", b"") == b""
        for item in head_messages
        if item["type"] == "http.response.body"
    )


def test_dashboard_rejects_every_nonempty_query_before_cache_access(
    main_module,
    monkeypatch,
) -> None:
    store = _FixedStore(SnapshotResult(_payload(), "HIT", 0.0, None))
    monkeypatch.setattr(main_module, "dashboard_snapshot_store", store)

    status, _, body, _ = _request(
        main_module.app,
        query_string=b"ignored=",
    )

    assert status == 400
    assert json.loads(body)["error_code"] == "dashboard_query_not_allowed"
    assert store.get_calls == 0


@pytest.mark.parametrize(
    "failure",
    [
        SnapshotUnavailable("secret database hostname"),
        DashboardDataError("secret malformed scheme row"),
    ],
)
def test_dashboard_without_lkg_returns_stable_503_without_exception_text(
    main_module,
    monkeypatch,
    failure: Exception,
) -> None:
    store = _FixedStore(error=failure)
    monkeypatch.setattr(main_module, "dashboard_snapshot_store", store)

    status, headers, body, _ = _request(main_module.app)

    decoded = body.decode("utf-8")
    assert status == 503
    assert json.loads(decoded) == {
        "error_code": "dashboard_snapshot_unavailable"
    }
    assert "secret" not in decoded
    assert headers["cache-control"] == "no-store"
    assert headers["x-dashboard-cache"] == "UNAVAILABLE"
    assert headers["x-dashboard-snapshot-id"] == "unavailable"
    assert main_module._dashboard_health_snapshot() == {
        "status": "degraded",
        "error_code": "dashboard_snapshot_unavailable",
    }


def test_stale_lkg_is_visible_in_body_headers_and_health(
    main_module,
    monkeypatch,
) -> None:
    canonical = _payload()
    store = _FixedStore(SnapshotResult(canonical, "STALE", 1.2349, None))
    monkeypatch.setattr(main_module, "dashboard_snapshot_store", store)

    status, headers, body, _ = _request(main_module.app)

    decoded = json.loads(body)
    assert status == 200
    assert decoded["stale"] is True
    assert decoded["snapshot_age_ms"] == 1_234
    assert headers["x-dashboard-warning"] == "stale-last-known-good"
    assert headers["x-dashboard-cache"] == "STALE"
    assert main_module._dashboard_health_snapshot() == {
        "status": "degraded",
        "error_code": "dashboard_snapshot_stale",
    }
    assert canonical["stale"] is False
    assert canonical["snapshot_age_ms"] == 0


def test_gzip_and_identity_decode_to_equal_json_without_double_compression(
    main_module,
    monkeypatch,
) -> None:
    store = _FixedStore(SnapshotResult(_payload(), "HIT", 0.0, None))
    monkeypatch.setattr(main_module, "dashboard_snapshot_store", store)

    _, identity_headers, identity_body, _ = _request(
        main_module.app,
        headers=[(b"accept-encoding", b"gzip;q=0")],
    )
    _, gzip_headers, gzip_body, _ = _request(
        main_module.app,
        headers=[(b"accept-encoding", b"gzip")],
    )

    assert "content-encoding" not in identity_headers
    assert gzip_headers["content-encoding"] == "gzip"
    once = gzip.decompress(gzip_body)
    assert json.loads(identity_body) == json.loads(once)
    assert len(identity_body) <= MAX_RAW_JSON_BYTES
    assert len(gzip_body) <= MAX_GZIP_JSON_BYTES
    with pytest.raises((gzip.BadGzipFile, EOFError)):
        gzip.decompress(once)


@pytest.mark.parametrize(
    ("path", "query_string"),
    [
        ("/aifin-shell.js", b"v=20260721a"),
        ("/aifin-shell.css", b"v=20260705a"),
        ("/assets/aifin-lab-logo.svg", b""),
        ("/assets/aifin-lab-icon.svg", b""),
    ],
)
def test_frontend_assets_are_gzipped_once_at_fastapi(
    main_module,
    path: str,
    query_string: bytes,
) -> None:
    status, identity_headers, identity_body, _ = _request(
        main_module.app,
        path,
        query_string=query_string,
        headers=[(b"accept-encoding", b"gzip;q=0")],
    )
    gzip_status, gzip_headers, gzip_body, _ = _request(
        main_module.app,
        path,
        query_string=query_string,
        headers=[(b"accept-encoding", b"gzip")],
    )

    assert status == gzip_status == 200
    assert "content-encoding" not in identity_headers
    assert gzip_headers["content-encoding"] == "gzip"
    once = gzip.decompress(gzip_body)
    assert once == identity_body
    with pytest.raises((gzip.BadGzipFile, EOFError)):
        gzip.decompress(once)


def test_invalid_request_id_is_replaced_and_never_logged(
    main_module,
    monkeypatch,
    caplog,
) -> None:
    store = _FixedStore(SnapshotResult(_payload(), "HIT", 0.0, None))
    monkeypatch.setattr(main_module, "dashboard_snapshot_store", store)
    injected = "bad-request-id\nforged-log-line"

    with caplog.at_level(logging.INFO, logger="backend.main"):
        _, headers, _, _ = _request(
            main_module.app,
            headers=[(b"x-request-id", injected.encode("latin-1"))],
        )

    assert re.fullmatch(r"[0-9a-f]{32}", headers["x-request-id"])
    assert injected not in caplog.text
    assert "forged-log-line" not in caplog.text


def test_structured_request_log_uses_build_diagnostics_without_payload_scan(
    main_module,
    monkeypatch,
    caplog,
) -> None:
    store = _FixedStore(
        SnapshotResult(_payload(), "HIT", 0.125, None),
        diagnostics={
            "active_waiter_count": 0,
            "last_waiter_count": 4,
            "last_build_seconds": 0.082,
        },
    )
    monkeypatch.setattr(main_module, "dashboard_snapshot_store", store)

    with caplog.at_level(logging.INFO, logger="backend.main"):
        status, headers, _, _ = _request(
            main_module.app,
            headers=[(b"x-request-id", b"request-observe")],
        )

    record = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("factor_lab_dashboard_request ")
    )
    message = record.getMessage()
    event = json.loads(message.split(" ", 1)[1])
    assert record.dashboard_event == event
    assert status == 200
    assert event["db_read_ms"] == pytest.approx(12.0)
    assert event["canonical_build_ms"] == pytest.approx(23.0)
    assert event["canonical_serialization_ms"] == pytest.approx(4.0)
    assert event["build_ms"] == pytest.approx(82.0)
    assert event["request_id"] == "request-observe"
    assert event["snapshot_id"] == "snapshot-test"
    assert event["cache"] == "HIT"
    assert event["waiter_count"] == 4
    assert event["scheme_count"] == 1
    assert event["live_row_count"] == 2
    assert event["backtest_row_count"] == 3
    assert event["raw_bytes"] == int(headers["content-length"])
    assert event["gzip_bytes"] > 0
    assert "secret-scheme-must-not-be-logged" not in message
    assert "SELECT " not in message
    assert "connection" not in message.casefold()


def test_startup_sync_attempt_precedes_prewarm_and_success_sets_ready(
    main_module,
    monkeypatch,
) -> None:
    events: list[str] = []
    engine = object()
    store = _FixedStore(SnapshotResult(_payload(), "MISS", 0.0, 0.01))

    def sync(received_engine) -> None:
        assert received_engine is engine
        events.append("sync")

    original_prewarm = store.prewarm

    def prewarm() -> SnapshotResult:
        events.append("prewarm")
        return original_prewarm()

    monkeypatch.setattr(store, "prewarm", prewarm)
    monkeypatch.setattr(main_module, "get_engine", lambda: engine)
    monkeypatch.setattr(main_module, "sync_registry_from_configs", sync)
    monkeypatch.setattr(main_module, "dashboard_snapshot_store", store)

    main_module._sync_registry_on_startup()

    assert events == ["sync", "prewarm"]
    assert main_module._dashboard_health_snapshot() == {
        "status": "ready",
        "error_code": None,
    }


def test_startup_prewarm_failure_is_degraded_stable_and_does_not_leak(
    main_module,
    monkeypatch,
    caplog,
) -> None:
    store = _FixedStore(error=SnapshotUnavailable("mysql://secret@host"))
    monkeypatch.setattr(main_module, "get_engine", lambda: object())
    monkeypatch.setattr(main_module, "sync_registry_from_configs", lambda engine: None)
    monkeypatch.setattr(main_module, "dashboard_snapshot_store", store)

    with caplog.at_level(logging.ERROR, logger="backend.main"):
        main_module._sync_registry_on_startup()

    assert store.prewarm_calls == 1
    assert main_module._dashboard_health_snapshot() == {
        "status": "degraded",
        "error_code": "dashboard_snapshot_unavailable",
    }
    assert "mysql://secret@host" not in caplog.text


def test_successful_fresh_request_recovers_health_to_ready(
    main_module,
    monkeypatch,
) -> None:
    main_module._set_dashboard_health(
        "degraded",
        "dashboard_snapshot_unavailable",
    )
    monkeypatch.setattr(
        main_module,
        "dashboard_snapshot_store",
        _FixedStore(SnapshotResult(_payload(), "HIT", 0.0, None)),
    )

    status, _, _, _ = _request(main_module.app)

    assert status == 200
    assert main_module._dashboard_health_snapshot() == {
        "status": "ready",
        "error_code": None,
    }


def test_snapshot_store_diagnostics_report_real_waiter_counts() -> None:
    build_started = threading.Event()
    release_build = threading.Event()
    waiter_started_waiting = threading.Event()
    results: list[SnapshotResult] = []

    class _NotifyingCondition(threading.Condition):
        def wait(self, timeout: float | None = None) -> bool:
            waiter_started_waiting.set()
            return super().wait(timeout)

    def builder() -> dict[str, Any]:
        build_started.set()
        assert release_build.wait(timeout=3.0)
        return _payload()

    store = DashboardSnapshotStore(builder, wait_timeout_seconds=2.0)
    store._condition = _NotifyingCondition()
    owner = threading.Thread(target=lambda: results.append(store.get()))
    waiter = threading.Thread(target=lambda: results.append(store.get()))
    owner.start()
    assert build_started.wait(timeout=3.0)
    waiter.start()

    assert waiter_started_waiting.wait(timeout=3.0)
    assert store.diagnostics()["active_waiter_count"] == 1
    release_build.set()
    owner.join(timeout=3.0)
    waiter.join(timeout=3.0)

    diagnostics = store.diagnostics()
    assert diagnostics["active_waiter_count"] == 0
    assert diagnostics["last_waiter_count"] == 1
    assert diagnostics["last_build_seconds"] is not None
    assert len(results) == 2


def test_failure_timing_cancellation_still_releases_snapshot_flight() -> None:
    class _FailureTimingCancellationStore(DashboardSnapshotStore):
        def __init__(self) -> None:
            super().__init__(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
            self._samples = iter([0.0])

        def _sample_monotonic(self) -> float:
            try:
                return next(self._samples)
            except StopIteration:
                raise SystemExit("cancel while sampling failed build")

    store = _FailureTimingCancellationStore()

    with pytest.raises(SystemExit, match="cancel while sampling failed build"):
        store.get()

    assert store._flight is None
    assert store.diagnostics()["active_waiter_count"] == 0
