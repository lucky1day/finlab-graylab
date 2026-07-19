from __future__ import annotations

import base64
import http.client
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlencode, urljoin, urlparse


class DataBridgeConfigurationError(ValueError):
    """DataBridge client configuration is missing or invalid."""


class DataBridgeRequestError(RuntimeError):
    """A DataBridge request failed after retries."""


class _TransientHttpError(http.client.HTTPException):
    pass


@dataclass(frozen=True)
class HttpPayload:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class DataBridgeClientConfig:
    base_url: str
    username: str
    password: str
    connect_timeout_sec: int = 10
    read_timeout_sec: int = 180
    retry_delays_sec: tuple[int, ...] = (1, 3, 10)

    @classmethod
    def from_env(cls) -> "DataBridgeClientConfig":
        _load_project_env()
        base_url = os.getenv("DATABRIDGE_API_BASE_URL", "").strip()
        username = os.getenv("DATABRIDGE_API_USERNAME", "").strip()
        password = os.getenv("DATABRIDGE_API_PASSWORD", "")
        if not base_url or not username or not password:
            raise DataBridgeConfigurationError(
                "DATABRIDGE_API_BASE_URL, DATABRIDGE_API_USERNAME and "
                "DATABRIDGE_API_PASSWORD must all be configured"
            )
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise DataBridgeConfigurationError("DATABRIDGE_API_BASE_URL must be an HTTP(S) URL")
        return cls(base_url=base_url.rstrip("/") + "/", username=username, password=password)


class DataBridgeClient:
    def __init__(self, config: DataBridgeClientConfig) -> None:
        self.config = config
        token = base64.b64encode(f"{config.username}:{config.password}".encode("utf-8")).decode("ascii")
        self.request_headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json, text/csv",
            "User-Agent": "bond-factor-lab-databridge-refresh/1.0",
        }

    def get_tables(self) -> dict[str, object]:
        payload = self._request_with_retry("tables/", {})
        self._require_status(payload, expected=200)
        try:
            result = json.loads(payload.body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DataBridgeRequestError("DataBridge tables response is not valid JSON") from exc
        if not isinstance(result, dict) or result.get("success") is not True:
            raise DataBridgeRequestError("DataBridge tables response did not report success")
        return result

    def export_csv(
        self,
        frequency: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        allow_empty: bool = False,
    ) -> bytes | None:
        if frequency not in {"日", "周", "月"}:
            raise ValueError(f"unsupported DataBridge frequency: {frequency}")
        params = {"frequency": frequency}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        payload = self._request_with_retry("export/csv/", params)
        if allow_empty and _is_empty_export_response(payload):
            return None
        self._require_status(payload, expected=200)
        content_type = str(payload.headers.get("content-type", "")).lower()
        if "text/csv" not in content_type and "application/csv" not in content_type:
            raise DataBridgeRequestError(
                f"DataBridge export returned unexpected content type: {content_type or '<missing>'}"
            )
        if not payload.body.strip():
            raise DataBridgeRequestError("DataBridge export returned an empty CSV")
        return payload.body

    def redact(self, value: object) -> str:
        text = str(value)
        if self.config.password:
            text = text.replace(self.config.password, "<redacted>")
        auth = self.request_headers.get("Authorization", "")
        if auth:
            text = text.replace(auth, "Basic <redacted>")
        return text

    def _request_with_retry(self, path: str, params: dict[str, str]) -> HttpPayload:
        delays = self.config.retry_delays_sec
        for attempt in range(len(delays) + 1):
            try:
                payload = self._request_once(path, params)
                if payload.status == 429 or 500 <= payload.status <= 599:
                    body = payload.body[:500].decode("utf-8", errors="replace")
                    raise _TransientHttpError(f"HTTP {payload.status}: {self.redact(body)}")
                return payload
            except (OSError, http.client.HTTPException, TimeoutError) as exc:
                if attempt >= len(delays):
                    raise DataBridgeRequestError(
                        f"DataBridge request failed after {attempt + 1} attempts: {self.redact(exc)}"
                    ) from exc
                time.sleep(delays[attempt])
        raise AssertionError("unreachable")

    def _request_once(self, path: str, params: dict[str, str]) -> HttpPayload:
        url = urljoin(self.config.base_url, path)
        parsed = urlparse(url)
        query = urlencode(params)
        request_path = parsed.path + (f"?{query}" if query else "")
        connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_cls(
            parsed.hostname,
            parsed.port,
            timeout=self.config.connect_timeout_sec,
        )
        try:
            connection.connect()
            if connection.sock is not None:
                connection.sock.settimeout(self.config.read_timeout_sec)
            connection.request("GET", request_path, headers=self.request_headers)
            response = connection.getresponse()
            body = response.read()
            headers = {key.lower(): value for key, value in response.getheaders()}
            return HttpPayload(status=response.status, headers=headers, body=body)
        finally:
            connection.close()

    def _require_status(self, payload: HttpPayload, *, expected: int) -> None:
        if payload.status == expected:
            return
        body = payload.body[:500].decode("utf-8", errors="replace")
        raise DataBridgeRequestError(
            f"DataBridge returned HTTP {payload.status}: {self.redact(body)}"
        )


def _load_project_env() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _is_empty_export_response(payload: HttpPayload) -> bool:
    if payload.status != 404:
        return False
    try:
        body = json.loads(payload.body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(body, dict)
        and body.get("success") is False
        and "查询结果为空" in str(body.get("error", ""))
    )
