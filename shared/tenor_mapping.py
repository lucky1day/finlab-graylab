from __future__ import annotations

import re
from typing import Iterable


TENOR_TO_INDICATOR = {
    "1Y": "TB1YWI0C",
    "3Y": "TB3YWI0C",
    "5Y": "TB5YWI0C",
    "7Y": "TB7YWI0C",
    "10Y": "TB0YWI0C",
}


def normalize_tenor(value: object) -> str:
    """标准化期限标识。"""
    text_value = str(value).strip().upper()
    return text_value.replace("年", "Y")


def indicator_for_tenor(tenor: object) -> str | None:
    """返回 Wind 收益率指标代码；常规 NY 期限按 TB{N}YWI0C 推导。"""
    normalized = normalize_tenor(tenor)
    if normalized in TENOR_TO_INDICATOR:
        return TENOR_TO_INDICATOR[normalized]
    match = re.fullmatch(r"([1-9][0-9]*)Y", normalized)
    if match:
        return f"TB{match.group(1)}YWI0C"
    return None


def indicator_map_for_tenors(tenors: Iterable[object]) -> dict[str, str]:
    """构建 indicators_code -> tenor 映射，跳过无法识别的期限。"""
    result: dict[str, str] = {}
    for tenor in tenors:
        normalized = normalize_tenor(tenor)
        code = indicator_for_tenor(normalized)
        if code:
            result[code] = normalized
    return result
