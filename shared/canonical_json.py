"""跨控制面复用的稳定 JSON 编码与摘要。"""

from __future__ import annotations

import hashlib
import json


def canonical_json_bytes(value: object) -> bytes:
    """返回键序、分隔符和 Unicode 表示均固定的 JSON bytes。"""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("value is not canonical JSON data") from exc
    return encoded.encode("utf-8")


def canonical_json_sha256(value: object) -> str:
    """计算 canonical JSON 的 SHA-256。"""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
