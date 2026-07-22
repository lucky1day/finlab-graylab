#!/usr/bin/env python3
"""用标准库测量 Factor Lab dashboard API 的真实传输阶段。"""

from __future__ import annotations

import argparse
import gzip
import http.client
import json
import math
import os
import re
import socket
import ssl
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import SplitResult, urlsplit


USER_AGENT = "bond-factor-lab-api-benchmark/1.0"
SCHEMA_VERSION = "factor-lab-dashboard-v1"
ROW_FIELDS = [
    "predict_date",
    "feature_date",
    "target_date",
    "prediction_phase",
    "predicted_direction",
    "actual_direction",
]
TOP_LEVEL_FIELDS = {
    "schema_version",
    "snapshot_id",
    "generated_at",
    "display_until",
    "stale",
    "snapshot_age_ms",
    "row_fields",
    "target_labels",
    "schemes",
}
SCHEME_FIELDS = {
    "scheme_id",
    "base_scheme_id",
    "name",
    "description",
    "horizon",
    "task_type",
    "frequency",
    "target_tenor",
    "target_label",
    "status",
    "deployed_at",
    "live_rows",
    "backtest",
}
BACKTEST_FIELDS = {
    "benchmark_id",
    "benchmark_label",
    "data_source",
    "data_source_label",
    "latest_run_date",
    "rows",
}
TASK_TYPES = {"T+1", "T+5", "weekly_point", "weekly_average", "monthly"}
LIVE_PHASES = {"gray_live", "scheduled_live"}
MAX_SAFE_INTEGER = 9_007_199_254_740_991
SNAPSHOT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
GENERATED_AT_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})"
    r"(?:\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$"
)


@dataclass(frozen=True)
class Attempt:
    """一次 API 尝试；失败与超时同样是正式样本。"""

    index: int
    started_at: str
    wait_before_seconds: float
    status: int | None
    dns_ms: float | None
    tcp_ms: float | None
    tls_ms: float | None
    ttfb_ms: float | None
    total_ms: float
    wire_bytes: int
    raw_bytes: int
    content_encoding: str | None
    cache_status: str | None
    stale_header: str | None
    server_timing: str | None
    schema_valid: bool
    stale: bool | None
    connection_reused: bool
    reconnected: bool
    success: bool
    error: str | None


def nearest_rank(values: list[float], percentile: float) -> float:
    """返回 nearest-rank 百分位；percentile 使用 ``(0, 1]``。"""

    if not values:
        raise ValueError("nearest_rank requires at least one sample")
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be in (0, 1]")
    if any(not math.isfinite(value) for value in values):
        raise ValueError("samples must be finite")
    ordered = sorted(values)
    return ordered[math.ceil(percentile * len(ordered)) - 1]


def _latency_summary(attempts: list[Attempt]) -> dict[str, float | int | None]:
    values = [attempt.total_ms for attempt in attempts]
    return {
        "sample_count": len(values),
        "p50": nearest_rank(values, 0.50) if values else None,
        "p95": nearest_rank(values, 0.95) if values else None,
        "p99": nearest_rank(values, 0.99) if values else None,
    }


def summarize_attempts(attempts: list[Attempt]) -> dict[str, Any]:
    """汇总全部尝试；任何失败、stale 或 schema 错误都会阻断验收。"""

    latency = _latency_summary(attempts)
    failures = [
        {"index": attempt.index, "error": attempt.error or "probe_failed"}
        for attempt in attempts
        if not attempt.success
    ]
    formal = len(attempts) >= 200
    gate_failures: list[str] = []
    if not formal:
        gate_failures.append("insufficient_attempts")
    if failures:
        gate_failures.append("attempt_failures")
    if any(attempt.stale is True for attempt in attempts):
        gate_failures.append("stale_response")
    if any(not attempt.schema_valid for attempt in attempts):
        gate_failures.append("schema_invalid")
    if latency["p95"] is None or float(latency["p95"]) >= 1000:
        gate_failures.append("p95_not_below_1000ms")
    return {
        "attempt_count": len(attempts),
        "formal_acceptance": formal,
        "acceptance": formal and not gate_failures,
        "latency_ms": latency,
        "success_count": sum(attempt.success for attempt in attempts),
        "failure_count": len(failures),
        "failures": failures,
        "stale_count": sum(attempt.stale is True for attempt in attempts),
        "schema_invalid_count": sum(not attempt.schema_valid for attempt in attempts),
        "gate_failures": gate_failures,
    }


