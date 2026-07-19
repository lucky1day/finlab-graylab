from __future__ import annotations

import json
from typing import Any
from urllib.request import Request, urlopen


def fetch_json(url: str, timeout_sec: int = 10) -> tuple[dict[str, Any], int]:
    """执行只读 JSON API 探针。"""
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    with urlopen(request, timeout=timeout_sec) as response:
        raw = response.read().decode("utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"API payload must be a JSON object: {url}")
        return payload, int(getattr(response, "status", 200))


def factor_lab_url(base_url: str, *, data_source: str | None = None) -> str:
    url = f"{base_url.rstrip('/')}/api/backtests/factor-lab"
    if data_source is None:
        return url
    from urllib.parse import urlencode

    return f"{url}?{urlencode({'data_source': data_source})}"


def schemes_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/schemes"


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
