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
from backend import dashboard_snapshot as snapshot_module
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

    def request_diagnostics(self) -> dict[str, Any]:
        return {
            "observation_revision": self.get_calls,
            "request_waited": False,
            "build_waiter_count": self._diagnostics.get(
                "last_waiter_count",
                0,
            ),
            "snapshot_generation": 1,
            "cache_status": (
                None if self.result is None else self.result.cache_status
            ),
            "attempt_status": None,
            "attempt_seconds": None,
            "attempt_id": None,
        }


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
    if original is not None and hasattr(
        main,
        "_reset_dashboard_health_for_tests",
    ):
        main._reset_dashboard_health_for_tests(
            original["status"],
            original["error_code"],
        )


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
    assert "request_compact_json_budget_gzip;dur=" in headers["server-timing"]
    assert "request_route;dur=" in headers["server-timing"]
    assert "snapshot_origin_db;dur=" in headers["server-timing"]
    assert "snapshot_origin_build;dur=" in headers["server-timing"]
    timing_names = {
        item.split(";", 1)[0]
        for item in headers["server-timing"].split(", ")
    }
    assert "db" not in timing_names
    assert "build" not in timing_names
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
    base_request_diagnostics = store.request_diagnostics()
    base_request_diagnostics.update(
        {
            "attempt_id": 7,
            "attempt_status": "failed",
            "attempt_seconds": 0.005,
        }
    )
    monkeypatch.setattr(
        store,
        "request_diagnostics",
        lambda: dict(base_request_diagnostics),
    )

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
    assert event["snapshot_origin_db_ms"] == pytest.approx(12.0)
    assert event["snapshot_origin_canonical_ms"] == pytest.approx(23.0)
    assert event["snapshot_origin_serialization_ms"] == pytest.approx(4.0)
    assert event["snapshot_origin_build_ms"] == pytest.approx(82.0)
    assert event["request_id"] == "request-observe"
    assert event["snapshot_id"] == "snapshot-test"
    assert event["cache"] == "HIT"
    assert event["active_waiter_count"] == 0
    assert event["request_waited"] is False
    assert event["build_waiter_count"] == 4
    assert event["associated_refresh_attempt_id"] == 7
    assert event["associated_refresh_attempt_status"] == "failed"
    assert event["associated_refresh_attempt_ms"] == pytest.approx(5.0)
    assert event["scheme_count"] == 1
    assert event["live_row_count"] == 2
    assert event["backtest_row_count"] == 3
    assert event["response_raw_bytes"] == int(headers["content-length"])
    assert event["response_budget_gzip_bytes"] > 0
    assert event["request_compact_json_budget_gzip_ms"] >= 0.0
    assert "db_read_ms" not in event
    assert "build_ms" not in event
    assert "waiter_count" not in event
    assert "request_attempt_status" not in event
    assert "request_attempt_ms" not in event
    timing_names = {
        item.split(";", 1)[0]
        for item in headers["server-timing"].split(", ")
    }
    assert "associated_refresh_attempt" in timing_names
    assert "request_refresh_attempt" not in timing_names
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
    main_module._reset_dashboard_health_for_tests(
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


def test_health_setter_without_observation_revision_cannot_reopen_race(
    main_module,
) -> None:
    assert main_module._set_dashboard_health(
        "ready",
        None,
        observation_revision=100,
    )

    updated = main_module._set_dashboard_health(
        "degraded",
        "dashboard_snapshot_stale",
    )

    assert updated is False
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


def test_active_waiter_count_survives_flight_detach_until_waiter_resumes() -> None:
    rebuild_started = threading.Event()
    release_rebuild = threading.Event()
    call_count = 0

    class _ResumeGateCondition(threading.Condition):
        def __init__(self) -> None:
            super().__init__()
            self.wait_started = threading.Event()
            self.resume_entered = threading.Event()
            self.resume_release = threading.Event()

        def wait(self, timeout: float | None = None) -> bool:
            self.wait_started.set()
            notified = super().wait(timeout)
            self.release()
            try:
                self.resume_entered.set()
                assert self.resume_release.wait(timeout=3.0)
            finally:
                self.acquire()
            return notified

    def builder() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _payload(snapshot_id="old")
        rebuild_started.set()
        assert release_rebuild.wait(timeout=3.0)
        return _payload(snapshot_id="new")

    class _Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = _Clock()
    store = DashboardSnapshotStore(builder, monotonic=clock)
    condition = _ResumeGateCondition()
    store._condition = condition
    store.get()
    clock.value = 2.0
    results: dict[str, SnapshotResult] = {}
    owner = threading.Thread(
        target=lambda: results.setdefault("owner", store.get()),
        name="owner",
    )
    waiter = threading.Thread(
        target=lambda: results.setdefault("waiter", store.get()),
        name="waiter",
    )
    owner.start()
    assert rebuild_started.wait(timeout=3.0)
    waiter.start()
    assert condition.wait_started.wait(timeout=3.0)
    release_rebuild.set()
    assert condition.resume_entered.wait(timeout=3.0)
    owner.join(timeout=3.0)

    try:
        assert store._flight is None
        assert store.diagnostics()["active_waiter_count"] == 1
    finally:
        condition.resume_release.set()
        waiter.join(timeout=3.0)

    assert not owner.is_alive()
    assert not waiter.is_alive()
    assert store.diagnostics()["active_waiter_count"] == 0


def test_request_diagnostics_distinguish_hit_from_snapshot_origin_build() -> None:
    store = DashboardSnapshotStore(lambda: _payload())

    store.get()
    miss_diagnostics = store.request_diagnostics()
    hit = store.get()
    hit_diagnostics = store.request_diagnostics()

    assert miss_diagnostics["request_waited"] is False
    assert miss_diagnostics["attempt_status"] == "success"
    assert hit.cache_status == "HIT"
    assert hit_diagnostics["request_waited"] is False
    assert hit_diagnostics["build_waiter_count"] == 0
    assert hit_diagnostics["attempt_status"] is None
    assert hit_diagnostics["attempt_seconds"] is None
    assert hit_diagnostics["snapshot_generation"] == 1


def test_failed_refresh_reports_real_attempt_instead_of_last_success() -> None:
    class _Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = _Clock()
    call_count = 0

    def builder() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _payload(snapshot_id="old")
        clock.value += 0.125
        raise RuntimeError("refresh failed")

    store = DashboardSnapshotStore(builder, monotonic=clock)
    store.get()
    clock.value = 2.0

    result = store.get()
    request_diagnostics = store.request_diagnostics()
    diagnostics = store.diagnostics()

    assert result.cache_status == "STALE"
    assert request_diagnostics["attempt_status"] == "failed"
    assert request_diagnostics["attempt_seconds"] == pytest.approx(0.125)
    assert diagnostics["last_attempt_status"] == "failed"
    assert diagnostics["last_attempt_seconds"] == pytest.approx(0.125)
    assert diagnostics["snapshot_origin_build_seconds"] == pytest.approx(0.0)


def test_waiter_timeout_reports_real_attempt_duration(
    monkeypatch,
) -> None:
    rebuild_started = threading.Event()
    release_rebuild = threading.Event()
    call_count = 0

    class _Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    def builder() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _payload(snapshot_id="old")
        rebuild_started.set()
        assert release_rebuild.wait(timeout=3.0)
        return _payload(snapshot_id="new")

    samples = iter([10.0, 10.0, 10.25])
    monkeypatch.setattr(
        snapshot_module,
        "_sample_wait_monotonic",
        lambda: next(samples),
    )
    clock = _Clock()
    store = DashboardSnapshotStore(
        builder,
        monotonic=clock,
        wait_timeout_seconds=0.0,
    )
    store.get()
    clock.value = 2.0
    owner = threading.Thread(target=store.get)
    owner.start()
    assert rebuild_started.wait(timeout=3.0)

    try:
        result = store.get()
        diagnostics = store.request_diagnostics()
        terminal_during_timeout = store.diagnostics()
    finally:
        release_rebuild.set()
        owner.join(timeout=3.0)

    assert result.cache_status == "STALE"
    assert diagnostics["attempt_status"] == "timeout"
    assert diagnostics["attempt_seconds"] == pytest.approx(0.25)
    assert terminal_during_timeout["last_attempt_id"] == 1
    assert terminal_during_timeout["last_attempt_status"] == "success"
    assert store.diagnostics()["last_attempt_id"] == 2
    assert store.diagnostics()["last_attempt_status"] == "success"


def test_error_response_measures_json_encoding_time(
    main_module,
    monkeypatch,
    caplog,
) -> None:
    samples = iter([1.001, 1.004, 1.006])
    monkeypatch.setattr(main_module.time, "perf_counter", lambda: next(samples))

    with caplog.at_level(logging.INFO, logger="backend.main"):
        response = main_module._dashboard_error_response(
            request_id="error-timing",
            error_code="dashboard_snapshot_unavailable",
            status_code=503,
            route_started_at=1.0,
        )

    event = next(
        record.dashboard_event
        for record in caplog.records
        if record.getMessage().startswith("factor_lab_dashboard_request ")
    )
    assert response.status_code == 503
    assert event["request_json_encoding_ms"] == pytest.approx(3.0)
    assert event["request_route_ms"] == pytest.approx(6.0)
    assert "request_json_encoding;dur=3.000" in response.headers[
        "server-timing"
    ]


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


class _ThreadRevisionStore:
    def __init__(self, outcomes: dict[str, tuple[Any, int]]) -> None:
        self._outcomes = outcomes
        self._local = threading.local()

    def get(self) -> SnapshotResult:
        outcome, revision = self._outcomes[threading.current_thread().name]
        self._local.diagnostics = {
            "observation_revision": revision,
            "request_waited": False,
            "build_waiter_count": 0,
            "snapshot_generation": revision,
            "cache_status": (
                "UNAVAILABLE"
                if isinstance(outcome, BaseException)
                else outcome.cache_status
            ),
        }
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def request_diagnostics(self) -> dict[str, Any]:
        return dict(self._local.diagnostics)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "active_waiter_count": 0,
            "last_waiter_count": 0,
            "last_build_seconds": None,
        }


