"""存量 Native 适配器的源输出校验。"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any, Collection


def require_source_integer(
    value: Any, *, field: str, allowed_values: Collection[int] | None = None,
) -> int:
    """读取有限整数，保留 CSV 整数表示，拒绝截断小数或布尔。"""
    if hasattr(value, "item"):
        value = value.item()
    message = f"source prediction direction/integer {field} is invalid"
    if isinstance(value, bool) or value is None:
        raise RuntimeError(message)
    try:
        numeric = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError(message) from exc
    if not numeric.is_finite() or numeric != numeric.to_integral_value():
        raise RuntimeError(message)
    if allowed_values is not None and numeric not in allowed_values:
        raise RuntimeError(message)
    return int(numeric)


def select_frequency_row(source_rows: list[dict[str, Any]], frequency: str, scheme_id: str) -> dict[str, Any]:
    """从源输出中选择唯一的目标频率，缺失或重复均拒绝。"""
    matches = [row for row in source_rows if str(row.get("frequency")) == frequency]
    if len(matches) != 1:
        raise RuntimeError(f"{scheme_id}: expected exactly one source row for {frequency}, got {len(matches)}")
    return matches[0]


def source_int_or_none(value: Any) -> int | None:
    """保留既有审计字段清洗；不用于最终方向判定。"""
    value = clean_source_value(value)
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def source_float_or_none(value: Any) -> float | None:
    """将可读的有限审计数值转换为浮点。"""
    value = clean_source_value(value)
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(parsed) or math.isinf(parsed) else parsed


def source_text_or_none(value: Any) -> str | None:
    """保留可选审计文本。"""
    value = clean_source_value(value)
    return None if value is None else str(value)


def clean_source_value(value: Any) -> Any:
    """清理日/月来源的标量审计字段，不改变模型内部数值。"""
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, str):
        return None if value == "" else value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    return value
