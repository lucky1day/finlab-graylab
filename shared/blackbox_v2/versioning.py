from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from shared.blackbox_v2.platform_input_registry import (
    normalize_platform_input_ids,
)


REQUIRED_TOP_LEVEL_FIELDS = (
    "runtime_type",
    "input_source",
    "runtime_profile",
    "data_schema_version",
)
DEFAULT_TIMEZONE = "Asia/Shanghai"


def canonical_platform_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """提取参与 Blackbox V2 版本计算的平台执行配置。"""
    canonical = {
        field: str(_required_value(raw, field, field))
        for field in REQUIRED_TOP_LEVEL_FIELDS
    }
    schedule = _required_mapping(raw, "schedule")
    delivery = _required_mapping(raw, "delivery")
    timeout_sec = schedule.get("timeout_sec")
    canonical["schedule"] = {
        "cron": str(_required_value(schedule, "cron", "schedule.cron")),
        "timezone": str(schedule.get("timezone", DEFAULT_TIMEZONE)),
        "timeout_sec": int(timeout_sec) if timeout_sec is not None else None,
    }
    canonical["delivery"] = {
        "script": str(_required_value(delivery, "script", "delivery.script")),
        "metadata": str(_required_value(delivery, "metadata", "delivery.metadata")),
    }
    if "platform_inputs" in raw:
        canonical["platform_inputs"] = list(
            normalize_platform_input_ids(raw["platform_inputs"])
        )
    return canonical


def compute_blackbox_config_hash(raw: Mapping[str, Any]) -> str:
    """计算 Blackbox V2 canonical 平台配置哈希。"""
    payload = json.dumps(
        canonical_platform_config(raw),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _required_mapping(raw: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    value = _required_value(raw, field, field)
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a mapping")
    return value


def _required_value(raw: Mapping[str, Any], field: str, path: str) -> Any:
    if field not in raw:
        raise ValueError(f"{path} is required")
    return raw[field]
