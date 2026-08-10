from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024


class ApiProbeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_summary: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_summary = error_summary or message


def fetch_json(
    url: str,
    timeout_sec: int = 10,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> tuple[Any, int]:
    """执行只读 JSON API 探针。"""
    if max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be positive")
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=timeout_sec) as response:
            status = int(getattr(response, "status", 200))
            raw = response.read(max_response_bytes + 1)
    except HTTPError as exc:
        raw = exc.read(max_response_bytes + 1)
        summary = raw[:512].decode("utf-8", errors="replace")
        raise ApiProbeError(
            f"HTTP {exc.code}: {summary}",
            status_code=int(exc.code),
            error_summary=summary,
        ) from exc
    if len(raw) > max_response_bytes:
        raise ApiProbeError(
            f"API response exceeds {max_response_bytes} bytes",
            status_code=status,
            error_summary=f"response exceeds {max_response_bytes} bytes",
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiProbeError(
            f"API response is not valid UTF-8 JSON: {exc}",
            status_code=status,
            error_summary=str(exc)[:512],
        ) from exc
    if not isinstance(payload, (dict, list)):
        raise ApiProbeError(
            "API payload must be a JSON object or array",
            status_code=status,
        )
    return payload, status
