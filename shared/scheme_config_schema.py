"""方案配置的低层纯校验契约，供运行时与 harness 共同复用。"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from shared.task_specs import (
    ALLOWED_DATA_FREQUENCIES,
    ALLOWED_FREQUENCIES,
    ALLOWED_TASK_TYPES,
)


SCHEME_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
ALLOWED_TENORS = {"1Y", "3Y", "5Y", "7Y", "10Y"}
ALLOWED_STATUS = {"active", "paused"}
ALLOWED_RUNTIME_TYPES = {"native_adapter", "blackbox_v2"}
ALLOWED_INPUT_SOURCES = {"legacy_db", "data_bridge_current"}
ALLOWED_VERSION_STATUS = {"draft", "validated", "shadow", "active", "paused", "retired"}
TASK_TYPE_ERROR = "task_type must be one of " + ", ".join(sorted(ALLOWED_TASK_TYPES))
MAX_OWNER_LENGTH = 64
FORBIDDEN_OWNER_PLACEHOLDERS = frozenset({"--", "unknown", "待定"})

# 已有 Registry 身份的事实步长；只允许这九个存量方案保留旧业务键。
PRESERVED_FACT_HORIZONS = {
    "weekly_10y_d_overlay_0529": ("weekly_point", 6),
    "weekly_5y_direct_0529": ("weekly_point", 6),
    "weekly_7y_cross_d_overlay_0529": ("weekly_point", 6),
    "weekly_avg_10y_lgbm_0529": ("weekly_average", 6),
    "weekly_avg_1y_lgbm_0529": ("weekly_average", 6),
    "weekly_avg_5y_lgbm_0529": ("weekly_average", 6),
    "monthly_10y_rf_top5_0629": ("monthly", 30),
    "monthly_1y_rf_top30_0629": ("monthly", 30),
    "monthly_5y_knn_top20_0629": ("monthly", 30),
}

# 临时迁移例外：仅已接入原 ID 的 W2/W3A/W3B 候选可声明不参与执行的 Native 附件。
NATIVE_ATTACHMENT_SCHEMES = frozenset({
    "daily_5y_2_v28",
    "daily_7y_1_v28",
    "liwei_0616_cons_sda_k3_div_k10",
    "liwei_0616_5y01_full_oos_k3_div_k10",
    "liwei_0616_10y01_cons_say_k3_div_k10",
    "liwei_0616_10y01_full_oos_k3_div_k10",
    "liwei_0616_10y02_cons_say_k3_div_k5",
})


def validate_native_attachments(scheme_id: str, attachments: object) -> None:
    """校验迁移期附件的精确文件清单；不授予 Native 执行资格。"""
    if scheme_id not in NATIVE_ATTACHMENT_SCHEMES:
        raise ValueError("native_attachments requires an approved migration scheme")
    if not isinstance(attachments, dict) or not attachments:
        raise ValueError("native_attachments must be a non-empty SHA-256 mapping")
    for name, digest in attachments.items():
        if not isinstance(name, str):
            raise ValueError("native_attachments path must be a string")
        parts = name.split("/")
        if (any(part in {"", ".", "..", "__pycache__"} for part in parts)
                or "\\" in name
                or not (name in {"predict.py", "inference.py", "__init__.py"}
                        or len(parts) > 1 and parts[0] in {"core", "benchmarks"})):
            raise ValueError("native_attachments path is outside the retained Native layout")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("native_attachments requires lowercase SHA-256 values")


def resolve_fact_horizon(
    scheme_id: str, task_type: str, execution_horizon: int,
    fact_horizon: int | None = None,
) -> int:
    """将已批准存量方案的执行桶投影至原事实步长，不改变日期语义。"""
    if fact_horizon is None:
        return execution_horizon
    if (type(fact_horizon) is not int or execution_horizon != 1
            or PRESERVED_FACT_HORIZONS.get(scheme_id) != (task_type, fact_horizon)):
        raise ValueError("fact_horizon does not match an approved scheme/task mapping")
    return fact_horizon


def normalize_scheme_owner(value: object) -> str:
    """校验并返回 Registry/Metadata 共用的方案来源值。"""
    if not isinstance(value, str) or not value:
        raise ValueError("owner must be a non-empty string")
    if value != value.strip():
        raise ValueError("owner must not contain surrounding whitespace")
    if len(value) > MAX_OWNER_LENGTH:
        raise ValueError(
            f"owner must not exceed {MAX_OWNER_LENGTH} characters"
        )
    if value.casefold() in FORBIDDEN_OWNER_PLACEHOLDERS:
        raise ValueError("owner must not use a placeholder value")
    if any(
        marker in value
        for marker in ("\n", "\r", "<", ">")
    ) or any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("owner must be single-line plain text")
    return value


def validate_config(raw: dict, dirname: str) -> list[str]:
    """纯字典校验 config.yaml，返回错误列表。"""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ["config.yaml must contain a mapping"]

    scheme_id = raw.get("scheme_id")
    if not isinstance(scheme_id, str) or not scheme_id.strip():
        errors.append("scheme_id must be a non-empty string")
    else:
        if not SCHEME_ID_PATTERN.fullmatch(scheme_id):
            errors.append("scheme_id must match ^[a-z][a-z0-9_]*$")
        if scheme_id != dirname:
            errors.append("scheme_id must match directory name")

    runtime_type = raw.get("runtime_type", "native_adapter")
    if runtime_type not in ALLOWED_RUNTIME_TYPES:
        errors.append("runtime_type must be native_adapter or blackbox_v2")

    if "fact_horizon" in raw:
        expected = PRESERVED_FACT_HORIZONS.get(scheme_id) if isinstance(scheme_id, str) else None
        if (runtime_type != "blackbox_v2" or expected is None
                or type(raw["fact_horizon"]) is not int
                or raw["fact_horizon"] != expected[1]):
            errors.append("fact_horizon requires an approved Blackbox scheme and exact value")

    if "incremental_state" in raw:
        if runtime_type != "blackbox_v2":
            errors.append("incremental_state is only supported for Blackbox V2")
        if raw["incremental_state"] is not True:
            errors.append("incremental_state must be literal true when present")

    if "native_attachments" in raw:
        try:
            if runtime_type != "blackbox_v2":
                raise ValueError("native_attachments is only supported for Blackbox V2")
            validate_native_attachments(scheme_id, raw["native_attachments"])
        except ValueError as exc:
            errors.append(str(exc))

    if runtime_type == "blackbox_v2":
        errors.extend(_validate_blackbox_config(raw))
        return errors

    for field in ("name", "description"):
        if not isinstance(raw.get(field), str) or not raw.get(field, "").strip():
            errors.append(f"{field} must be a non-empty string")

    horizon = raw.get("horizon")
    if not isinstance(horizon, int) or horizon <= 0:
        errors.append("horizon must be a positive integer")

    tenors = raw.get("tenors")
    if not isinstance(tenors, list) or not tenors:
        errors.append("tenors must be a non-empty list")
    else:
        invalid = [item for item in tenors if not isinstance(item, str) or item not in ALLOWED_TENORS]
        if invalid:
            errors.append(f"tenors contain unsupported values: {invalid}")

    frequency = raw.get("frequency")
    if frequency not in ALLOWED_FREQUENCIES:
        errors.append(
            "frequency must be one of " + ", ".join(sorted(ALLOWED_FREQUENCIES))
        )

    if raw.get("task_type") not in ALLOWED_TASK_TYPES:
        errors.append(TASK_TYPE_ERROR)

    schedule = raw.get("schedule")
    if not isinstance(schedule, dict):
        errors.append("schedule must be a mapping")
    else:
        cron = schedule.get("cron")
        if not isinstance(cron, str) or len(cron.split()) != 5:
            errors.append("schedule.cron must be a valid 5-field cron string")
        timezone = schedule.get("timezone", "Asia/Shanghai")
        if not isinstance(timezone, str) or not _valid_timezone(timezone):
            errors.append("schedule.timezone must be a valid timezone")
        timeout_sec = schedule.get("timeout_sec")
        if timeout_sec is not None and (not isinstance(timeout_sec, int) or timeout_sec <= 0):
            errors.append("schedule.timeout_sec must be a positive integer when present")

    entry_point = raw.get("entry_point", "predict.run")
    if entry_point != "predict.run":
        errors.append("entry_point must be predict.run")

    if raw.get("status") not in ALLOWED_STATUS:
        errors.append("status must be active or paused")

    input_source = raw.get("input_source", "legacy_db")
    if input_source not in ALLOWED_INPUT_SOURCES:
        errors.append("input_source must be legacy_db or data_bridge_current")

    input_spec = raw.get("input_spec")
    if not isinstance(input_spec, dict):
        errors.append("input_spec must be a mapping")
    else:
        data_version = input_spec.get("data_version")
        if not isinstance(data_version, str) or not data_version.strip():
            errors.append("input_spec.data_version must be a non-empty string")
        required = input_spec.get("required_columns")
        if not isinstance(required, list) or not required or not all(isinstance(item, str) and item for item in required):
            errors.append("input_spec.required_columns must be a non-empty list of strings")
        if frequency == "weekly":
            weekly_variant = input_spec.get("weekly_variant")
            if not isinstance(weekly_variant, str) or not weekly_variant.strip():
                errors.append("input_spec.weekly_variant is required for weekly schemes")
        auxiliary_inputs = input_spec.get("auxiliary_inputs")
        if auxiliary_inputs is not None:
            if not isinstance(auxiliary_inputs, list) or not auxiliary_inputs:
                errors.append("input_spec.auxiliary_inputs must be a non-empty list when present")
            else:
                seen_frequencies: set[str] = set()
                for idx, item in enumerate(auxiliary_inputs):
                    if not isinstance(item, dict):
                        errors.append(f"input_spec.auxiliary_inputs[{idx}] must be a mapping")
                        continue
                    aux_frequency = item.get("frequency")
                    if aux_frequency not in ALLOWED_DATA_FREQUENCIES:
                        errors.append(
                            f"input_spec.auxiliary_inputs[{idx}].frequency must be one of daily, weekly, monthly"
                        )
                    else:
                        if aux_frequency == frequency:
                            errors.append(
                                f"input_spec.auxiliary_inputs[{idx}].frequency must differ from scheme frequency"
                            )
                        if aux_frequency in seen_frequencies:
                            errors.append(f"input_spec.auxiliary_inputs contain duplicate frequency: {aux_frequency}")
                        seen_frequencies.add(aux_frequency)

                    aux_data_version = item.get("data_version")
                    if not isinstance(aux_data_version, str) or not aux_data_version.strip():
                        errors.append(f"input_spec.auxiliary_inputs[{idx}].data_version must be a non-empty string")

                    aux_required = item.get("required_columns")
                    if (
                        not isinstance(aux_required, list)
                        or not aux_required
                        or not all(isinstance(col, str) and col for col in aux_required)
                    ):
                        errors.append(
                            f"input_spec.auxiliary_inputs[{idx}].required_columns must be a non-empty list of strings"
                        )

    if frequency in {"weekly", "monthly", "quarterly", "annual"}:
        target_rule = raw.get("target_rule")
        if not isinstance(target_rule, str) or not target_rule.strip():
            errors.append("target_rule is required for weekly/monthly schemes")

    backtest = raw.get("backtest")
    if backtest is not None:
        if not isinstance(backtest, dict):
            errors.append("backtest must be a mapping when present")
        else:
            if "runner" in backtest and (not isinstance(backtest["runner"], str) or not backtest["runner"].strip()):
                errors.append("backtest.runner must be a non-empty string")
            runner_args = backtest.get("runner_args")
            if runner_args is not None:
                if not isinstance(runner_args, list) or not all(
                    isinstance(item, str) and item for item in runner_args
                ):
                    errors.append("backtest.runner_args must be a list of non-empty strings")
                elif "--no-persist" in runner_args:
                    errors.append("backtest.runner_args must not include --no-persist")
            benchmark_required = backtest.get("benchmark_required")
            if benchmark_required is not None and not isinstance(benchmark_required, bool):
                errors.append("backtest.benchmark_required must be a boolean")
            if benchmark_required is True:
                benchmark_id = backtest.get("benchmark_id")
                if not isinstance(benchmark_id, str) or not benchmark_id.strip():
                    errors.append("backtest.benchmark_id is required when benchmark_required=true")
                data_source = backtest.get("data_source")
                if not isinstance(data_source, str) or not data_source.strip():
                    errors.append("backtest.data_source is required when benchmark_required=true")
            if frequency == "weekly":
                if backtest.get("predict_start_date") != "2025-01-01":
                    errors.append("backtest.predict_start_date must be 2025-01-01 for weekly backtests")
            elif frequency in {"daily", "monthly", "quarterly", "annual"}:
                if backtest.get("start_date") != "2025-01-01":
                    errors.append("backtest.start_date must be 2025-01-01 for daily/monthly backtests")

    return errors


def _validate_blackbox_config(raw: dict) -> list[str]:
    errors: list[str] = []
    if raw.get("input_source") != "data_bridge_current":
        errors.append("Blackbox V2 input_source must be data_bridge_current")
    factor_input_mode = raw.get("factor_input_mode", "legacy_v1")
    if factor_input_mode not in {"legacy_v1", "algorithm_managed"}:
        errors.append(
            "Blackbox V2 factor_input_mode must be legacy_v1 or "
            "algorithm_managed"
        )
    for field in ("runtime_profile", "data_schema_version"):
        if not isinstance(raw.get(field), str) or not raw.get(field, "").strip():
            errors.append(f"{field} must be a non-empty string")
    if raw.get("status") not in ALLOWED_STATUS:
        errors.append("status must be active or paused")
    if raw.get("version_status") not in ALLOWED_VERSION_STATUS:
        errors.append("version_status must be one of draft, validated, shadow, active, paused, retired")
    display_name = raw.get("display_name")
    if display_name is not None and (
        not isinstance(display_name, str) or not display_name.strip()
    ):
        errors.append(
            "Blackbox V2 display_name must be a non-empty string when present"
        )
    schedule = raw.get("schedule")
    if not isinstance(schedule, dict):
        errors.append("schedule must be a mapping")
    else:
        cron = schedule.get("cron")
        if not isinstance(cron, str) or len(cron.split()) != 5:
            errors.append("schedule.cron must be a valid 5-field cron string")
        timezone = schedule.get("timezone", "Asia/Shanghai")
        if not isinstance(timezone, str) or not _valid_timezone(timezone):
            errors.append("schedule.timezone must be a valid timezone")
        timeout_sec = schedule.get("timeout_sec")
        if type(timeout_sec) is not int or timeout_sec <= 0:
            errors.append(
                "Blackbox V2 schedule.timeout_sec must be a positive integer"
            )

    delivery = raw.get("delivery")
    if not isinstance(delivery, dict):
        errors.append("delivery must be a mapping")
    else:
        for field in ("script", "metadata"):
            value = delivery.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"delivery.{field} must be a non-empty relative path")
            elif Path(value).is_absolute() or ".." in Path(value).parts:
                errors.append(f"delivery.{field} must stay inside the scheme directory")
    return errors


def _valid_timezone(value: str) -> bool:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return False
    return True