def _direct_request() -> Any:
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": DASHBOARD_PATH,
            "query_string": b"",
            "headers": [],
        }
    )


def _run_dashboard_in_thread(
    main_module,
    *,
    name: str,
    responses: dict[str, Any],
    errors: dict[str, BaseException],
) -> threading.Thread:
    def target() -> None:
        try:
            responses[name] = main_module._factor_lab_dashboard_response(
                _direct_request()
            )
        except BaseException as error:
            errors[name] = error

    thread = threading.Thread(target=target, name=name)
    thread.start()
    return thread


def test_older_stale_response_cannot_overwrite_newer_ready_health(
    main_module,
    monkeypatch,
) -> None:
    stale_encoding_started = threading.Event()
    release_stale_encoding = threading.Event()
    original_encode = main_module.encode_canonical_snapshot
    store = _ThreadRevisionStore(
        {
            "old-stale": (
                SnapshotResult(_payload(snapshot_id="old"), "STALE", 2.0, None),
                1,
            ),
            "new-fresh": (
                SnapshotResult(_payload(snapshot_id="new"), "HIT", 0.0, None),
                2,
            ),
        }
    )

    def controlled_encode(payload: dict[str, Any]):
        if payload["snapshot_id"] == "old":
            stale_encoding_started.set()
            assert release_stale_encoding.wait(timeout=3.0)
        return original_encode(payload)

    monkeypatch.setattr(main_module, "dashboard_snapshot_store", store)
    monkeypatch.setattr(main_module, "encode_canonical_snapshot", controlled_encode)
    main_module._reset_dashboard_health_for_tests(
        "degraded",
        "dashboard_snapshot_stale",
    )
    responses: dict[str, Any] = {}
    errors: dict[str, BaseException] = {}

    old_thread = _run_dashboard_in_thread(
        main_module,
        name="old-stale",
        responses=responses,
        errors=errors,
    )
    assert stale_encoding_started.wait(timeout=3.0)
    new_thread = _run_dashboard_in_thread(
        main_module,
        name="new-fresh",
        responses=responses,
        errors=errors,
    )
    new_thread.join(timeout=3.0)
    assert not new_thread.is_alive()
    release_stale_encoding.set()
    old_thread.join(timeout=3.0)

    assert not old_thread.is_alive()
    assert errors == {}
    assert set(responses) == {"old-stale", "new-fresh"}
    assert main_module._dashboard_health_snapshot() == {
        "status": "ready",
        "error_code": None,
    }


