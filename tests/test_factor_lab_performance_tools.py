"""Factor Lab 公网性能探针的统计、协议和本地传输测试。"""

from __future__ import annotations

import base64
import gzip
import json
import mimetypes
import os
import random
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts import benchmark_factor_lab_browser as browser_probe
from scripts import benchmark_factor_lab_dashboard as api_probe
from scripts.benchmark_factor_lab_browser import (
    BrowserAttempt,
    CDPClient,
    CDPCommandError,
    browser_exit_code,
    classify_factor_lab_request,
    parse_arguments as parse_browser_arguments,
    select_ready_context,
    summarize_browser_attempts,
    terminate_owned_process,
    validate_ready_value,
)
from scripts.benchmark_factor_lab_dashboard import (
    Attempt,
    nearest_rank,
    run_benchmark,
    summarize_attempts,
    validate_dashboard_schema,
)
from tests.factor_lab_dashboard_conformance import dashboard_v1_conformance_samples


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)


def _dashboard_payload(*, stale: bool = False) -> dict:
    return {
        "schema_version": "factor-lab-dashboard-v1",
        "snapshot_id": "snapshot-test",
        "generated_at": "2026-07-22T12:00:00+08:00",
        "display_until": "2026-07-22",
        "stale": stale,
        "snapshot_age_ms": 0,
        "row_fields": [
            "predict_date",
            "feature_date",
            "target_date",
            "prediction_phase",
            "predicted_direction",
            "actual_direction",
        ],
        "target_labels": {},
        "schemes": [],
    }


def _api_attempt(index: int, *, total_ms: float, **overrides: object) -> Attempt:
    values: dict[str, object] = {
        "index": index,
        "started_at": "2026-07-22T12:00:00Z",
        "wait_before_seconds": 0.0,
        "status": 200,
        "dns_ms": None,
        "tcp_ms": None,
        "tls_ms": None,
        "ttfb_ms": 1.0,
        "total_ms": total_ms,
        "wire_bytes": 10,
        "raw_bytes": 20,
        "content_encoding": "gzip",
        "cache_status": "HIT",
        "stale_header": None,
        "server_timing": "request_route;dur=1",
        "schema_valid": True,
        "stale": False,
        "connection_reused": index > 1,
        "reconnected": False,
        "success": True,
        "error": None,
        "content_type": "application/json",
        "content_length": 10,
        "cache_control": "no-store",
        "vary": "Accept-Encoding",
        "snapshot_id_header": "snapshot-test",
        "snapshot_age_ms_header": 0,
        "snapshot_id_body": "snapshot-test",
        "snapshot_age_ms_body": 0,
    }
    values.update(overrides)
    return Attempt(**values)


def _browser_attempt(index: int, *, total_ms: float, **overrides: object) -> BrowserAttempt:
    values: dict[str, object] = {
        "index": index,
        "started_at": "2026-07-22T12:00:00Z",
        "wait_before_seconds": 0.0,
        "total_ms": total_ms,
        "success": True,
        "error": None,
        "dashboard_request_urls": [
            "https://bond.finailab.cn/bond-factor-lab/api/factor-lab/dashboard"
        ],
        "dashboard_response_evidence": [
            {
                "url": "https://bond.finailab.cn/bond-factor-lab/api/factor-lab/dashboard",
                "response_received": True,
                "redirected": False,
                "status": 200,
                "from_disk_cache": False,
                "from_service_worker": False,
                "from_prefetch_cache": False,
            }
        ],
        "legacy_request_urls": [],
        "console_errors": [],
        "page_errors": [],
        "ready": {
            "seq": index,
            "snapshotId": f"snapshot-{index}",
            "committedAt": 1.0,
            "stale": False,
            "source": "dashboard",
            "schemeCount": 35,
            "liveRowCount": 1000,
            "backtestRowCount": 8000,
        },
    }
    values.update(overrides)
    return BrowserAttempt(**values)


def test_nearest_rank_uses_ceil_without_interpolation() -> None:
    samples = [float(value) for value in range(1, 201)]
    assert nearest_rank(samples, 0.95) == 190.0
    assert nearest_rank([10.0, 1.0, 5.0], 0.50) == 5.0


@pytest.mark.parametrize("values, percentile", [([], 0.95), ([1.0], 0), ([1.0], 1.01)])
def test_nearest_rank_rejects_empty_or_invalid_percentiles(
    values: list[float], percentile: float
) -> None:
    with pytest.raises(ValueError):
        nearest_rank(values, percentile)


def test_api_summary_retains_failures_and_199_is_not_formal_acceptance() -> None:
    attempts = [_api_attempt(index, total_ms=float(index)) for index in range(1, 200)]
    attempts[-1] = _api_attempt(
        199,
        total_ms=5000.0,
        status=None,
        schema_valid=False,
        stale=None,
        success=False,
        error="timeout",
    )

    summary = summarize_attempts(attempts)

    assert summary["attempt_count"] == 199
    assert summary["formal_acceptance"] is False
    assert summary["acceptance"] is False
    assert summary["latency_ms"]["sample_count"] == 199
    assert summary["failures"] == [{"index": 199, "error": "timeout"}]


def test_api_summary_automatically_enforces_formal_200_sample_gate() -> None:
    attempts = [_api_attempt(index, total_ms=float(index)) for index in range(1, 201)]
    summary = summarize_attempts(attempts)
    assert summary["formal_acceptance"] is True
    assert summary["latency_ms"]["p95"] == 190.0
    assert summary["acceptance"] is True


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ({"content_type": None}, "content_type"),
        ({"content_length": 11}, "content_length"),
        ({"wire_bytes": 100_001, "content_length": 100_001}, "wire_budget"),
        ({"raw_bytes": 1_500_001}, "raw_budget"),
        ({"cache_status": "STALE"}, "cache_status"),
        ({"stale": True}, "stale"),
        ({"snapshot_id_header": "other"}, "snapshot_id"),
    ],
)
def test_api_summary_revalidates_recorded_contract_instead_of_trusting_success(
    mutation: dict[str, object], expected_error: str
) -> None:
    attempts = [_api_attempt(index, total_ms=float(index)) for index in range(1, 201)]
    attempts[-1] = _api_attempt(200, total_ms=200.0, **mutation)

    summary = summarize_attempts(attempts)

    assert summary["acceptance"] is False
    assert summary["failure_count"] == 1
    assert expected_error in summary["failures"][0]["error"]


@pytest.mark.parametrize(
    "sample", dashboard_v1_conformance_samples(), ids=lambda sample: sample["name"]
)
def test_api_schema_probe_matches_shared_dashboard_v1_conformance(sample: dict) -> None:
    if sample["valid"]:
        validate_dashboard_schema(sample["payload"])
    else:
        with pytest.raises(ValueError):
            validate_dashboard_schema(sample["payload"])


