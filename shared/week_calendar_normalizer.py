from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Mapping, Any


def normalize_week_calendar_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return calendar rows with isolated forward week_id jumps repaired."""
    items = [_normalize_row(row) for row in rows]
    items.sort(key=lambda item: item["rdate"])
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        current = dict(item)
        if _is_isolated_forward_jump(items, normalized, index):
            previous = normalized[-1]
            current["week_id"] = previous["week_id"]
            current["_week_id_int"] = previous["_week_id_int"]
        normalized.append(current)
    for item in normalized:
        item.pop("_week_id_int", None)
        item.pop("_is_trading", None)
    return normalized


def _normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    # 局部导入避免与 calendar_service 形成模块级循环依赖。
    from shared.calendar_service import is_trading_day_row

    item = dict(row)
    item["rdate"] = _date_string(item["rdate"])
    item["_week_id_int"] = _week_id_int(item.get("week_id"))
    item["_is_trading"] = is_trading_day_row(item["rdate"], item.get("trade_flag"))
    return item


def _is_isolated_forward_jump(
    items: list[dict[str, Any]],
    normalized: list[dict[str, Any]],
    index: int,
) -> bool:
    if index == 0 or not normalized:
        return False
    current = items[index]
    if not current["_is_trading"]:
        return False
    previous = normalized[-1]
    previous_week = previous["_week_id_int"]
    current_week = current["_week_id_int"]
    if previous_week is None or current_week is None or current_week <= previous_week:
        return False

    non_trading_until_next_trade = []
    for later in items[index + 1 :]:
        if later["_is_trading"]:
            break
        non_trading_until_next_trade.append(later)
    return any(later["_week_id_int"] == previous_week for later in non_trading_until_next_trade)


def _week_id_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip().replace(".0", "")
    if not text:
        return None
    return int(text)


def _date_string(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return datetime.strptime(str(value), "%Y-%m-%d").date().isoformat()
