"""Factor Lab 公网性能探针的统计、协议和本地传输测试。"""

from __future__ import annotations

import gzip
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.benchmark_factor_lab_browser import (
    BrowserAttempt,
    CDPClient,
    CDPCommandError,
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

    def log_message(self, _format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        type(self).connection_ids.append(self.client_address[1])
        type(self).accept_encodings.append(self.headers.get("Accept-Encoding", ""))
        if type(self).delay_seconds:
            time.sleep(type(self).delay_seconds)
        raw = json.dumps(
            _dashboard_payload(stale=type(self).stale), separators=(",", ":")
        ).encode("utf-8")
        body = gzip.compress(raw) if type(self).gzip_body else raw
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Dashboard-Cache", "HIT")
        if type(self).stale:
            self.send_header("X-Dashboard-Warning", "stale-last-known-good")
        if type(self).gzip_body:
            self.send_header("Content-Encoding", "gzip")
        if type(self).close_after_response:
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
        "main": "https://shell.example/",
        "child": "https://bond.finailab.cn/bond-factor-lab/",
    }
    assert select_ready_context(contexts, frames, "main", None) == 1
    assert select_ready_context(contexts, frames, "main", "bond-factor-lab") == 2
    assert select_ready_context({1: "main"}, frames, "main", "bond-factor-lab") is None


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
        allow_edge_acceptance=False,
        edge_approval_reference=None,
        metadata_complete=True,
    )
    assert summary["acceptance"] is False
    assert "ready_invalid" in summary["gate_failures"]
    assert "dashboard_request_count_invalid" in summary["gate_failures"]


def test_browser_formal_gate_requires_chrome_or_explicit_edge_approval() -> None:
    attempts = [_browser_attempt(index, total_ms=500) for index in range(1, 201)]
    chrome = summarize_browser_attempts(
        attempts,
        browser_product="Chrome/140.0.0.0",
        allow_edge_acceptance=False,
        edge_approval_reference=None,
        metadata_complete=True,
    )
    edge = summarize_browser_attempts(
        attempts,
        browser_product="Edg/140.0.0.0",
        allow_edge_acceptance=False,
        edge_approval_reference=None,
        metadata_complete=True,
    )
    approved_edge = summarize_browser_attempts(
        attempts,
        browser_product="Edg/140.0.0.0",
        allow_edge_acceptance=True,
        edge_approval_reference="user-approved-2026-07-22",
        metadata_complete=True,
    )
    assert chrome["acceptance"] is True
    assert edge["acceptance"] is False
    assert "browser_not_approved" in edge["gate_failures"]
    assert approved_edge["acceptance"] is True


def test_browser_formal_gate_requires_expected_ready_counts() -> None:
    attempts = [_browser_attempt(index, total_ms=500) for index in range(1, 201)]
    summary = summarize_browser_attempts(
        attempts,
        browser_product="Chrome/140.0.0.0",
        allow_edge_acceptance=False,
        edge_approval_reference=None,
        metadata_complete=True,
        expected_counts_configured=False,
    )
    assert summary["acceptance"] is False
    assert "expected_counts_not_configured" in summary["gate_failures"]


def test_browser_cli_rejects_mixed_or_incomplete_soak_modes() -> None:
    with pytest.raises(SystemExit):
        parse_browser_arguments(
            [
                "--url", "http://127.0.0.1/", "--browser-binary", "/bin/false",
                "--attempts", "5", "--concurrency", "2", "--duration-seconds", "1",
                "--refresh-interval-seconds", "0.1",
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