class _DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    connection_ids: list[int] = []
    stale = False
    gzip_body = True
    delay_seconds = 0.0
    close_after_response = False
    accept_encodings: list[str] = []
    content_type: str | None = "application/json"
    content_length_delta = 0
    cache_control: str | None = "no-store"
    vary: str | None = "Accept-Encoding"
    cache_status: str | None = "HIT"
    snapshot_id_header: str | None = "snapshot-test"
    snapshot_age_header: str | None = "0"
    payload_snapshot_id = "snapshot-test"
    payload_snapshot_age = 0
    target_label_padding = ""

    def log_message(self, _format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        type(self).connection_ids.append(self.client_address[1])
        type(self).accept_encodings.append(self.headers.get("Accept-Encoding", ""))
        if type(self).delay_seconds:
            time.sleep(type(self).delay_seconds)
        payload = _dashboard_payload(stale=type(self).stale)
        payload["snapshot_id"] = type(self).payload_snapshot_id
        payload["snapshot_age_ms"] = type(self).payload_snapshot_age
        if type(self).target_label_padding:
            payload["target_labels"] = {
                "padding": type(self).target_label_padding,
            }
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        body = gzip.compress(raw) if type(self).gzip_body else raw
        self.send_response(200)
        if type(self).content_type is not None:
            self.send_header("Content-Type", type(self).content_type)
        self.send_header(
            "Content-Length",
            str(len(body) + type(self).content_length_delta),
        )
        if type(self).cache_control is not None:
            self.send_header("Cache-Control", type(self).cache_control)
        if type(self).vary is not None:
            self.send_header("Vary", type(self).vary)
        if type(self).cache_status is not None:
            self.send_header("X-Dashboard-Cache", type(self).cache_status)
        if type(self).snapshot_id_header is not None:
            self.send_header(
                "X-Dashboard-Snapshot-ID",
                type(self).snapshot_id_header,
            )
        if type(self).snapshot_age_header is not None:
            self.send_header(
                "X-Dashboard-Snapshot-Age",
                type(self).snapshot_age_header,
            )
        if type(self).stale:
            self.send_header("X-Dashboard-Warning", "stale-last-known-good")
        if type(self).gzip_body:
            self.send_header("Content-Encoding", "gzip")
        if type(self).close_after_response or type(self).content_length_delta:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def dashboard_server() -> tuple[ThreadingHTTPServer, str]:
    _DashboardHandler.connection_ids = []
    _DashboardHandler.stale = False
    _DashboardHandler.gzip_body = True
    _DashboardHandler.delay_seconds = 0.0
    _DashboardHandler.close_after_response = False
    _DashboardHandler.accept_encodings = []
    _DashboardHandler.content_type = "application/json"
    _DashboardHandler.content_length_delta = 0
    _DashboardHandler.cache_control = "no-store"
    _DashboardHandler.vary = "Accept-Encoding"
    _DashboardHandler.cache_status = "HIT"
    _DashboardHandler.snapshot_id_header = "snapshot-test"
    _DashboardHandler.snapshot_age_header = "0"
    _DashboardHandler.payload_snapshot_id = "snapshot-test"
    _DashboardHandler.payload_snapshot_age = 0
    _DashboardHandler.target_label_padding = ""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield server, f"http://{host}:{port}/api/factor-lab/dashboard"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_api_probe_reuses_keepalive_and_records_gzip_schema(
    dashboard_server: tuple[ThreadingHTTPServer, str],
) -> None:
    _, url = dashboard_server
    attempts, metadata = run_benchmark(
        url=url,
        attempt_count=2,
        timeout_seconds=2.0,
        disable_keepalive=False,
        minimum_attempt_period_seconds=0.0,
    )

    assert len(set(_DashboardHandler.connection_ids)) == 1
    assert attempts[0].dns_ms is not None
    assert attempts[0].tcp_ms is not None
    assert attempts[1].dns_ms is None
    assert attempts[1].tcp_ms is None
    assert attempts[1].connection_reused is True
    assert all(attempt.success and attempt.schema_valid for attempt in attempts)
    assert attempts[0].wire_bytes < attempts[0].raw_bytes
    assert attempts[0].content_type == "application/json"
    assert attempts[0].content_length == attempts[0].wire_bytes
    assert attempts[0].cache_control == "no-store"
    assert attempts[0].vary == "Accept-Encoding"
    assert attempts[0].snapshot_id_header == "snapshot-test"
    assert attempts[0].snapshot_age_ms_header == 0
    assert metadata["connection_reuse"] == "enabled"
    assert _DashboardHandler.accept_encodings == ["gzip", "gzip"]


def test_api_probe_disable_keepalive_uses_new_connection_each_attempt(
    dashboard_server: tuple[ThreadingHTTPServer, str],
) -> None:
    _, url = dashboard_server
    attempts, metadata = run_benchmark(
        url=url,
        attempt_count=2,
        timeout_seconds=2.0,
        disable_keepalive=True,
        minimum_attempt_period_seconds=0.0,
    )

    assert len(set(_DashboardHandler.connection_ids)) == 2
    assert all(attempt.dns_ms is not None for attempt in attempts)
    assert all(attempt.connection_reused is False for attempt in attempts)
    assert metadata["connection_reuse"] == "disabled"


def test_api_probe_records_server_closed_keepalive_as_a_new_connection(
    dashboard_server: tuple[ThreadingHTTPServer, str],
) -> None:
    _, url = dashboard_server
    _DashboardHandler.close_after_response = True
    attempts, _ = run_benchmark(
        url=url,
        attempt_count=2,
        timeout_seconds=2.0,
        disable_keepalive=False,
        minimum_attempt_period_seconds=0.0,
    )
    assert len(set(_DashboardHandler.connection_ids)) == 2
    assert attempts[1].connection_reused is False
    assert attempts[1].dns_ms is not None
    assert attempts[1].tcp_ms is not None


def test_api_probe_marks_stale_and_timeout_as_failures(
    dashboard_server: tuple[ThreadingHTTPServer, str],
) -> None:
    _, url = dashboard_server
    _DashboardHandler.stale = True
    stale_attempts, _ = run_benchmark(
        url=url,
        attempt_count=1,
        timeout_seconds=2.0,
        disable_keepalive=True,
        minimum_attempt_period_seconds=0.0,
    )
    assert stale_attempts[0].success is False
    assert stale_attempts[0].stale is True

    _DashboardHandler.stale = False
    _DashboardHandler.delay_seconds = 0.15
    timeout_attempts, _ = run_benchmark(
        url=url,
        attempt_count=1,
        timeout_seconds=0.03,
        disable_keepalive=True,
        minimum_attempt_period_seconds=0.0,
    )
    assert timeout_attempts[0].success is False
    assert timeout_attempts[0].error is not None
    assert timeout_attempts[0].total_ms >= 20


def test_api_probe_requires_single_gzip_representation_when_requested(
    dashboard_server: tuple[ThreadingHTTPServer, str],
) -> None:
    _, url = dashboard_server
    _DashboardHandler.gzip_body = False
    attempts, _ = run_benchmark(
        url=url,
        attempt_count=1,
        timeout_seconds=2.0,
        disable_keepalive=True,
        minimum_attempt_period_seconds=0.0,
    )
    assert attempts[0].success is False
    assert attempts[0].schema_valid is True
    assert "gzip" in (attempts[0].error or "")


@pytest.mark.parametrize(
    ("attribute", "mutated_value", "error_fragment"),
    [
        ("content_type", None, "content-type"),
        ("content_type", "text/plain", "content-type"),
        ("cache_control", None, "cache-control"),
        ("cache_control", "max-age=60", "cache-control"),
        ("vary", None, "vary"),
        ("vary", "Origin", "vary"),
    ],
)
def test_api_probe_fails_closed_on_response_contract_header_mutations(
    dashboard_server: tuple[ThreadingHTTPServer, str],
    attribute: str,
    mutated_value: str | None,
    error_fragment: str,
) -> None:
    _, url = dashboard_server
    setattr(_DashboardHandler, attribute, mutated_value)

    attempts, _ = run_benchmark(
        url=url,
        attempt_count=1,
        timeout_seconds=2.0,
        disable_keepalive=True,
        minimum_attempt_period_seconds=0.0,
    )

    assert attempts[0].success is False
    assert error_fragment in (attempts[0].error or "").casefold()


def test_api_probe_accepts_vary_when_accept_encoding_is_one_of_multiple_tokens(
    dashboard_server: tuple[ThreadingHTTPServer, str],
) -> None:
    _, url = dashboard_server
    _DashboardHandler.vary = "Origin, ACCEPT-ENCODING"

    attempts, _ = run_benchmark(
        url=url,
        attempt_count=1,
        timeout_seconds=2.0,
        disable_keepalive=True,
        minimum_attempt_period_seconds=0.0,
    )

    assert attempts[0].success is True


def test_api_probe_rejects_content_length_that_differs_from_wire_bytes(
    dashboard_server: tuple[ThreadingHTTPServer, str],
) -> None:
    _, url = dashboard_server
    _DashboardHandler.content_length_delta = 1

    attempts, _ = run_benchmark(
        url=url,
        attempt_count=1,
        timeout_seconds=2.0,
        disable_keepalive=True,
        minimum_attempt_period_seconds=0.0,
    )

    attempt = attempts[0]
    assert attempt.success is False
    assert attempt.content_length == attempt.wire_bytes + 1
    assert "content-length" in (attempt.error or "").casefold()


@pytest.mark.parametrize("cache_status", [None, "STALE", "UNAVAILABLE", "BOGUS"])
def test_api_probe_rejects_missing_degraded_or_invalid_cache_status(
    dashboard_server: tuple[ThreadingHTTPServer, str],
    cache_status: str | None,
) -> None:
    _, url = dashboard_server
    _DashboardHandler.cache_status = cache_status

    attempts, _ = run_benchmark(
        url=url,
        attempt_count=1,
        timeout_seconds=2.0,
        disable_keepalive=True,
        minimum_attempt_period_seconds=0.0,
    )

    assert attempts[0].success is False
    assert "x-dashboard-cache" in (attempts[0].error or "").casefold()


def test_api_probe_accepts_fresh_cache_miss(
    dashboard_server: tuple[ThreadingHTTPServer, str],
) -> None:
    _, url = dashboard_server
    _DashboardHandler.cache_status = "MISS"

    attempts, _ = run_benchmark(
        url=url,
        attempt_count=1,
        timeout_seconds=2.0,
        disable_keepalive=True,
        minimum_attempt_period_seconds=0.0,
    )

    assert attempts[0].success is True
    assert attempts[0].cache_status == "MISS"


@pytest.mark.parametrize(
    ("snapshot_id_header", "snapshot_age_header", "error_fragment"),
    [
        (None, "0", "x-dashboard-snapshot-id"),
        ("different-snapshot", "0", "x-dashboard-snapshot-id"),
        ("snapshot-test", None, "x-dashboard-snapshot-age"),
        ("snapshot-test", "not-an-integer", "x-dashboard-snapshot-age"),
        ("snapshot-test", "1", "x-dashboard-snapshot-age"),
    ],
)
def test_api_probe_rejects_snapshot_headers_that_do_not_match_body(
    dashboard_server: tuple[ThreadingHTTPServer, str],
    snapshot_id_header: str | None,
    snapshot_age_header: str | None,
    error_fragment: str,
) -> None:
    _, url = dashboard_server
    _DashboardHandler.snapshot_id_header = snapshot_id_header
    _DashboardHandler.snapshot_age_header = snapshot_age_header

    attempts, _ = run_benchmark(
        url=url,
        attempt_count=1,
        timeout_seconds=2.0,
        disable_keepalive=True,
        minimum_attempt_period_seconds=0.0,
    )

    assert attempts[0].success is False
    assert error_fragment in (attempts[0].error or "").casefold()


def test_api_probe_rejects_fresh_snapshot_older_than_one_second(
    dashboard_server: tuple[ThreadingHTTPServer, str],
) -> None:
    _, url = dashboard_server
    _DashboardHandler.payload_snapshot_age = 1001
    _DashboardHandler.snapshot_age_header = "1001"

    attempts, _ = run_benchmark(
        url=url,
        attempt_count=1,
        timeout_seconds=2.0,
        disable_keepalive=True,
        minimum_attempt_period_seconds=0.0,
    )

    assert attempts[0].success is False
    assert "snapshot age" in (attempts[0].error or "").casefold()


def test_api_probe_rejects_raw_json_over_budget(
    dashboard_server: tuple[ThreadingHTTPServer, str],
) -> None:
    _, url = dashboard_server
    _DashboardHandler.target_label_padding = "x" * 1_500_000

    attempts, _ = run_benchmark(
        url=url,
        attempt_count=1,
        timeout_seconds=5.0,
        disable_keepalive=True,
        minimum_attempt_period_seconds=0.0,
    )

    assert attempts[0].raw_bytes > 1_500_000
    assert attempts[0].success is False
    assert "raw bytes" in (attempts[0].error or "").casefold()


def test_api_probe_rejects_gzip_wire_representation_over_budget(
    dashboard_server: tuple[ThreadingHTTPServer, str],
) -> None:
    _, url = dashboard_server
    random_bytes = random.Random(0).randbytes(150_000)
    _DashboardHandler.target_label_padding = base64.b64encode(random_bytes).decode()

    attempts, _ = run_benchmark(
        url=url,
        attempt_count=1,
        timeout_seconds=5.0,
        disable_keepalive=True,
        minimum_attempt_period_seconds=0.0,
    )

    assert attempts[0].wire_bytes > 100_000
    assert attempts[0].raw_bytes <= 1_500_000
    assert attempts[0].success is False
    assert "wire bytes" in (attempts[0].error or "").casefold()


def test_api_probe_throttles_from_attempt_start_but_excludes_wait_from_latency(
    dashboard_server: tuple[ThreadingHTTPServer, str],
) -> None:
    _, url = dashboard_server
    started = time.monotonic()
    attempts, _ = run_benchmark(
        url=url,
        attempt_count=2,
        timeout_seconds=2.0,
        disable_keepalive=False,
        minimum_attempt_period_seconds=0.08,
    )
    elapsed = time.monotonic() - started
    assert elapsed >= 0.075
    assert attempts[1].wait_before_seconds > 0
    assert attempts[1].total_ms < elapsed * 1000


def test_api_probe_propagates_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(api_probe.socket, "getaddrinfo", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_benchmark(
            url="http://127.0.0.1:18101/api/factor-lab/dashboard",
            attempt_count=1,
            timeout_seconds=1,
            disable_keepalive=True,
            minimum_attempt_period_seconds=0,
        )


def test_request_classifier_is_path_strict_and_redacts_query() -> None:
    assert classify_factor_lab_request(
        "https://host/bond-factor-lab/api/factor-lab/dashboard?token=secret"
    ) == (
        "dashboard",
        "https://host/bond-factor-lab/api/factor-lab/dashboard",
    )
    assert classify_factor_lab_request("https://host/api/schemes") == (
        "legacy",
        "https://host/api/schemes",
    )
    assert classify_factor_lab_request("https://host/api/metrics/a%2Fb") == (None, None)
    assert classify_factor_lab_request("https://host/x/api/schemes.css") == (None, None)


def test_ready_validation_requires_dashboard_fresh_counts_and_types() -> None:
    ready = _browser_attempt(1, total_ms=1.0).ready
    assert validate_ready_value(ready) is None
    assert validate_ready_value({**ready, "stale": True}) == "ready_stale"
    assert validate_ready_value({**ready, "source": "legacy"}) == "ready_source"
    assert validate_ready_value({**ready, "schemeCount": True}) == "ready_schema"


def test_ready_context_selection_cannot_accept_parent_in_iframe_mode() -> None:
    contexts = {1: "main", 2: "child"}
    frames = {
        "main": "https://shell.example/bond-factor-lab/",
        "child": "https://bond.finailab.cn/bond-factor-lab/",
    }
    assert select_ready_context(contexts, frames, "main", None) == 1
    assert select_ready_context(contexts, frames, "main", "bond-factor-lab") == 2
    assert (
        select_ready_context(
            {1: "main"},
            {"main": frames["main"]},
            "main",
            "bond-factor-lab",
        )
        is None
    )
    with pytest.raises(ValueError, match="ready_frame_match_count=2"):
        select_ready_context(
            {1: "main", 2: "child", 3: "second-child"},
            {
                **frames,
                "second-child": "https://backup.example/bond-factor-lab/",
            },
            "main",
            "bond-factor-lab",
        )


class _FakeWebSocket:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = deque(json.dumps(message) for message in messages)
        self.sent: list[dict] = []

    def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def recv(self, timeout: float | None = None) -> str:
        del timeout
        return self.messages.popleft()

    def close(self) -> None:
        return


def test_cdp_client_demultiplexes_events_and_out_of_order_responses() -> None:
    socket = _FakeWebSocket(
        [
            {"method": "Network.requestWillBeSent", "params": {"requestId": "r1"}},
            {"id": 99, "result": {"other": True}},
            {"id": 1, "result": {"value": 42}},
        ]
    )
    client = CDPClient(socket)

    assert client.call("Runtime.evaluate", {"expression": "1+1"}, timeout=1) == {
        "value": 42
    }
    assert client.next_event(timeout=0)["method"] == "Network.requestWillBeSent"
    assert client.pending_response(99) == {"other": True}


def test_cdp_client_exposes_transient_destroyed_context_error_code() -> None:
    socket = _FakeWebSocket(
        [{"id": 1, "error": {"code": -32000, "message": "context destroyed"}}]
    )
    client = CDPClient(socket)
    with pytest.raises(CDPCommandError) as caught:
        client.call("Runtime.evaluate", {"expression": "1"}, timeout=1)
    assert caught.value.code == -32000
    assert "Runtime.evaluate" in str(caught.value)

    known = CDPCommandError(
        "Runtime.evaluate", -32000, "Cannot find context with specified id"
    )
    unrelated = CDPCommandError("Runtime.evaluate", -32000, "Permission denied")
    other_method = CDPCommandError(
        "Page.navigate", -32000, "Cannot find context with specified id"
    )
    assert known.is_navigation_context_loss() is True
    assert unrelated.is_navigation_context_loss() is False
    assert other_method.is_navigation_context_loss() is False


def test_cdp_client_routes_flattened_session_commands() -> None:
    socket = _FakeWebSocket([{"id": 1, "sessionId": "child", "result": {"ok": True}}])
    client = CDPClient(socket)
    assert client.call(
        "Runtime.evaluate",
        {"expression": "1"},
        session_id="child",
        timeout=1,
    ) == {"ok": True}
    assert socket.sent == [
        {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": "1"},
            "sessionId": "child",
        }
    ]


def test_request_events_preserve_redirects_that_reuse_request_id() -> None:
    dashboard_requests: list[str] = []
    legacy_requests: list[str] = []
    common = {
        "contexts": {},
        "frame_urls": {},
        "frame_sessions": {},
        "dashboard_requests": dashboard_requests,
        "legacy_requests": legacy_requests,
        "dashboard_response_evidence": [],
        "dashboard_request_indices": {},
        "console_errors": [],
        "page_errors": [],
    }
    first = {
        "method": "Network.requestWillBeSent",
        "sessionId": "worker-session",
        "params": {
            "requestId": "reused",
            "request": {
                "url": "https://bond.finailab.cn/api/factor-lab/dashboard?token=first"
            },
        },
    }
    redirected = {
        "method": "Network.requestWillBeSent",
        "sessionId": "worker-session",
        "params": {
            "requestId": "reused",
            "redirectResponse": {"status": 302},
            "request": {
                "url": "https://bond.finailab.cn/api/factor-lab/dashboard?token=second"
            },
        },
    }

    assert browser_probe._record_event(first, **common) is False
    assert browser_probe._record_event(redirected, **common) is False
    assert dashboard_requests == [
        "https://bond.finailab.cn/api/factor-lab/dashboard",
        "https://bond.finailab.cn/api/factor-lab/dashboard",
    ]
    assert legacy_requests == []


def test_dashboard_redirect_to_non_dashboard_fails_closed() -> None:
    dashboard_requests: list[str] = []
    dashboard_responses: list[dict[str, object]] = []
    common = {
        "contexts": {},
        "frame_urls": {},
        "frame_sessions": {},
        "dashboard_requests": dashboard_requests,
        "legacy_requests": [],
        "dashboard_response_evidence": dashboard_responses,
        "dashboard_request_indices": {},
        "console_errors": [],
        "page_errors": [],
    }
    dashboard = {
        "method": "Network.requestWillBeSent",
        "sessionId": "worker-session",
        "params": {
            "requestId": "reused",
            "request": {
                "url": "https://bond.finailab.cn/api/factor-lab/dashboard?token=first"
            },
        },
    }
    redirected_away = {
        "method": "Network.requestWillBeSent",
        "sessionId": "worker-session",
        "params": {
            "requestId": "reused",
            "redirectResponse": {"status": 302},
            "request": {"url": "https://login.finailab.cn/session"},
        },
    }

    assert browser_probe._record_event(dashboard, **common) is False
    assert browser_probe._record_event(redirected_away, **common) is False
    assert dashboard_requests == [
        "https://bond.finailab.cn/api/factor-lab/dashboard"
    ]
    assert dashboard_responses == [
        {
            "url": "https://bond.finailab.cn/api/factor-lab/dashboard",
            "response_received": True,
            "redirected": True,
            "status": 302,
            "from_disk_cache": False,
            "from_service_worker": False,
            "from_prefetch_cache": False,
        }
    ]
    assert (
        browser_probe.validate_dashboard_response_evidence(
            dashboard_requests,
            dashboard_responses,
        )
        == "dashboard_redirect_detected"
    )


def test_dashboard_response_cache_evidence_fails_closed() -> None:
    dashboard_responses: list[dict[str, object]] = []
    common = {
        "contexts": {},
        "frame_urls": {},
        "frame_sessions": {},
        "dashboard_requests": [],
        "legacy_requests": [],
        "dashboard_response_evidence": dashboard_responses,
        "dashboard_request_indices": {},
        "console_errors": [],
        "page_errors": [],
    }
    dashboard_request = {
        "method": "Network.requestWillBeSent",
        "sessionId": "worker-session",
        "params": {
            "requestId": "worker-dashboard",
            "request": {
                "url": "https://bond.finailab.cn/api/factor-lab/dashboard?secret=1"
            },
        },
    }
    served_from_cache = {
        "method": "Network.requestServedFromCache",
        "sessionId": "worker-session",
        "params": {"requestId": "worker-dashboard"},
    }
    cached_response = {
        "method": "Network.responseReceived",
        "sessionId": "worker-session",
        "params": {
            "requestId": "worker-dashboard",
            "response": {
                "url": "https://bond.finailab.cn/api/factor-lab/dashboard?secret=1",
                "status": 200,
                "fromDiskCache": True,
                "fromServiceWorker": False,
                "fromPrefetchCache": True,
            },
        },
    }
    assert browser_probe._record_event(dashboard_request, **common) is False
    assert browser_probe._record_event(served_from_cache, **common) is False
    assert browser_probe._record_event(cached_response, **common) is False
    assert dashboard_responses == [
        {
            "url": "https://bond.finailab.cn/api/factor-lab/dashboard",
            "response_received": True,
            "redirected": False,
            "status": 200,
            "from_disk_cache": True,
            "from_service_worker": False,
            "from_prefetch_cache": True,
        }
    ]
    assert (
        browser_probe.validate_dashboard_response_evidence(
            ["https://bond.finailab.cn/api/factor-lab/dashboard"],
            dashboard_responses,
        )
        == "dashboard_cache_or_service_worker"
    )
    assert (
        browser_probe.validate_dashboard_response_evidence(
            ["https://bond.finailab.cn/api/factor-lab/dashboard"],
            [],
        )
        == "dashboard_response_count_invalid"
    )
    assert (
        browser_probe.validate_dashboard_response_evidence(
            ["https://bond.finailab.cn/api/factor-lab/dashboard"],
            [
                {
                    "url": "https://bond.finailab.cn/api/factor-lab/dashboard",
                    "response_received": False,
                    "redirected": False,
                    "status": None,
                    "from_disk_cache": False,
                    "from_service_worker": False,
                    "from_prefetch_cache": False,
                }
            ],
        )
        == "dashboard_response_count_invalid"
    )
    assert browser_probe.validate_dashboard_response_evidence(
        ["https://bond.finailab.cn/api/factor-lab/dashboard"],
        [
            {
                "url": "https://bond.finailab.cn/api/factor-lab/dashboard",
                "response_received": True,
                "redirected": False,
                "status": 200,
                "from_disk_cache": False,
                "from_service_worker": False,
                "from_prefetch_cache": False,
            }
        ]
    ) is None


@pytest.mark.parametrize(
    "cache_field",
    ["from_disk_cache", "from_service_worker", "from_prefetch_cache"],
)
@pytest.mark.parametrize("mutation", ["missing", "none", "string", "zero"])
def test_dashboard_cache_evidence_requires_literal_booleans(
    cache_field: str,
    mutation: str,
) -> None:
    evidence: dict[str, object] = {
        "url": "https://bond.finailab.cn/api/factor-lab/dashboard",
        "response_received": True,
        "redirected": False,
        "status": 200,
        "from_disk_cache": False,
        "from_service_worker": False,
        "from_prefetch_cache": False,
    }
    if mutation == "missing":
        evidence.pop(cache_field)
    elif mutation == "none":
        evidence[cache_field] = None
    elif mutation == "string":
        evidence[cache_field] = "false"
    else:
        evidence[cache_field] = 0

    assert (
        browser_probe.validate_dashboard_response_evidence(
            ["https://bond.finailab.cn/api/factor-lab/dashboard"],
            [evidence],
        )
        == "dashboard_cache_evidence_invalid"
    )

    attempts = [_browser_attempt(index, total_ms=500) for index in range(1, 201)]
    attempts[0] = _browser_attempt(
        1,
        total_ms=500,
        dashboard_request_urls=[
            "https://bond.finailab.cn/api/factor-lab/dashboard"
        ],
        dashboard_response_evidence=[evidence],
    )
    summary = summarize_browser_attempts(
        attempts,
        browser_product="Chrome/140.0.0.0",
        browser_user_agent="Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36",
        allow_edge_acceptance=False,
        edge_approval_reference=None,
        metadata_complete=True,
    )
    assert summary["acceptance"] is False
    assert "dashboard_cache_evidence_invalid" in summary["gate_failures"]


@pytest.mark.parametrize(
    "mutation",
    ["missing", "bool", "float", "server_error"],
)
def test_dashboard_response_status_requires_integer_200(mutation: str) -> None:
    dashboard_url = "https://bond.finailab.cn/api/factor-lab/dashboard"
    evidence: dict[str, object] = {
        "url": dashboard_url,
        "response_received": True,
        "redirected": False,
        "status": 200,
        "from_disk_cache": False,
        "from_service_worker": False,
        "from_prefetch_cache": False,
    }
    if mutation == "missing":
        evidence.pop("status")
    elif mutation == "bool":
        evidence["status"] = True
    elif mutation == "float":
        evidence["status"] = 200.0
    else:
        evidence["status"] = 503

    assert (
        browser_probe.validate_dashboard_response_evidence(
            [dashboard_url],
            [evidence],
        )
        == "dashboard_response_status_invalid"
    )


@pytest.mark.parametrize(
    "mutation",
    ["not_dict", "extra", "query", "url_mismatch", "response_type", "redirect_type"],
)
def test_dashboard_response_evidence_structure_and_url_fail_closed(
    mutation: str,
) -> None:
    dashboard_url = "https://bond.finailab.cn/api/factor-lab/dashboard"
    evidence: object = {
        "url": dashboard_url,
        "response_received": True,
        "redirected": False,
        "status": 200,
        "from_disk_cache": False,
        "from_service_worker": False,
        "from_prefetch_cache": False,
    }
    if mutation == "not_dict":
        evidence = None
    else:
        assert isinstance(evidence, dict)
        if mutation == "extra":
            evidence["unexpected"] = False
        elif mutation == "query":
            evidence["url"] = f"{dashboard_url}?secret=1"
        elif mutation == "url_mismatch":
            evidence["url"] = "https://other.finailab.cn/api/factor-lab/dashboard"
        elif mutation == "response_type":
            evidence["response_received"] = 1
        else:
            evidence["redirected"] = 0

    assert (
        browser_probe.validate_dashboard_response_evidence(
            [dashboard_url],
            [evidence],
        )
        == "dashboard_response_evidence_invalid"
    )


def test_page_and_worker_sessions_use_target_specific_cdp_domains() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict, str | None]] = []

        def call(
            self,
            method: str,
            params: dict | None = None,
            *,
            timeout: float,
            session_id: str | None = None,
        ) -> dict:
            del timeout
            self.calls.append((method, params or {}, session_id))
            return {}

    page_client = FakeClient()
    assert browser_probe._enable_cdp_session(  # type: ignore[arg-type]
        page_client, "oopif-child", "iframe"
    ) is True
    assert page_client.calls[0] == (
        "Target.setAutoAttach",
        {
            "autoAttach": True,
            "waitForDebuggerOnStart": True,
            "flatten": True,
        },
        "oopif-child",
    )
    page_methods = [
        method
        for method, _params, session in page_client.calls
        if session == "oopif-child"
    ]
    for required in (
        "Runtime.enable",
        "Network.enable",
        "Page.enable",
        "Log.enable",
        "Inspector.enable",
        "Network.setCacheDisabled",
        "Network.setBypassServiceWorker",
        "Network.setUserAgentOverride",
        "Runtime.runIfWaitingForDebugger",
    ):
        assert required in page_methods

    for target_type in ("worker", "shared_worker", "service_worker"):
        worker_client = FakeClient()
        session_id = f"{target_type}-session"
        assert browser_probe._enable_cdp_session(  # type: ignore[arg-type]
            worker_client, session_id, target_type
        ) is True
        worker_methods = [
            method
            for method, _params, session in worker_client.calls
            if session == session_id
        ]
        assert "Page.enable" not in worker_methods
        assert "Inspector.enable" not in worker_methods
        assert "Network.clearBrowserCache" not in worker_methods
        for required in (
            "Target.setAutoAttach",
            "Runtime.enable",
            "Network.enable",
            "Log.enable",
            "Network.setCacheDisabled",
            "Network.setBypassServiceWorker",
            "Network.setUserAgentOverride",
            "Runtime.runIfWaitingForDebugger",
        ):
            assert required in worker_methods
        assert (
            "Network.setCacheDisabled",
            {"cacheDisabled": True},
            session_id,
        ) in worker_client.calls
        assert (
            "Network.setBypassServiceWorker",
            {"bypass": True},
            session_id,
        ) in worker_client.calls
        assert (
            "Network.setUserAgentOverride",
            {"userAgent": browser_probe.USER_AGENT},
            session_id,
        ) in worker_client.calls


