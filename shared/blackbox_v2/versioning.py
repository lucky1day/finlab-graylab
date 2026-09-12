from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

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
    if "factor_input_mode" in raw:
        canonical["factor_input_mode"] = str(raw["factor_input_mode"])
    if "fact_horizon" in raw:
        canonical["fact_horizon"] = raw["fact_horizon"]
    if "native_attachments" in raw:
        raise ValueError("native_attachments is not supported")
    if "incremental_state" in raw:
        if raw["incremental_state"] is not True:
            raise ValueError("incremental_state must be literal true when present")
        canonical["incremental_state"] = True
    schedule = _required_mapping(raw, "schedule")
    timeout_sec = schedule.get("timeout_sec")
    canonical["schedule"] = {
        "cron": str(_required_value(schedule, "cron", "schedule.cron")),
        "timezone": str(schedule.get("timezone", DEFAULT_TIMEZONE)),
        "timeout_sec": int(timeout_sec) if timeout_sec is not None else None,
    }
    if "deliveries" in raw:
        from shared.scheme_config_schema import validate_target_deliveries

        validate_target_deliveries(raw)
        canonical["deliveries"] = sorted(
            (dict(item) for item in raw["deliveries"]), key=lambda item: item["target_tenor"],
        )
    else:
        delivery = _required_mapping(raw, "delivery")
        canonical["delivery"] = {
            "script": str(_required_value(delivery, "script", "delivery.script")),
            "metadata": str(_required_value(delivery, "metadata", "delivery.metadata")),
        }
    if "platform_inputs" in raw:
        # 存量配置仅保留在版本哈希中，避免本次平台减负让已激活 exact
        # scheme_version 漂移；运行时和 Intake 已完全不读取该字段。
        canonical["platform_inputs"] = raw["platform_inputs"]
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
