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


def factor_lab_url(base_url: str, *, data_source: str | None = None) -> str:
    url = f"{base_url.rstrip('/')}/api/backtests/factor-lab"
    if data_source is None:
        return url
    from urllib.parse import urlencode

    return f"{url}?{urlencode({'data_source': data_source})}"


def schemes_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/schemes"


def health_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/health"


def metrics_url(base_url: str, registry_scheme_id: str) -> str:
    return f"{base_url.rstrip().rstrip('/')}/api/metrics/{registry_scheme_id}"


def find_factor_lab_cell(payload: dict[str, Any], registry_scheme_ids: list[str]) -> dict[str, Any] | None:
    """在 factor-lab 矩阵 payload 中定位指定方案的可展示格子。"""
    rows = payload.get("schemes", [])
    if not isinstance(rows, list):
        return None
    scheme_id_set = {str(item) for item in registry_scheme_ids}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("scheme_id") or "") not in scheme_id_set:
            continue
        if "monthly_metrics" in row or "daily_rows" in row:
            return row
    return None


def metrics_cell_present(payload: dict[str, Any]) -> bool:
    """判定 /api/metrics/{id} 返回了前端所需的月度或日度指标字段。"""
    return isinstance(payload.get("monthly_metrics"), list) or isinstance(payload.get("daily_rows"), list)