def test_worker_target_url_does_not_count_as_an_iframe() -> None:
    class WorkerAttachClient:
        def __init__(self) -> None:
            self.events = deque(
                [
                    {
                        "method": "Target.attachedToTarget",
                        "params": {
                            "sessionId": "worker-session",
                            "targetInfo": {
                                "targetId": "worker-target",
                                "type": "worker",
                                "url": "https://host/bond-factor-lab/worker.js",
                            },
                        },
                    }
                ]
            )

        def next_event(self, *, timeout: float) -> dict:
            del timeout
            if not self.events:
                raise TimeoutError
            return self.events.popleft()

        def call(
            self,
            method: str,
            params: dict | None = None,
            *,
            timeout: float,
            session_id: str | None = None,
        ) -> dict:
            del method, params, timeout, session_id
            return {}

    frame_urls: dict[str, str] = {}
    assert browser_probe._consume_cdp_events(  # type: ignore[arg-type]
        WorkerAttachClient(),
        configured_sessions=set(),
        session_targets={},
        session_target_types={},
        contexts={},
        frame_urls=frame_urls,
        frame_sessions={},
        dashboard_requests=[],
        legacy_requests=[],
        dashboard_response_evidence=[],
        dashboard_request_indices={},
        console_errors=[],
        page_errors=[],
    ) is False
    assert frame_urls == {}


