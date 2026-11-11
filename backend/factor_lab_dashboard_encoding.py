"""Dashboard canonical snapshot 的 JSON/gzip 编码与字节预算。"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from typing import Any, Mapping

from backend.factor_lab_dashboard_semantics import DashboardDataError


MAX_RAW_JSON_BYTES = 1_500_000

MAX_GZIP_JSON_BYTES = 100_000



@dataclass(frozen=True, slots=True)
class SnapshotEncoding:
    """Canonical snapshot 的 identity/gzip 编码及其精确字节数。"""

    raw_body: bytes
    gzip_body: bytes
    raw_size: int
    gzip_size: int



def encode_canonical_snapshot(payload: Mapping[str, Any]) -> SnapshotEncoding:
    """按未来 route 的固定参数编码 canonical snapshot 并校验字节预算。"""
    raw_body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(raw_body) > MAX_RAW_JSON_BYTES:
        raise DashboardDataError(
            "dashboard raw JSON exceeds budget: "
            f"bytes={len(raw_body)} limit={MAX_RAW_JSON_BYTES}"
        )

    gzip_body = gzip.compress(raw_body, compresslevel=6)
    if len(gzip_body) > MAX_GZIP_JSON_BYTES:
        raise DashboardDataError(
            "dashboard gzip JSON exceeds budget: "
            f"bytes={len(gzip_body)} limit={MAX_GZIP_JSON_BYTES}"
        )
    return SnapshotEncoding(
        raw_body=raw_body,
        gzip_body=gzip_body,
        raw_size=len(raw_body),
        gzip_size=len(gzip_body),
    )