class _TimedConnectionMixin:
    """在 http.client connect 阶段显式采样 DNS/TCP/TLS。"""

    stage_dns_ms: float | None
    stage_tcp_ms: float | None
    stage_tls_ms: float | None

    def reset_stages(self) -> None:
        self.stage_dns_ms = None
        self.stage_tcp_ms = None
        self.stage_tls_ms = None

    def _open_tcp_socket(self) -> socket.socket:
        dns_started = time.perf_counter()
        addresses = socket.getaddrinfo(
            self.host,
            self.port,
            type=socket.SOCK_STREAM,
        )
        self.stage_dns_ms = (time.perf_counter() - dns_started) * 1000
        if not addresses:
            raise OSError("DNS returned no addresses")

        tcp_started = time.perf_counter()
        last_error: OSError | None = None
        for family, socktype, protocol, _canonical, sockaddr in addresses:
            candidate = socket.socket(family, socktype, protocol)
            try:
                candidate.settimeout(self.timeout)
                if self.source_address:
                    candidate.bind(self.source_address)
                candidate.connect(sockaddr)
                candidate.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.stage_tcp_ms = (time.perf_counter() - tcp_started) * 1000
                return candidate
            except OSError as error:
                last_error = error
                candidate.close()
        self.stage_tcp_ms = (time.perf_counter() - tcp_started) * 1000
        if last_error is not None:
            raise last_error
        raise OSError("TCP connection failed")


class TimedHTTPConnection(_TimedConnectionMixin, http.client.HTTPConnection):
    """记录连接阶段的 HTTP/1.1 connection。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.reset_stages()

    def connect(self) -> None:
        if self._tunnel_host:
            raise ValueError("HTTP CONNECT tunnels are not supported by this probe")
        self.sock = self._open_tcp_socket()


class TimedHTTPSConnection(_TimedConnectionMixin, http.client.HTTPSConnection):
    """记录 DNS、TCP 和证书校验 TLS 阶段的 HTTPS connection。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.reset_stages()

    def connect(self) -> None:
        if self._tunnel_host:
            raise ValueError("HTTP CONNECT tunnels are not supported by this probe")
        plain_socket = self._open_tcp_socket()
        tls_started = time.perf_counter()
        try:
            self.sock = self._context.wrap_socket(
                plain_socket,
                server_hostname=self.host,
            )
        except BaseException:
            plain_socket.close()
            raise
        self.stage_tls_ms = (time.perf_counter() - tls_started) * 1000


def _validated_url(url: str) -> SplitResult:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("url scheme must be http or https")
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("url must have a host and must not contain credentials or fragment")
    return parsed


def _new_connection(parsed: SplitResult, timeout: float) -> http.client.HTTPConnection:
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        return TimedHTTPSConnection(
            parsed.hostname,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    return TimedHTTPConnection(parsed.hostname, port, timeout=timeout)


def _request_target(parsed: SplitResult) -> str:
    path = parsed.path or "/"
    return f"{path}?{parsed.query}" if parsed.query else path


def _is_integer(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value).is_integer()
        and abs(float(value)) <= MAX_SAFE_INTEGER
    )