def test_session_configuration_failure_still_resumes_before_raising() -> None:
    class FailingPageClient:
        def __init__(self) -> None:
            self.methods: list[str] = []

        def call(
            self,
            method: str,
            params: dict | None = None,
            *,
            timeout: float,
            session_id: str | None = None,
        ) -> dict:
            del params, timeout, session_id
            self.methods.append(method)
            if method == "Page.enable":
                raise CDPCommandError(method, -32601, "method not found")
            return {}

    client = FailingPageClient()
    with pytest.raises(CDPCommandError) as caught:
        browser_probe._enable_cdp_session(  # type: ignore[arg-type]
            client, "bad-page", "page"
        )
    assert caught.value.method == "Page.enable"
    assert client.methods[-1] == "Runtime.runIfWaitingForDebugger"


def test_unknown_attached_target_is_resumed_detached_and_not_recorded() -> None:
    class EventClient:
        def __init__(self) -> None:
            self.events = deque(
                [
                    {
                        "method": "Target.attachedToTarget",
                        "params": {
                            "sessionId": "unknown-session",
                            "targetInfo": {
                                "targetId": "unknown-target",
                                "type": "auction_worklet",
                                "url": "https://example.test/worklet",
                            },
                        },
                    }
                ]
            )
            self.calls: list[tuple[str, dict, str | None]] = []

        def next_event(self, *, timeout: float) -> dict:
            del timeout
            if not self.events:
                raise TimeoutError
            return self.events.popleft()

        def call(
            self,
            method: str,
            params: dict | None = None,
            *,
            timeout: float,
            session_id: str | None = None,
        ) -> dict:
            del timeout
            self.calls.append((method, params or {}, session_id))
            return {}

    client = EventClient()
    configured_sessions: set[str] = set()
    session_targets: dict[str, str] = {}
    session_target_types: dict[str, str] = {}
    assert browser_probe._consume_cdp_events(  # type: ignore[arg-type]
        client,
        configured_sessions=configured_sessions,
        session_targets=session_targets,
        session_target_types=session_target_types,
        contexts={},
        frame_urls={},
        frame_sessions={},
        dashboard_requests=[],
        legacy_requests=[],
        dashboard_response_evidence=[],
        dashboard_request_indices={},
        console_errors=[],
        page_errors=[],
    ) is False
    assert client.calls == [
        ("Runtime.runIfWaitingForDebugger", {}, "unknown-session"),
        ("Target.detachFromTarget", {"sessionId": "unknown-session"}, None),
    ]
    assert configured_sessions == set()
    assert session_targets == {}
    assert session_target_types == {}


