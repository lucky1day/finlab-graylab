"""存量 Native 适配器的源输出校验。"""

from __future__ import annotations

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