def test_older_fresh_response_cannot_overwrite_newer_failed_health(
    main_module,
    monkeypatch,
) -> None:
    fresh_encoding_started = threading.Event()
    release_fresh_encoding = threading.Event()
    original_encode = main_module.encode_canonical_snapshot
    store = _ThreadRevisionStore(
        {
            "old-fresh": (
                SnapshotResult(_payload(snapshot_id="old"), "HIT", 0.0, None),
                1,
            ),
            "new-failed": (
                SnapshotUnavailable("do not leak"),
                2,
            ),
        }
    )

    def controlled_encode(payload: dict[str, Any]):
        if payload["snapshot_id"] == "old":
            fresh_encoding_started.set()
            assert release_fresh_encoding.wait(timeout=3.0)
        return original_encode(payload)

    monkeypatch.setattr(main_module, "dashboard_snapshot_store", store)
    monkeypatch.setattr(main_module, "encode_canonical_snapshot", controlled_encode)
    main_module._reset_dashboard_health_for_tests("ready", None)
    responses: dict[str, Any] = {}
    errors: dict[str, BaseException] = {}

    old_thread = _run_dashboard_in_thread(
        main_module,
        name="old-fresh",
        responses=responses,
        errors=errors,
    )
    assert fresh_encoding_started.wait(timeout=3.0)
    new_thread = _run_dashboard_in_thread(
        main_module,
        name="new-failed",
        responses=responses,
        errors=errors,
    )
    new_thread.join(timeout=3.0)
    assert not new_thread.is_alive()
    release_fresh_encoding.set()
    old_thread.join(timeout=3.0)

    assert not old_thread.is_alive()
    assert errors == {}
    assert set(responses) == {"old-fresh", "new-failed"}
    assert main_module._dashboard_health_snapshot() == {
        "status": "degraded",
        "error_code": "dashboard_snapshot_unavailable",
    }