def test_unknown_target_fails_closed_when_resume_and_detach_both_fail() -> None:
    class StuckUnknownClient:
        def __init__(self) -> None:
            self.events = deque(
                [
                    {
                        "method": "Target.attachedToTarget",
                        "params": {
                            "sessionId": "stuck-session",
                            "targetInfo": {
                                "targetId": "stuck-target",
                                "type": "auction_worklet",
                            },
                        },
                    }
                ]
            )

        def next_event(self, *, timeout: float) -> dict:
            del timeout
            if not self.events:
                raise TimeoutError
            return self.events.popleft()

        def call(
            self,
            method: str,
            params: dict | None = None,
            *,
            timeout: float,
            session_id: str | None = None,
        ) -> dict:
            del params, timeout, session_id
            if method == "Runtime.runIfWaitingForDebugger":
                raise CDPCommandError(method, -32601, "method not found")
            if method == "Target.detachFromTarget":
                raise CDPCommandError(method, -32000, "detach failed")
            return {}

    with pytest.raises(CDPCommandError) as caught:
        browser_probe._consume_cdp_events(  # type: ignore[arg-type]
            StuckUnknownClient(),
            configured_sessions=set(),
            session_targets={},
            session_target_types={},
            contexts={},
            frame_urls={},
            frame_sessions={},
            dashboard_requests=[],
            legacy_requests=[],
            dashboard_response_evidence=[],
            dashboard_request_indices={},
            console_errors=[],
            page_errors=[],
        )
    assert caught.value.method == "Runtime.runIfWaitingForDebugger"