def _canonical_string(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError("structured string is invalid")
    if value:
        boundary = "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f"
        boundary += "\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f"
        boundary += " \x7f\x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
        boundary += "\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
        if value[0] in boundary or value[-1] in boundary:
            raise ValueError("structured string has a boundary character")
    return value


def _iso_date(value: object) -> str:
    text = _canonical_string(value)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError("ISO date is invalid") from error
    if parsed.strftime("%Y-%m-%d") != text:
        raise ValueError("ISO date is not canonical")
    return text


def _aware_datetime(value: object) -> str:
    text = _canonical_string(value)
    match = GENERATED_AT_PATTERN.fullmatch(text)
    if match is None:
        raise ValueError("generated_at is not a canonical aware datetime")
    _iso_date(match.group(1))
    if int(match.group(2)) > 23 or int(match.group(3)) > 59 or int(match.group(4)) > 59:
        raise ValueError("generated_at time is invalid")
    offset = match.group(5)
    if offset != "Z" and (int(offset[1:3]) > 23 or int(offset[4:6]) > 59):
        raise ValueError("generated_at offset is invalid")
    return text


def _direction(value: object, *, allow_none: bool) -> None:
    if value is None and allow_none:
        return
    if not _is_integer(value) or int(value) not in {-1, 0, 1}:
        raise ValueError("direction is invalid")


def _validate_rows(rows: object, *, source: str) -> None:
    if not isinstance(rows, list):
        raise ValueError("rows must be an array")
    previous_key: tuple[str, str] | None = None
    targets: set[str] = set()
    for row in rows:
        if not isinstance(row, list) or len(row) != len(ROW_FIELDS):
            raise ValueError("compact row width is invalid")
        predict_date = _iso_date(row[0])
        _iso_date(row[1])
        target_date = _iso_date(row[2])
        phase = row[3]
        if source == "live" and phase not in LIVE_PHASES:
            raise ValueError("live phase is invalid")
        if source == "backtest" and phase is not None:
            raise ValueError("backtest phase must be null")
        _direction(row[4], allow_none=False)
        _direction(row[5], allow_none=source == "live")
        if target_date in targets:
            raise ValueError("duplicate canonical target_date")
        targets.add(target_date)
        sort_key = (target_date, predict_date)
        if previous_key is not None and sort_key < previous_key:
            raise ValueError("rows are not canonically sorted")
        previous_key = sort_key


def validate_dashboard_schema(payload: object) -> None:
    """校验 benchmark 所需的 dashboard V1 结构，不导入应用代码。"""

    if not isinstance(payload, dict) or set(payload) != TOP_LEVEL_FIELDS:
        raise ValueError("top-level dashboard fields are invalid")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    if (
        not isinstance(payload["snapshot_id"], str)
        or SNAPSHOT_ID_PATTERN.fullmatch(payload["snapshot_id"]) is None
    ):
        raise ValueError("snapshot_id is invalid")
    _aware_datetime(payload["generated_at"])
    _iso_date(payload["display_until"])
    if not isinstance(payload["stale"], bool):
        raise ValueError("stale is invalid")
    if not _is_integer(payload["snapshot_age_ms"]) or payload["snapshot_age_ms"] < 0:
        raise ValueError("snapshot_age_ms is invalid")
    if payload["row_fields"] != ROW_FIELDS:
        raise ValueError("row_fields do not match dashboard v1")
    if not isinstance(payload["target_labels"], dict):
        raise ValueError("target_labels is invalid")
    target_labels: dict[str, str] = {}
    for target, label in payload["target_labels"].items():
        target_labels[_canonical_string(target)] = _canonical_string(label)
    schemes = payload["schemes"]
    if not isinstance(schemes, list):
        raise ValueError("schemes is invalid")
    previous_scheme_id: str | None = None
    seen_scheme_ids: set[str] = set()
    for scheme in schemes:
        if not isinstance(scheme, dict) or set(scheme) != SCHEME_FIELDS:
            raise ValueError("scheme fields are invalid")
        scheme_id = _canonical_string(scheme["scheme_id"])
        base_scheme_id = _canonical_string(scheme["base_scheme_id"])
        target_tenor = _canonical_string(scheme["target_tenor"])
        horizon = scheme["horizon"]
        if not _is_integer(horizon) or int(horizon) < 1:
            raise ValueError("horizon is invalid")
        if scheme_id != f"{base_scheme_id}__h{int(horizon)}__{target_tenor}":
            raise ValueError("scheme composite identity is invalid")
        if scheme_id in seen_scheme_ids:
            raise ValueError("duplicate scheme_id")
        if previous_scheme_id is not None and scheme_id < previous_scheme_id:
            raise ValueError("schemes are not sorted by Unicode code point")
        previous_scheme_id = scheme_id
        seen_scheme_ids.add(scheme_id)
        _canonical_string(scheme["name"])
        if not isinstance(scheme["description"], str):
            raise ValueError("description is invalid")
        if scheme["task_type"] not in TASK_TYPES:
            raise ValueError("task_type is invalid")
        _canonical_string(scheme["frequency"])
        target_label = _canonical_string(scheme["target_label"])
        if target_labels.get(target_tenor) != target_label:
            raise ValueError("target label identity is invalid")
        if scheme["status"] != "active":
            raise ValueError("status must be active")
        _iso_date(scheme["deployed_at"])
        _validate_rows(scheme["live_rows"], source="live")
        backtest = scheme["backtest"]
        if backtest is not None:
            if not isinstance(backtest, dict) or set(backtest) != BACKTEST_FIELDS:
                raise ValueError("backtest fields are invalid")
            for field in (
                "benchmark_id",
                "benchmark_label",
                "data_source",
                "data_source_label",
            ):
                _canonical_string(backtest[field])
            _iso_date(backtest["latest_run_date"])
            _validate_rows(backtest["rows"], source="backtest")


_RECONNECTABLE = (
    BrokenPipeError,
    ConnectionResetError,
    http.client.CannotSendRequest,
    http.client.RemoteDisconnected,
    http.client.ResponseNotReady,
)


def _single_attempt(
    *,
    index: int,
    started_at: str,
    wait_before_seconds: float,
    parsed: SplitResult,
    timeout_seconds: float,
    connection: http.client.HTTPConnection | None,
) -> tuple[Attempt, http.client.HTTPConnection]:
    attempt_started = time.perf_counter()
    status: int | None = None
    ttfb_ms: float | None = None
    wire = b""
    raw = b""
    encoding: str | None = None
    cache_status: str | None = None
    stale_header: str | None = None
    server_timing: str | None = None
    schema_valid = False
    stale: bool | None = None
    error_message: str | None = None
    reconnected = False
    existing_socket = connection is not None and connection.sock is not None
    if connection is None:
        connection = _new_connection(parsed, timeout_seconds)

    try:
        for request_number in range(2):
            assert isinstance(connection, _TimedConnectionMixin)
            connection.reset_stages()
            request_started = time.perf_counter()
            try:
                connection.request(
                    "GET",
                    _request_target(parsed),
                    headers={
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip",
                        "Connection": "keep-alive",
                        "User-Agent": USER_AGENT,
                    },
                )
                response = connection.getresponse()
                ttfb_ms = (time.perf_counter() - request_started) * 1000
                status = response.status
                encoding = response.getheader("Content-Encoding")
                cache_status = response.getheader("X-Dashboard-Cache")
                stale_header = response.getheader("X-Dashboard-Warning")
                server_timing = response.getheader("Server-Timing")
                wire = response.read()
                break
            except _RECONNECTABLE:
                if request_number:
                    raise
                connection.close()
                connection = _new_connection(parsed, timeout_seconds)
                existing_socket = False
                reconnected = True
        normalized_encoding = encoding.casefold().strip() if encoding else None
        if normalized_encoding is None:
            raw = wire
        elif normalized_encoding == "gzip":
            raw = gzip.decompress(wire)
        else:
            raise ValueError(f"unsupported content-encoding: {encoding}")
        payload = json.loads(raw.decode("utf-8"))
        validate_dashboard_schema(payload)
        schema_valid = True
        stale = payload["stale"]
        if normalized_encoding != "gzip":
            raise ValueError("gzip content-encoding is required for this probe")
        if status != 200:
            raise ValueError(f"unexpected HTTP status {status}")
        if stale or stale_header:
            raise ValueError("dashboard response is stale")
    except Exception as error:  # 业务失败转为 attempt；进程中断必须向上传播。
        error_message = f"{type(error).__name__}: {error}"
    total_ms = (time.perf_counter() - attempt_started) * 1000
    assert isinstance(connection, _TimedConnectionMixin)
    attempt = Attempt(
        index=index,
        started_at=started_at,
        wait_before_seconds=wait_before_seconds,
        status=status,
        dns_ms=connection.stage_dns_ms,
        tcp_ms=connection.stage_tcp_ms,
        tls_ms=connection.stage_tls_ms,
        ttfb_ms=ttfb_ms,
        total_ms=total_ms,
        wire_bytes=len(wire),
        raw_bytes=len(raw),
        content_encoding=encoding,
        cache_status=cache_status,
        stale_header=stale_header,
        server_timing=server_timing,
        schema_valid=schema_valid,
        stale=stale,
        connection_reused=existing_socket and not reconnected,
        reconnected=reconnected,
        success=error_message is None,
        error=error_message,
    )
    if error_message is not None:
        connection.close()
    return attempt, connection


def run_benchmark(
    *,
    url: str,
    attempt_count: int,
    timeout_seconds: float,
    disable_keepalive: bool,
    minimum_attempt_period_seconds: float,
) -> tuple[list[Attempt], dict[str, Any]]:
    """运行顺序 API benchmark；节流从 attempt start 计算且不计入 latency。"""

    if attempt_count < 1:
        raise ValueError("attempt_count must be positive")
    if timeout_seconds <= 0 or minimum_attempt_period_seconds < 0:
        raise ValueError("timeout must be positive and period must be non-negative")
    parsed = _validated_url(url)
    attempts: list[Attempt] = []
    connection: http.client.HTTPConnection | None = None
    previous_attempt_started: float | None = None
    try:
        for index in range(1, attempt_count + 1):
            wait_seconds = 0.0
            if previous_attempt_started is not None:
                wait_seconds = max(
                    0.0,
                    minimum_attempt_period_seconds
                    - (time.monotonic() - previous_attempt_started),
                )
                if wait_seconds:
                    time.sleep(wait_seconds)
            previous_attempt_started = time.monotonic()
            if disable_keepalive and connection is not None:
                connection.close()
                connection = None
            attempt, connection = _single_attempt(
                index=index,
                started_at=datetime.now(timezone.utc).isoformat(),
                wait_before_seconds=wait_seconds,
                parsed=parsed,
                timeout_seconds=timeout_seconds,
                connection=connection,
            )
            attempts.append(attempt)
            if disable_keepalive:
                connection.close()
                connection = None
    finally:
        if connection is not None:
            connection.close()
    metadata = {
        "user_agent": USER_AGENT,
        "url_origin_and_path": f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}",
        "connection_reuse": "disabled" if disable_keepalive else "enabled",
        "reused_stage_representation": "null_when_stage_did_not_occur",
        "minimum_attempt_period_seconds": minimum_attempt_period_seconds,
        "timeout_seconds": timeout_seconds,
    }
    return attempts, metadata


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """仅向已存在父目录原子写报告，避免半份验收证据。"""

    destination = path.expanduser().resolve()
    parent = destination.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"output parent directory does not exist: {parent}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--attempts", type=int, default=200)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--disable-keepalive", action="store_true")
    parser.add_argument("--minimum-attempt-period-seconds", type=float, default=0.0)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--enforce-slo", action="store_true")
    parser.add_argument("--probe-location", default="unknown")
    parser.add_argument("--device", default="unknown")
    parser.add_argument("--network", default="unknown")
    parser.add_argument("--connection-condition", default="unknown")
    arguments = parser.parse_args(argv)
    if arguments.attempts < 1:
        parser.error("--attempts must be positive")
    if arguments.timeout <= 0:
        parser.error("--timeout must be positive")
    if arguments.minimum_attempt_period_seconds < 0:
        parser.error("--minimum-attempt-period-seconds must be non-negative")
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    started_at = datetime.now(timezone.utc)
    attempts, transport_metadata = run_benchmark(
        url=arguments.url,
        attempt_count=arguments.attempts,
        timeout_seconds=arguments.timeout,
        disable_keepalive=arguments.disable_keepalive,
        minimum_attempt_period_seconds=arguments.minimum_attempt_period_seconds,
    )
    summary = summarize_attempts(attempts)
    report = {
        "report_schema": "factor-lab-api-benchmark-v1",
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "timezone": time.tzname[0] if time.tzname else "unknown",
        "metadata": {
            **transport_metadata,
            "probe_location": arguments.probe_location,
            "device": arguments.device,
            "network": arguments.network,
            "connection_condition": arguments.connection_condition,
            "output_parent_policy": "parent_must_exist; atomic_replace",
        },
        "summary": summary,
        "attempts": [asdict(attempt) for attempt in attempts],
    }
    write_json_atomic(arguments.output_json, report)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if arguments.enforce_slo and not summary["formal_acceptance"]:
        return 2
    if summary["formal_acceptance"]:
        return 0 if summary["acceptance"] else 1
    return 0 if summary["failure_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