def test_owner_publish_observation_is_newer_than_earlier_waiter_timeout() -> None:
    class _Clock:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = _Clock()
    rebuild_started = threading.Event()
    release_rebuild = threading.Event()
    call_count = 0

    def builder() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _payload(snapshot_id="old")
        rebuild_started.set()
        assert release_rebuild.wait(timeout=3.0)
        return _payload(snapshot_id="new")

    store = DashboardSnapshotStore(
        builder,
        monotonic=clock,
        wait_timeout_seconds=0.0,
    )
    store.get()
    assert hasattr(store, "request_diagnostics")
    clock.value = 2.0
    owner_observation: dict[str, Any] = {}

    def owner() -> None:
        store.get()
        owner_observation.update(store.request_diagnostics())

    owner_thread = threading.Thread(target=owner)
    owner_thread.start()
    assert rebuild_started.wait(timeout=3.0)
    stale_result = store.get()
    stale_observation = store.request_diagnostics()
    release_rebuild.set()
    owner_thread.join(timeout=3.0)

    assert stale_result.cache_status == "STALE"
    assert owner_observation["cache_status"] == "MISS"
    assert (
        owner_observation["observation_revision"]
        > stale_observation["observation_revision"]
    )


def test_real_old_failed_owner_prefers_newer_fresh_route_result_and_health(
    main_module,
    monkeypatch,
) -> None:
    class _Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    class _FailureReturnGateCondition(threading.Condition):
        def __init__(self) -> None:
            super().__init__()
            self.failure_finalized = threading.Event()
            self.release_old_result = threading.Event()
            self._old_enters = 0

        def __enter__(self) -> Any:
            if threading.current_thread().name == "old-failed-owner":
                self._old_enters += 1
                if self._old_enters == 3:
                    self.failure_finalized.set()
                    assert self.release_old_result.wait(timeout=3.0)
            return super().__enter__()

    clock = _Clock()
    condition = _FailureReturnGateCondition()
    call_lock = threading.Lock()
    call_count = 0

    def builder() -> dict[str, Any]:
        nonlocal call_count
        with call_lock:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            return _payload(snapshot_id="seed")
        if current_call == 2:
            raise RuntimeError("old refresh failed")
        if current_call == 3:
            return _payload(snapshot_id="recovered")
        raise AssertionError(f"unexpected build {current_call}")

    store = DashboardSnapshotStore(builder, monotonic=clock)
    store._condition = condition
    assert store.get().payload["snapshot_id"] == "seed"
    clock.value = 2.0
    monkeypatch.setattr(main_module, "dashboard_snapshot_store", store)
    main_module._reset_dashboard_health_for_tests("ready", None)
    responses: dict[str, Any] = {}
    diagnostics: dict[str, dict[str, Any]] = {}
    errors: dict[str, BaseException] = {}

    def request_target(name: str) -> None:
        try:
            responses[name] = main_module._factor_lab_dashboard_response(
                _direct_request()
            )
            diagnostics[name] = store.request_diagnostics()
        except BaseException as error:
            errors[name] = error

    old_thread = threading.Thread(
        target=request_target,
        args=("old",),
        name="old-failed-owner",
    )
    old_thread.start()
    assert condition.failure_finalized.wait(timeout=3.0)
    new_thread = threading.Thread(
        target=request_target,
        args=("new",),
        name="new-success-owner",
    )
    new_thread.start()
    new_thread.join(timeout=3.0)
    assert not new_thread.is_alive()

    condition.release_old_result.set()
    old_thread.join(timeout=3.0)

    assert not old_thread.is_alive()
    assert errors == {}
    assert responses["new"].headers["x-dashboard-cache"] == "MISS"
    assert responses["old"].headers["x-dashboard-cache"] == "HIT"
    assert json.loads(responses["old"].body)["snapshot_id"] == "recovered"
    assert json.loads(responses["old"].body)["stale"] is False
    assert diagnostics["old"]["attempt_id"] == 2
    assert diagnostics["old"]["attempt_status"] == "failed"
    assert diagnostics["new"]["attempt_id"] == 3
    assert diagnostics["new"]["attempt_status"] == "success"
    assert (
        diagnostics["old"]["observation_revision"]
        > diagnostics["new"]["observation_revision"]
    )
    assert main_module._dashboard_health_snapshot() == {
        "status": "ready",
        "error_code": None,
    }