def test_attached_session_without_target_info_is_resumed_and_detached() -> None:
    class MalformedAttachClient:
        def __init__(self) -> None:
            self.events = deque(
                [
                    {
                        "method": "Target.attachedToTarget",
                        "params": {"sessionId": "orphan-session", "targetInfo": {}},
                    }
                ]
            )
            self.calls: list[tuple[str, dict, str | None]] = []

        def next_event(self, *, timeout: float) -> dict:
            del timeout
            if not self.events:
                raise TimeoutError
            return self.events.popleft()

        def call(
            self,
            method: str,
            params: dict | None = None,
            *,
            timeout: float,
            session_id: str | None = None,
        ) -> dict:
            del timeout
            self.calls.append((method, params or {}, session_id))
            return {}

    client = MalformedAttachClient()
    assert browser_probe._consume_cdp_events(  # type: ignore[arg-type]
        client,
        configured_sessions=set(),
        session_targets={},
        session_target_types={},
        contexts={},
        frame_urls={},
        frame_sessions={},
        dashboard_requests=[],
        legacy_requests=[],
        dashboard_response_evidence=[],
        dashboard_request_indices={},
        console_errors=[],
        page_errors=[],
    ) is False
    assert client.calls == [
        ("Runtime.runIfWaitingForDebugger", {}, "orphan-session"),
        ("Target.detachFromTarget", {"sessionId": "orphan-session"}, None),
    ]


def test_attached_page_configuration_error_detaches_and_preserves_error() -> None:
    class FailingEventClient:
        def __init__(self) -> None:
            self.events = deque(
                [
                    {
                        "method": "Target.attachedToTarget",
                        "params": {
                            "sessionId": "bad-session",
                            "targetInfo": {
                                "targetId": "bad-target",
                                "type": "page",
                                "url": "https://example.test/",
                            },
                        },
                    }
                ]
            )
            self.calls: list[tuple[str, dict, str | None]] = []

        def next_event(self, *, timeout: float) -> dict:
            del timeout
            if not self.events:
                raise TimeoutError
            return self.events.popleft()

        def call(
            self,
            method: str,
            params: dict | None = None,
            *,
            timeout: float,
            session_id: str | None = None,
        ) -> dict:
            del timeout
            self.calls.append((method, params or {}, session_id))
            if method == "Page.enable":
                raise CDPCommandError(method, -32601, "method not found")
            return {}

    client = FailingEventClient()
    session_targets: dict[str, str] = {}
    session_target_types: dict[str, str] = {}
    with pytest.raises(CDPCommandError) as caught:
        browser_probe._consume_cdp_events(  # type: ignore[arg-type]
            client,
            configured_sessions=set(),
            session_targets=session_targets,
            session_target_types=session_target_types,
            contexts={},
            frame_urls={},
            frame_sessions={},
            dashboard_requests=[],
            legacy_requests=[],
            dashboard_response_evidence=[],
            dashboard_request_indices={},
            console_errors=[],
            page_errors=[],
        )
    assert caught.value.method == "Page.enable"
    methods = [method for method, _params, _session_id in client.calls]
    assert methods[-2:] == [
        "Runtime.runIfWaitingForDebugger",
        "Target.detachFromTarget",
    ]
    assert session_targets == {}
    assert session_target_types == {}


def test_browser_summary_retains_failures_and_requires_200_formal_samples() -> None:
    attempts = [_browser_attempt(index, total_ms=float(index)) for index in range(1, 200)]
    attempts[-1] = _browser_attempt(
        199,
        total_ms=5000,
        success=False,
        error="timeout",
        ready=None,
    )
    summary = summarize_browser_attempts(
        attempts,
        browser_product="Chrome/140.0.0.0",
        browser_user_agent="Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36",
        allow_edge_acceptance=False,
        edge_approval_reference=None,
        metadata_complete=True,
    )
    assert summary["attempt_count"] == 199
    assert summary["formal_acceptance"] is False
    assert summary["acceptance"] is False
    assert summary["failures"] == [{"index": 199, "error": "timeout"}]
    assert summary["latency_ms"]["sample_count"] == 199


def test_browser_summary_fails_closed_on_missing_ready_or_dashboard_request() -> None:
    attempts = [_browser_attempt(index, total_ms=500) for index in range(1, 201)]
    attempts[0] = _browser_attempt(1, total_ms=500, ready=None)
    attempts[1] = _browser_attempt(2, total_ms=500, dashboard_request_urls=[])
    summary = summarize_browser_attempts(
        attempts,
        browser_product="Chrome/140.0.0.0",
        browser_user_agent="Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36",
        allow_edge_acceptance=False,
        edge_approval_reference=None,
        metadata_complete=True,
    )
    assert summary["acceptance"] is False
    assert "ready_invalid" in summary["gate_failures"]
    assert "dashboard_request_count_invalid" in summary["gate_failures"]


def test_browser_summary_fails_closed_on_missing_or_cached_dashboard_response() -> None:
    attempts = [_browser_attempt(index, total_ms=500) for index in range(1, 201)]
    attempts[0] = _browser_attempt(
        1,
        total_ms=500,
        dashboard_response_evidence=[],
    )
    attempts[1] = _browser_attempt(
        2,
        total_ms=500,
        dashboard_response_evidence=[
            {
                "url": "https://bond.finailab.cn/bond-factor-lab/api/factor-lab/dashboard",
                "response_received": True,
                "redirected": False,
                "status": 200,
                "from_disk_cache": False,
                "from_service_worker": False,
                "from_prefetch_cache": True,
            }
        ],
    )
    attempts[2] = _browser_attempt(
        3,
        total_ms=500,
        dashboard_response_evidence=[
            {
                "url": "https://bond.finailab.cn/bond-factor-lab/api/factor-lab/dashboard",
                "response_received": True,
                "redirected": True,
                "status": 302,
                "from_disk_cache": False,
                "from_service_worker": False,
                "from_prefetch_cache": False,
            }
        ],
    )
    invalid_status = dict(attempts[3].dashboard_response_evidence[0])
    invalid_status["status"] = 500
    attempts[3] = _browser_attempt(
        4,
        total_ms=500,
        dashboard_response_evidence=[invalid_status],
    )
    mismatched_url = dict(attempts[4].dashboard_response_evidence[0])
    mismatched_url["url"] = "https://other.finailab.cn/api/factor-lab/dashboard"
    attempts[4] = _browser_attempt(
        5,
        total_ms=500,
        dashboard_response_evidence=[mismatched_url],
    )
    summary = summarize_browser_attempts(
        attempts,
        browser_product="Chrome/140.0.0.0",
        browser_user_agent="Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36",
        allow_edge_acceptance=False,
        edge_approval_reference=None,
        metadata_complete=True,
    )
    assert summary["acceptance"] is False
    assert "dashboard_response_count_invalid" in summary["gate_failures"]
    assert "dashboard_cache_or_service_worker" in summary["gate_failures"]
    assert "dashboard_redirect_detected" in summary["gate_failures"]
    assert "dashboard_response_status_invalid" in summary["gate_failures"]
    assert "dashboard_response_evidence_invalid" in summary["gate_failures"]


def test_browser_formal_gate_requires_chrome_or_explicit_edge_approval() -> None:
    attempts = [_browser_attempt(index, total_ms=500) for index in range(1, 201)]
    chrome = summarize_browser_attempts(
        attempts,
        browser_product="Chrome/140.0.0.0",
        browser_user_agent="Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36",
        allow_edge_acceptance=False,
        edge_approval_reference=None,
        metadata_complete=True,
    )
    edge = summarize_browser_attempts(
        attempts,
        browser_product="Edg/140.0.0.0",
        browser_user_agent="Mozilla/5.0 Chrome/140.0.0.0 Edg/140.0.0.0",
        allow_edge_acceptance=False,
        edge_approval_reference=None,
        metadata_complete=True,
    )
    approved_edge = summarize_browser_attempts(
        attempts,
        browser_product="Edg/140.0.0.0",
        browser_user_agent="Mozilla/5.0 Chrome/140.0.0.0 Edg/140.0.0.0",
        allow_edge_acceptance=True,
        edge_approval_reference="user-approved-2026-07-22",
        metadata_complete=True,
    )
    blank_edge_approval = summarize_browser_attempts(
        attempts,
        browser_product="Edg/140.0.0.0",
        browser_user_agent="Mozilla/5.0 Chrome/140.0.0.0 Edg/140.0.0.0",
        allow_edge_acceptance=True,
        edge_approval_reference="   ",
        metadata_complete=True,
    )
    unknown_edge_approval = summarize_browser_attempts(
        attempts,
        browser_product="Edg/140.0.0.0",
        browser_user_agent="Mozilla/5.0 Chrome/140.0.0.0 Edg/140.0.0.0",
        allow_edge_acceptance=True,
        edge_approval_reference="UNKNOWN",
        metadata_complete=True,
    )
    assert chrome["acceptance"] is True
    assert edge["acceptance"] is False
    assert "browser_not_approved" in edge["gate_failures"]
    assert approved_edge["acceptance"] is True
    assert blank_edge_approval["acceptance"] is False
    assert unknown_edge_approval["acceptance"] is False

    conflicting = summarize_browser_attempts(
        attempts,
        browser_product="Chrome/140.0.0.0",
        browser_user_agent="Mozilla/5.0 Chrome/140.0.0.0 Edg/140.0.0.0",
        allow_edge_acceptance=True,
        edge_approval_reference="user-approved-2026-07-22",
        metadata_complete=True,
    )
    unknown = summarize_browser_attempts(
        attempts,
        browser_product="unknown",
        browser_user_agent="Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36",
        allow_edge_acceptance=False,
        edge_approval_reference=None,
        metadata_complete=True,
    )
    assert conflicting["acceptance"] is False
    assert unknown["acceptance"] is False
    assert "browser_identity_mismatch" in conflicting["gate_failures"]
    assert "browser_identity_mismatch" in unknown["gate_failures"]


