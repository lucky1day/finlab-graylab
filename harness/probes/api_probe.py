from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
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


def factor_lab_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/backtests/factor-lab"


def metrics_url(base_url: str, scheme_id: str, tenor: str) -> str:
    query = urlencode({"tenor": tenor})
    return f"{base_url.rstrip().rstrip('/')}/api/metrics/{scheme_id}?{query}"


def find_factor_lab_cell(payload: dict[str, Any], scheme_id: str, tenors: list[str]) -> dict[str, Any] | None:
    """在 factor-lab 矩阵 payload 中定位指定方案的可展示格子。"""
    rows = payload.get("schemes", [])
    if not isinstance(rows, list):
        return None
    tenor_set = {str(item) for item in tenors}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("scheme_id") != scheme_id:
            continue
        tenor = str(row.get("tenor") or "")
        if tenor_set and tenor not in tenor_set:
            continue
        if "monthly_metrics" in row or "daily_rows" in row:
            return row
    return None


def metrics_cell_present(payload: dict[str, Any]) -> bool:
    """判定 /api/metrics/{id} 返回了前端所需的月度或日度指标字段。"""
    return isinstance(payload.get("monthly_metrics"), list) or isinstance(payload.get("daily_rows"), list)