def test_late_old_success_waiter_cannot_overwrite_newer_failed_attempt() -> None:
    class _Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    class _ResumeGateCondition(threading.Condition):
        def __init__(self) -> None:
            super().__init__()
            self.wait_started = threading.Event()
            self.resume_entered = threading.Event()
            self.resume_release = threading.Event()

        def wait(self, timeout: float | None = None) -> bool:
            should_gate = threading.current_thread().name == "old-waiter"
            if should_gate:
                self.wait_started.set()
            notified = super().wait(timeout)
            if should_gate:
                self.release()
                try:
                    self.resume_entered.set()
                    assert self.resume_release.wait(timeout=3.0)
                finally:
                    self.acquire()
            return notified

    clock = _Clock()
    condition = _ResumeGateCondition()
    first_started = threading.Event()
    release_first = threading.Event()
    call_count = 0

    def builder() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            first_started.set()
            assert release_first.wait(timeout=3.0)
            return _payload(snapshot_id="attempt-one")
        if call_count == 2:
            clock.value += 0.125
            raise RuntimeError("attempt two failed")
        raise AssertionError(f"unexpected build {call_count}")

    store = DashboardSnapshotStore(
        builder,
        monotonic=clock,
        wait_timeout_seconds=10.0,
    )
    store._condition = condition
    results: dict[str, SnapshotResult] = {}
    errors: dict[str, BaseException] = {}

    owner = threading.Thread(
        target=lambda: results.setdefault("owner", store.get()),
        name="first-owner",
    )
    waiter = threading.Thread(
        target=lambda: results.setdefault("waiter", store.get()),
        name="old-waiter",
    )
    owner.start()
    assert first_started.wait(timeout=3.0)
    waiter.start()
    assert condition.wait_started.wait(timeout=3.0)
    release_first.set()
    assert condition.resume_entered.wait(timeout=3.0)
    owner.join(timeout=3.0)
    assert not owner.is_alive()

    clock.value = 2.0
    newer_failed = store.get()
    assert newer_failed.cache_status == "STALE"
    assert store.diagnostics()["last_attempt_status"] == "failed"

    try:
        condition.resume_release.set()
        waiter.join(timeout=3.0)
    except BaseException as error:
        errors["waiter"] = error

    assert not waiter.is_alive()
    assert errors == {}
    diagnostics = store.diagnostics()
    assert diagnostics["last_attempt_status"] == "failed"
    assert diagnostics["last_attempt_id"] == 2
    assert diagnostics["last_attempt_seconds"] == pytest.approx(0.125)
    assert results["waiter"].cache_status == "STALE"


