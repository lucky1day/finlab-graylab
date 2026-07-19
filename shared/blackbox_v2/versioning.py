from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


FUNCTIONAL_FIELDS = (
    "runtime_type",
    "input_source",
    "runtime_profile",
    "data_schema_version",
    "schedule",
    "delivery",
)


def canonical_platform_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """提取参与 Blackbox V2 版本计算的平台执行配置。"""
    return {field: raw[field] for field in FUNCTIONAL_FIELDS}


def compute_blackbox_config_hash(raw: Mapping[str, Any]) -> str:
    """计算 Blackbox V2 canonical 平台配置哈希。"""
    payload = json.dumps(
        canonical_platform_config(raw),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