def test_close_target_requires_explicit_success_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FalseCloseClient:
        def call(
            self,
            method: str,
            params: dict | None = None,
            *,
            timeout: float,
        ) -> dict:
            assert method == "Target.closeTarget"
            assert params == {"targetId": "target-1"}
            assert timeout == 2
            return {"success": False}

    fallback_calls: list[str] = []
    monkeypatch.setattr(
        browser_probe,
        "_close_page",
        lambda _browser, target_id: fallback_calls.append(target_id) or True,
    )
    assert browser_probe._close_target_with_fallback(  # type: ignore[arg-type]
        FalseCloseClient(), object(), "target-1"
    ) is True
    assert fallback_calls == ["target-1"]

    monkeypatch.setattr(browser_probe, "_close_page", lambda *_args: False)
    assert browser_probe._close_target_with_fallback(  # type: ignore[arg-type]
        FalseCloseClient(), object(), "target-1"
    ) is False


def test_browser_formal_gate_requires_expected_ready_counts() -> None:
    attempts = [_browser_attempt(index, total_ms=500) for index in range(1, 201)]
    summary = summarize_browser_attempts(
        attempts,
        browser_product="Chrome/140.0.0.0",
        browser_user_agent="Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36",
        allow_edge_acceptance=False,
        edge_approval_reference=None,
        metadata_complete=True,
        expected_counts_configured=False,
    )
    assert summary["acceptance"] is False
    assert "expected_counts_not_configured" in summary["gate_failures"]


def test_browser_exit_code_distinguishes_enforced_199_from_smoke() -> None:
    attempts = [_browser_attempt(index, total_ms=500) for index in range(1, 200)]
    summary = summarize_browser_attempts(
        attempts,
        browser_product="Chrome/140.0.0.0",
        browser_user_agent="Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36",
        allow_edge_acceptance=False,
        edge_approval_reference=None,
        metadata_complete=True,
    )
    assert browser_exit_code(summary, mode="attempts", enforce_slo=True) == 2
    assert browser_exit_code(summary, mode="attempts", enforce_slo=False) == 0


def test_browser_exit_code_automatically_enforces_200_sample_gate() -> None:
    attempts = [_browser_attempt(index, total_ms=500) for index in range(1, 201)]
    summary = summarize_browser_attempts(
        attempts,
        browser_product="Chrome/140.0.0.0",
        browser_user_agent="Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36",
        allow_edge_acceptance=False,
        edge_approval_reference=None,
        metadata_complete=True,
    )
    assert browser_exit_code(summary, mode="attempts", enforce_slo=False) == 0
    failed = dict(summary, acceptance=False)
    assert browser_exit_code(failed, mode="attempts", enforce_slo=False) == 1