@pytest.mark.parametrize(
    "failure_stage",
    [
        "request_local_before_registration",
        "registry_before_insert",
        "registry_after_insert",
        "request_local_after_registration",
    ],
)
def test_waiter_registration_cancellation_is_idempotently_removed(
    failure_stage: str,
) -> None:
    signal = KeyboardInterrupt(failure_stage)
    build_started = threading.Event()
    release_build = threading.Event()
    builder_calls = 0

    def builder() -> dict[str, Any]:
        nonlocal builder_calls
        builder_calls += 1
        build_started.set()
        assert release_build.wait(timeout=3.0)
        return _payload(snapshot_id="owner")

    store = DashboardSnapshotStore(
        builder,
        wait_timeout_seconds=0.0,
    )

    class _InterruptingRegistry(dict):
        def __setitem__(self, key: object, value: Any) -> None:
            if threading.current_thread().name == "cancelled-waiter":
                if failure_stage == "registry_before_insert":
                    raise signal
                super().__setitem__(key, value)
                if failure_stage == "registry_after_insert":
                    raise signal
                return
            super().__setitem__(key, value)

    class _InterruptingDiagnostics(dict):
        def __setitem__(self, key: object, value: Any) -> None:
            if (
                threading.current_thread().name == "cancelled-waiter"
                and key == "request_waited"
                and failure_stage == "request_local_after_registration"
            ):
                raise signal
            super().__setitem__(key, value)

    class _RequestLocalProxy:
        def __init__(self, wrapped: Any) -> None:
            self._wrapped = wrapped

        @property
        def diagnostics(self) -> Any:
            return getattr(self._wrapped, "diagnostics", None)

        @diagnostics.setter
        def diagnostics(self, value: Any) -> None:
            if (
                threading.current_thread().name == "cancelled-waiter"
                and failure_stage == "request_local_before_registration"
            ):
                raise signal
            if (
                threading.current_thread().name == "cancelled-waiter"
                and failure_stage == "request_local_after_registration"
            ):
                value = _InterruptingDiagnostics(value)
            self._wrapped.diagnostics = value

    store._waiter_registrations = _InterruptingRegistry()
    store._request_local = _RequestLocalProxy(store._request_local)
    results: dict[str, SnapshotResult] = {}
    errors: dict[str, BaseException] = {}

    def call_get(name: str) -> None:
        try:
            results[name] = store.get()
        except BaseException as error:
            errors[name] = error

    owner = threading.Thread(
        target=call_get,
        args=("owner",),
        name="active-owner",
    )
    waiter = threading.Thread(
        target=call_get,
        args=("waiter",),
        name="cancelled-waiter",
    )
    owner.start()
    assert build_started.wait(timeout=3.0)
    waiter.start()
    waiter.join(timeout=3.0)

    try:
        assert not waiter.is_alive()
        assert errors.get("waiter") is signal
        assert store.diagnostics()["active_waiter_count"] == 0
    finally:
        release_build.set()
        owner.join(timeout=3.0)

    assert not owner.is_alive()
    assert errors.keys() == {"waiter"}
    assert results["owner"].cache_status == "MISS"
    assert builder_calls == 1
    assert store.diagnostics()["active_waiter_count"] == 0


@pytest.mark.parametrize("pop_stage", ["before_pop", "after_pop"])
def test_deregistration_cancellation_preserves_inflight_process_signal(
    pop_stage: str,
    monkeypatch,
) -> None:
    cleanup_signal = KeyboardInterrupt(pop_stage)
    wait_signal = SystemExit("wait sampling cancelled")
    build_started = threading.Event()
    release_build = threading.Event()
    calls = 0

    class _InterruptingPopRegistry(dict):
        def __init__(self) -> None:
            super().__init__()
            self._cancelled = False

        def pop(self, key: object, default: Any = None) -> Any:
            if (
                threading.current_thread().name == "signal-waiter"
                and not self._cancelled
            ):
                self._cancelled = True
                if pop_stage == "before_pop":
                    raise cleanup_signal
                super().pop(key, default)
                raise cleanup_signal
            return super().pop(key, default)

    def builder() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _payload(snapshot_id="seed")
        build_started.set()
        assert release_build.wait(timeout=3.0)
        return _payload(snapshot_id="new")

    class _Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = _Clock()
    store = DashboardSnapshotStore(builder, monotonic=clock)
    store.get()
    clock.value = 2.0
    registry = _InterruptingPopRegistry()
    store._waiter_registrations = registry
    errors: dict[str, BaseException] = {}

    owner = threading.Thread(target=store.get, name="signal-owner")
    owner.start()
    assert build_started.wait(timeout=3.0)
    original_wait_monotonic = snapshot_module._sample_wait_monotonic

    def cancel_wait_sample() -> float:
        if threading.current_thread().name == "signal-waiter":
            raise wait_signal
        return original_wait_monotonic()

    monkeypatch.setattr(
        snapshot_module,
        "_sample_wait_monotonic",
        cancel_wait_sample,
    )

    def waiter_target() -> None:
        try:
            store.get()
        except BaseException as error:
            errors["waiter"] = error

    waiter = threading.Thread(target=waiter_target, name="signal-waiter")
    waiter.start()
    waiter.join(timeout=3.0)

    try:
        assert not waiter.is_alive()
        assert errors.get("waiter") is wait_signal
        assert store.diagnostics()["active_waiter_count"] == 0
        assert dict(registry) == {}
    finally:
        release_build.set()
        owner.join(timeout=3.0)

    assert not owner.is_alive()
    assert calls == 2


@pytest.mark.parametrize("pop_stage", ["before_pop", "after_pop"])
def test_deregistration_cancellation_after_normal_result_is_propagated(
    pop_stage: str,
) -> None:
    cleanup_signal = KeyboardInterrupt(pop_stage)
    build_started = threading.Event()
    release_build = threading.Event()
    calls = 0

    class _InterruptingPopRegistry(dict):
        def __init__(self) -> None:
            super().__init__()
            self._cancelled = False

        def pop(self, key: object, default: Any = None) -> Any:
            if (
                threading.current_thread().name == "normal-waiter"
                and not self._cancelled
            ):
                self._cancelled = True
                if pop_stage == "before_pop":
                    raise cleanup_signal
                super().pop(key, default)
                raise cleanup_signal
            return super().pop(key, default)

    def builder() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _payload(snapshot_id="seed")
        build_started.set()
        assert release_build.wait(timeout=3.0)
        return _payload(snapshot_id="new")

    class _Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = _Clock()
    store = DashboardSnapshotStore(
        builder,
        monotonic=clock,
        wait_timeout_seconds=0.0,
    )
    store.get()
    clock.value = 2.0
    registry = _InterruptingPopRegistry()
    store._waiter_registrations = registry
    errors: dict[str, BaseException] = {}

    owner = threading.Thread(target=store.get, name="normal-owner")
    owner.start()
    assert build_started.wait(timeout=3.0)

    def waiter_target() -> None:
        try:
            store.get()
        except BaseException as error:
            errors["waiter"] = error

    waiter = threading.Thread(target=waiter_target, name="normal-waiter")
    waiter.start()
    waiter.join(timeout=3.0)

    try:
        assert not waiter.is_alive()
        assert errors.get("waiter") is cleanup_signal
        assert store.diagnostics()["active_waiter_count"] == 0
        assert dict(registry) == {}
    finally:
        release_build.set()
        owner.join(timeout=3.0)

    assert not owner.is_alive()
    assert calls == 2


@pytest.mark.parametrize(
    "failure_point",
    ["_record_terminal_attempt", "_record_observation"],
)
def test_success_terminal_notifies_waiter_before_diagnostics_cancellation(
    failure_point: str,
    main_module,
    monkeypatch,
) -> None:
    signal = KeyboardInterrupt(failure_point)
    rebuild_started = threading.Event()
    release_rebuild = threading.Event()
    waiter_done = threading.Event()
    calls = 0

    class _Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    class _NotifyTrackingCondition(threading.Condition):
        def __init__(self) -> None:
            super().__init__()
            self.wait_started = threading.Event()
            self.owner_notified = threading.Event()

        def wait(self, timeout: float | None = None) -> bool:
            if threading.current_thread().name == "diagnostic-waiter":
                self.wait_started.set()
            return super().wait(timeout)

        def notify_all(self) -> None:
            if (
                threading.current_thread().name == "diagnostic-owner"
                and self.wait_started.is_set()
            ):
                self.owner_notified.set()
            super().notify_all()

    def builder() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _payload(snapshot_id="seed")
        rebuild_started.set()
        assert release_rebuild.wait(timeout=3.0)
        return _payload(snapshot_id="fresh")

    clock = _Clock()
    condition = _NotifyTrackingCondition()
    store = DashboardSnapshotStore(
        builder,
        monotonic=clock,
        wait_timeout_seconds=1.0,
    )
    store._condition = condition
    store.get()
    clock.value = 2.0
    original_diagnostic = getattr(store, failure_point)
    cancellation_injected = False

    def cancel_diagnostic(*args: Any, **kwargs: Any) -> Any:
        nonlocal cancellation_injected
        if (
            threading.current_thread().name == "diagnostic-owner"
            and not cancellation_injected
        ):
            cancellation_injected = True
            raise signal
        return original_diagnostic(*args, **kwargs)

    monkeypatch.setattr(store, failure_point, cancel_diagnostic)
    monkeypatch.setattr(main_module, "dashboard_snapshot_store", store)
    main_module._reset_dashboard_health_for_tests(
        "degraded",
        "dashboard_snapshot_stale",
    )
    responses: dict[str, Any] = {}
    diagnostics: dict[str, dict[str, Any]] = {}
    errors: dict[str, BaseException] = {}

    def route_target(name: str) -> None:
        try:
            responses[name] = main_module._factor_lab_dashboard_response(
                _direct_request()
            )
            diagnostics[name] = store.request_diagnostics()
        except BaseException as error:
            errors[name] = error
        finally:
            if name == "waiter":
                waiter_done.set()

    owner = threading.Thread(
        target=route_target,
        args=("owner",),
        name="diagnostic-owner",
    )
    waiter = threading.Thread(
        target=route_target,
        args=("waiter",),
        name="diagnostic-waiter",
    )
    owner.start()
    assert rebuild_started.wait(timeout=3.0)
    waiter.start()
    assert condition.wait_started.wait(timeout=3.0)
    release_rebuild.set()
    completed_before_manual_notify = waiter_done.wait(timeout=0.3)
    if not completed_before_manual_notify:
        with condition:
            condition.notify_all()
    owner.join(timeout=3.0)
    waiter.join(timeout=3.0)

    assert not owner.is_alive()
    assert not waiter.is_alive()
    assert completed_before_manual_notify is True
    assert condition.owner_notified.is_set()
    assert errors.get("owner") is signal
    assert "waiter" not in errors
    assert responses["waiter"].headers["x-dashboard-cache"] == "HIT"
    assert json.loads(responses["waiter"].body)["snapshot_id"] == "fresh"
    assert diagnostics["waiter"]["attempt_status"] == "success"
    assert main_module._dashboard_health_snapshot() == {
        "status": "ready",
        "error_code": None,
    }