def test_browser_main_enforce_slo_exits_two_for_successful_199(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    browser = browser_probe.OwnedBrowser(
        process=object(),  # type: ignore[arg-type]
        profile_directory=tmp_path / "profile",
        debug_port=1,
        browser_websocket_url="ws://127.0.0.1/devtools/browser/test",
        product="Chrome/140.0.0.0",
        user_agent="Chrome test",
        protocol_version="1.3",
    )
    attempts = [_browser_attempt(index, total_ms=500) for index in range(1, 200)]
    monkeypatch.setattr(browser_probe, "start_owned_browser", lambda _binary: browser)
    monkeypatch.setattr(browser_probe, "stop_owned_browser", lambda _browser: None)
    monkeypatch.setattr(browser_probe, "_run_sequential", lambda _browser, _args: attempts)
    common = [
        "--url", "http://127.0.0.1/",
        "--browser-binary", "/bin/false",
        "--attempts", "199",
        "--probe-location", "local",
        "--device", "test",
        "--network", "loopback",
        "--connection-condition", "fresh-page",
        "--expected-scheme-count", "35",
        "--expected-live-row-count", "1000",
        "--expected-backtest-row-count", "8000",
    ]
    assert browser_probe.main(
        [*common, "--enforce-slo", "--output-json", str(tmp_path / "enforced.json")]
    ) == 2
    assert browser_probe.main(
        [*common, "--output-json", str(tmp_path / "smoke.json")]
    ) == 0


def test_browser_attempt_propagates_keyboard_interrupt_and_keeps_cleanup_in_finally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    browser = browser_probe.OwnedBrowser(
        process=object(),  # type: ignore[arg-type]
        profile_directory=tmp_path,
        debug_port=1,
        browser_websocket_url="ws://127.0.0.1/devtools/browser/test",
        product="Chrome/140.0.0.0",
        user_agent="Chrome test",
        protocol_version="1.3",
    )
    monkeypatch.setattr(
        browser_probe,
        "_connect_websocket",
        lambda _url: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        browser_probe.run_browser_attempt(
            browser=browser,
            url="http://127.0.0.1/",
            index=1,
            timeout_seconds=1,
            wait_before_seconds=0,
            ready_frame_url_substring=None,
        )


def test_browser_cli_rejects_mixed_or_incomplete_soak_modes() -> None:
    with pytest.raises(SystemExit):
        parse_browser_arguments(
            [
                "--url", "http://127.0.0.1/", "--browser-binary", "/bin/false",
                "--attempts", "5", "--concurrency", "2", "--duration-seconds", "1",
                "--refresh-interval-seconds", "0.1",
            ]
        )

    for invalid_attempts in ("0", "-1"):
        with pytest.raises(SystemExit):
            parse_browser_arguments(
                [
                    "--url", "http://127.0.0.1/",
                    "--browser-binary", "/bin/false",
                    "--attempts", invalid_attempts,
                    "--output-json", "/tmp/report.json",
                ]
            )
    with pytest.raises(SystemExit):
        parse_browser_arguments(
            [
                "--url", "http://127.0.0.1/", "--browser-binary", "/bin/false",
                "--concurrency", "2", "--duration-seconds", "1",
                "--refresh-interval-seconds", "0.1", "--enforce-slo",
                "--output-json", "/tmp/report.json",
            ]
        )

    parsed = parse_browser_arguments(
        [
            "--url", "http://127.0.0.1/", "--browser-binary", "/bin/false",
            "--attempts", "5", "--output-json", "/tmp/report.json",
            "--expected-scheme-count", "35", "--expected-live-row-count", "1035",
            "--expected-backtest-row-count", "8925",
        ]
    )
    assert parsed.expected_scheme_count == 35
    with pytest.raises(SystemExit):
        parse_browser_arguments(
            [
                "--url", "http://127.0.0.1/", "--browser-binary", "/bin/false",
                "--attempts", "5", "--output-json", "/tmp/report.json",
                "--expected-scheme-count", "35",
            ]
        )
    with pytest.raises(SystemExit):
        parse_browser_arguments(
            [
                "--url", "http://127.0.0.1/", "--browser-binary", "/bin/false",
                "--concurrency", "2", "--duration-seconds", "1",
            ]
        )


def test_owned_browser_cleanup_terminates_then_kills_only_the_owned_process() -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.pid = 987654321

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.events.append("terminate")

        def wait(self, timeout: float) -> None:
            self.events.append(f"wait:{timeout}")
            if self.events.count(f"wait:{timeout}") == 1:
                raise subprocess.TimeoutExpired("edge", timeout)

        def kill(self) -> None:
            self.events.append("kill")

    process = FakeProcess()
    terminate_owned_process(process, timeout_seconds=0.1)  # type: ignore[arg-type]
    assert process.events == ["terminate", "wait:0.1", "kill", "wait:0.1"]


def test_api_cli_enforce_slo_rejects_fewer_than_200_attempts(
    dashboard_server: tuple[ThreadingHTTPServer, str], tmp_path: Path
) -> None:
    _, url = dashboard_server
    result = subprocess.run(
        [
            str(PYTHON),
            str(PROJECT_ROOT / "scripts/benchmark_factor_lab_dashboard.py"),
            "--url", url,
            "--attempts", "2",
            "--timeout", "2",
            "--enforce-slo",
            "--output-json", str(tmp_path / "report.json"),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["summary"]["formal_acceptance"] is False
    assert "insufficient_attempts" in report["summary"]["gate_failures"]


class _FrontendSmokeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        path = self.path.split("?", 1)[0]
        if path == "/api/factor-lab/dashboard":
            raw = json.dumps(_dashboard_payload(), separators=(",", ":")).encode()
            body = gzip.compress(raw)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Dashboard-Cache", "HIT")
            self.end_headers()
            self.wfile.write(body)
            return
        relative = {
            "/": "index.html",
            "/index.html": "index.html",
            "/aifin-shell.js": "aifin-shell.js",
            "/aifin-shell.css": "aifin-shell.css",
            "/assets/aifin-lab-icon.svg": "assets/aifin-lab-icon.svg",
            "/assets/aifin-lab-logo.svg": "assets/aifin-lab-logo.svg",
        }.get(path)
        if relative is None:
            self.send_error(404)
            return
        content = (PROJECT_ROOT / "frontend" / relative).read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            mimetypes.guess_type(relative)[0] or "application/octet-stream",
        )
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


class _ParentIframeSmokeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    child_url = ""

    def log_message(self, _format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        body = (
            "<!doctype html><html><body>"
            f'<iframe id="lab" src="{type(self).child_url}"></iframe>'
            "</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _WorkerFrontendSmokeHandler(_FrontendSmokeHandler):
    worker_dashboard_user_agents: list[str | None] = []

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        path = self.path.split("?", 1)[0]
        if path == "/api/factor-lab/dashboard":
            type(self).worker_dashboard_user_agents.append(
                self.headers.get("User-Agent")
            )
        if path == "/worker-page.html":
            body = (
                "<!doctype html><html><head>"
                "<link rel='icon' href='data:,'>"
                "</head><body><script>"
                "window.workerSmoke = new Worker('/worker-smoke.js');"
                "window.workerSmoke.onmessage = event => {"
                "window.__factorLabReady = event.data;};"
                "</script></body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/worker-smoke.js":
            body = (
                "fetch('/api/factor-lab/dashboard').then(response => response.json())"
                ".then(payload => self.postMessage({seq:1,"
                "snapshotId:payload.snapshot_id,committedAt:performance.now(),"
                "stale:payload.stale,source:'dashboard',"
                "schemeCount:payload.schemes.length,liveRowCount:0,"
                "backtestRowCount:0}));"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


@pytest.mark.skipif(
    os.environ.get("FACTOR_LAB_RUN_EDGE_SMOKE") != "1",
    reason="真实 Edge smoke 仅在显式本地工具验收时运行",
)
def test_real_edge_smoke_and_short_soak_use_actual_frontend(tmp_path: Path) -> None:
    """显式运行时，用真实 Edge/CDP 验证 actual index/JS 单 dashboard ready。"""

    edge = Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
    if not edge.is_file():
        pytest.skip("Microsoft Edge is not installed")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FrontendSmokeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = [
        str(PYTHON),
        str(PROJECT_ROOT / "scripts/benchmark_factor_lab_browser.py"),
        "--url", f"http://{host}:{port}/",
        "--timeout-seconds", "5",
        "--browser-binary", str(edge),
    ]
    try:
        smoke = subprocess.run(
            [*base, "--attempts", "5", "--output-json", str(tmp_path / "edge.json")],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert smoke.returncode == 0, smoke.stderr + smoke.stdout
        smoke_report = json.loads((tmp_path / "edge.json").read_text(encoding="utf-8"))
        assert smoke_report["summary"]["success_count"] == 5
        assert smoke_report["summary"]["formal_acceptance"] is False
        assert all(
            len(attempt["dashboard_request_urls"]) == 1
            and not attempt["legacy_request_urls"]
            for attempt in smoke_report["attempts"]
        )

        soak = subprocess.run(
            [
                *base,
                "--concurrency", "2",
                "--duration-seconds", "1",
                "--refresh-interval-seconds", "0.2",
                "--output-json", str(tmp_path / "soak.json"),
            ],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert soak.returncode == 0, soak.stderr + soak.stdout
        soak_report = json.loads((tmp_path / "soak.json").read_text(encoding="utf-8"))
        assert soak_report["mode"] == "soak"
        assert soak_report["summary"]["success_count"] >= 2
        assert soak_report["summary"]["formal_acceptance"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.skipif(
    os.environ.get("FACTOR_LAB_RUN_EDGE_SMOKE") != "1",
    reason="真实 Edge worker smoke 仅在显式本地工具验收时运行",
)
def test_real_edge_worker_target_collects_dashboard_and_fresh_ready(
    tmp_path: Path,
) -> None:
    """dedicated Worker 必须按 worker 域启用并保留其 dashboard 请求。"""

    edge = Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
    if not edge.is_file():
        pytest.skip("Microsoft Edge is not installed")
    _WorkerFrontendSmokeHandler.worker_dashboard_user_agents = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WorkerFrontendSmokeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        result = subprocess.run(
            [
                str(PYTHON),
                str(PROJECT_ROOT / "scripts/benchmark_factor_lab_browser.py"),
                "--url", f"http://{host}:{port}/worker-page.html",
                "--attempts", "1",
                "--timeout-seconds", "5",
                "--browser-binary", str(edge),
                "--output-json", str(tmp_path / "worker.json"),
            ],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr + result.stdout
        report = json.loads((tmp_path / "worker.json").read_text(encoding="utf-8"))
        assert report["summary"]["success_count"] == 1
        attempt = report["attempts"][0]
        assert attempt["ready"]["source"] == "dashboard"
        assert attempt["ready"]["stale"] is False
        assert attempt["dashboard_request_urls"] == [
            f"http://{host}:{port}/api/factor-lab/dashboard"
        ]
        assert attempt["dashboard_response_evidence"] == [
            {
                "url": f"http://{host}:{port}/api/factor-lab/dashboard",
                "response_received": True,
                "redirected": False,
                "status": 200,
                "from_disk_cache": False,
                "from_service_worker": False,
                "from_prefetch_cache": False,
            }
        ]
        assert attempt["legacy_request_urls"] == []
        assert attempt["console_errors"] == []
        assert attempt["page_errors"] == []
        assert _WorkerFrontendSmokeHandler.worker_dashboard_user_agents == [
            browser_probe.USER_AGENT
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.skipif(
    os.environ.get("FACTOR_LAB_RUN_EDGE_SMOKE") != "1",
    reason="真实 Edge OOPIF smoke 仅在显式本地工具验收时运行",
)
def test_real_edge_cross_origin_iframe_uses_child_ready_and_requests(
    tmp_path: Path,
) -> None:
    """父 localhost + 子 127.0.0.1 强制 OOPIF，父页面不能伪造 ready。"""

    edge = Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
    if not edge.is_file():
        pytest.skip("Microsoft Edge is not installed")
    child_server = ThreadingHTTPServer(("127.0.0.1", 0), _FrontendSmokeHandler)
    child_thread = threading.Thread(target=child_server.serve_forever, daemon=True)
    child_thread.start()
    child_host, child_port = child_server.server_address
    child_url = f"http://{child_host}:{child_port}/"
    _ParentIframeSmokeHandler.child_url = child_url
    parent_server = ThreadingHTTPServer(("127.0.0.1", 0), _ParentIframeSmokeHandler)
    parent_thread = threading.Thread(target=parent_server.serve_forever, daemon=True)
    parent_thread.start()
    _, parent_port = parent_server.server_address
    try:
        result = subprocess.run(
            [
                str(PYTHON),
                str(PROJECT_ROOT / "scripts/benchmark_factor_lab_browser.py"),
                "--url", f"http://localhost:{parent_port}/",
                "--ready-frame-url-substring", child_url,
                "--attempts", "1",
                "--timeout-seconds", "5",
                "--browser-binary", str(edge),
                "--output-json", str(tmp_path / "oopif.json"),
            ],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr + result.stdout
        report = json.loads((tmp_path / "oopif.json").read_text(encoding="utf-8"))
        assert report["summary"]["success_count"] == 1
        attempt = report["attempts"][0]
        assert attempt["ready"]["source"] == "dashboard"
        assert attempt["ready"]["stale"] is False
        assert attempt["dashboard_request_urls"] == [
            f"http://{child_host}:{child_port}/api/factor-lab/dashboard"
        ]
        assert attempt["legacy_request_urls"] == []
        assert attempt["console_errors"] == []
        assert attempt["page_errors"] == []
    finally:
        parent_server.shutdown()
        parent_server.server_close()
        parent_thread.join(timeout=3)
        child_server.shutdown()
        child_server.server_close()
        child_thread.join(timeout=3)