def test_late_waiter_prefers_fresh_global_snapshot_over_timeout_stale(
    main_module,
    monkeypatch,
) -> None:
    rebuild_started = threading.Event()
    release_rebuild = threading.Event()
    calls = 0

    class _Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    class _LateResumeCondition(threading.Condition):
        def __init__(self) -> None:
            super().__init__()
            self.wait_started = threading.Event()
            self.resume_entered = threading.Event()
            self.resume_release = threading.Event()

        def wait(self, timeout: float | None = None) -> bool:
            is_waiter = threading.current_thread().name == "late-waiter"
            if is_waiter:
                self.wait_started.set()
            notified = super().wait(timeout)
            if is_waiter:
                self.release()
                try:
                    self.resume_entered.set()
                    assert self.resume_release.wait(timeout=3.0)
                finally:
                    self.acquire()
            return notified

    def builder() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _payload(snapshot_id="seed")
        rebuild_started.set()
        assert release_rebuild.wait(timeout=3.0)
        return _payload(snapshot_id="fresh")

    waiter_samples = iter([0.0, 0.0, 2.0])
    original_wait_monotonic = snapshot_module._sample_wait_monotonic

    def sample_wait_monotonic() -> float:
        if threading.current_thread().name == "late-waiter":
            return next(waiter_samples)
        return original_wait_monotonic()

    monkeypatch.setattr(
        snapshot_module,
        "_sample_wait_monotonic",
        sample_wait_monotonic,
    )
    clock = _Clock()
    condition = _LateResumeCondition()
    store = DashboardSnapshotStore(
        builder,
        monotonic=clock,
        wait_timeout_seconds=1.0,
    )
    store._condition = condition
    store.get()
    clock.value = 2.0
    monkeypatch.setattr(main_module, "dashboard_snapshot_store", store)
    main_module._reset_dashboard_health_for_tests(
        "degraded",
        "dashboard_snapshot_stale",
    )
    responses: dict[str, Any] = {}
    diagnostics: dict[str, dict[str, Any]] = {}
    errors: dict[str, BaseException] = {}

    def route_target(name: str) -> None:
        try:
            responses[name] = main_module._factor_lab_dashboard_response(
                _direct_request()
            )
            diagnostics[name] = store.request_diagnostics()
        except BaseException as error:
            errors[name] = error

    owner = threading.Thread(
        target=route_target,
        args=("owner",),
        name="late-owner",
    )
    waiter = threading.Thread(
        target=route_target,
        args=("waiter",),
        name="late-waiter",
    )
    owner.start()
    assert rebuild_started.wait(timeout=3.0)
    waiter.start()
    assert condition.wait_started.wait(timeout=3.0)
    release_rebuild.set()
    assert condition.resume_entered.wait(timeout=3.0)
    owner.join(timeout=3.0)
    condition.resume_release.set()
    waiter.join(timeout=3.0)

    assert not owner.is_alive()
    assert not waiter.is_alive()
    assert errors == {}
    assert responses["waiter"].headers["x-dashboard-cache"] == "HIT"
    assert json.loads(responses["waiter"].body)["snapshot_id"] == "fresh"
    assert diagnostics["waiter"]["request_waited"] is True
    assert diagnostics["waiter"]["attempt_id"] == 2
    assert diagnostics["waiter"]["attempt_status"] == "success"
    assert main_module._dashboard_health_snapshot() == {
        "status": "ready",
        "error_code": None,
    }
